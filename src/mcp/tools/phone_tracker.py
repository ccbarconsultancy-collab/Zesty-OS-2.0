"""App-less mobile phone tracker: web receiver, telemetry state, and MCP tools."""

from __future__ import annotations

import asyncio
import io
import json
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import aiohttp
from aiohttp import web

from src.core.event_bus import EventBus, Events
from src.logging import get_logger
from src.mcp.tooling import McpTool, PropertyList

logger = get_logger()

TRACKER_HOST = "0.0.0.0"
TRACKER_PORT = 8080
SYNC_STALE_SECONDS = 120
GEOCODE_USER_AGENT = "py-xiaozhi-phone-tracker/2.0"

TRACK_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Zesty Phone Link</title>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    background: #000; color: #00f3ff; font-family: 'SF Mono', Menlo, monospace;
    padding: 24px;
  }
  .card {
    width: min(420px, 100%); border: 1px solid rgba(0,243,255,0.35);
    border-radius: 12px; padding: 28px 22px; background: rgba(6,14,16,0.92);
    box-shadow: 0 0 40px rgba(0,243,255,0.12);
  }
  h1 { font-size: 14px; letter-spacing: 0.2em; margin: 0 0 8px; }
  p { font-size: 11px; line-height: 1.6; color: rgba(216,246,248,0.7); margin: 0 0 16px; }
  .status { font-size: 10px; letter-spacing: 0.14em; padding: 10px 12px;
    border-radius: 6px; background: rgba(0,243,255,0.06); border: 1px solid rgba(0,243,255,0.2); }
  .ok { color: #00e676; }
  .warn { color: #ff0055; }
  .pulse-banner {
    display: flex; align-items: center; gap: 10px; margin-bottom: 14px;
    padding: 10px 12px; border-radius: 8px;
    border: 1px solid rgba(0,230,118,0.35); background: rgba(0,40,20,0.35);
    font-size: 10px; letter-spacing: 0.18em; color: rgba(0,230,118,0.55);
    transition: all 0.35s ease;
  }
  .pulse-banner.active {
    color: #00e676; border-color: rgba(0,230,118,0.65);
    box-shadow: 0 0 18px rgba(0,230,118,0.25);
  }
  .pulse-dot {
    width: 8px; height: 8px; border-radius: 50%; background: rgba(255,0,85,0.8);
    box-shadow: 0 0 6px rgba(255,0,85,0.5);
  }
  .pulse-banner.active .pulse-dot {
    background: #00e676; box-shadow: 0 0 10px #00e676;
    animation: pulseGlow 1.4s ease-in-out infinite;
  }
  @keyframes pulseGlow {
    0%, 100% { transform: scale(1); opacity: 1; }
    50% { transform: scale(1.25); opacity: 0.65; }
  }
  .pulse-btn {
    width: 100%; margin-top: 12px; padding: 12px 14px;
    border-radius: 8px; border: 1px solid rgba(0,243,255,0.45);
    background: rgba(0,243,255,0.08); color: #00f3ff;
    font-family: inherit; font-size: 10px; letter-spacing: 0.16em;
    cursor: pointer; text-transform: uppercase;
  }
  .pulse-btn:active { transform: scale(0.98); }
</style>
</head>
<body>
<div class="card">
  <h1>ZESTY PHONE LINK</h1>
  <p>No app required. Tap below to authorize location, then keep this page open for live telemetry pulses.</p>
  <div id="pulseBanner" class="pulse-banner">
    <span class="pulse-dot"></span>
    <span id="pulseLabel">PULSE STANDBY</span>
  </div>
  <div id="status" class="status warn">INITIALIZING TELEMETRY...</div>
  <button id="pulseBtn" class="pulse-btn" type="button">⚡ ENABLE LOCATION PULSE</button>
</div>
<script>
(function () {
  const statusEl = document.getElementById('status');
  const pulseBanner = document.getElementById('pulseBanner');
  const pulseLabel = document.getElementById('pulseLabel');
  const pulseBtn = document.getElementById('pulseBtn');
  let pulseTimer = null;
  let locationArmed = false;
  let lastCoords = { latitude: 0, longitude: 0 };

  function setStatus(text, ok) {
    statusEl.textContent = text;
    statusEl.className = 'status ' + (ok ? 'ok' : 'warn');
  }

  function setPulseConnected(active, label) {
    pulseBanner.classList.toggle('active', !!active);
    pulseLabel.textContent = label || (active ? 'PULSE CONNECTED' : 'PULSE STANDBY');
  }

  function syncEndpoints() {
    const origin = window.location.origin || '';
    const paths = ['/api/phone/sync'];
    if (origin && origin !== 'null') paths.push(origin + '/api/phone/sync');
    return [...new Set(paths)];
  }

  async function postSync(payload) {
    const endpoints = syncEndpoints();
    let lastError = null;
    for (const url of endpoints) {
      try {
        const res = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
          cache: 'no-store'
        });
        if (!res.ok) {
          lastError = new Error('HTTP ' + res.status + ' @ ' + url);
          continue;
        }
        return await res.json();
      } catch (err) {
        lastError = err;
      }
    }
    throw lastError || new Error('Sync endpoint unreachable');
  }

  async function readBattery() {
    if (!navigator.getBattery) return { battery_level: -1, is_charging: false };
    try {
      const bat = await navigator.getBattery();
      return {
        battery_level: Math.round((bat.level || 0) * 100),
        is_charging: !!bat.charging
      };
    } catch (_) {
      return { battery_level: -1, is_charging: false };
    }
  }

  function readPosition(options) {
    return new Promise((resolve, reject) => {
      if (!navigator.geolocation) return reject(new Error('Geolocation unavailable'));
      navigator.geolocation.getCurrentPosition(
        (pos) => resolve({
          latitude: pos.coords.latitude,
          longitude: pos.coords.longitude
        }),
        (err) => reject(err),
        options || { enableHighAccuracy: true, timeout: 15000, maximumAge: 10000 }
      );
    });
  }

  async function buildPayload(gpsStatus) {
    const battery = await readBattery();
    const payload = {
      ...battery,
      latitude: lastCoords.latitude,
      longitude: lastCoords.longitude,
      gps_status: gpsStatus || (locationArmed ? 'fixing' : 'pending'),
      timestamp: new Date().toISOString()
    };
    return { battery, payload };
  }

  async function pulse(gpsStatus) {
    try {
      const { battery, payload } = await buildPayload(gpsStatus);
      const data = await postSync(payload);
      setPulseConnected(true, 'PULSE CONNECTED');
      const batText = battery.battery_level >= 0 ? battery.battery_level + '%' : 'N/A';
      const locText = data.location_name || (payload.latitude && payload.longitude
        ? 'GPS LOCK'
        : 'AWAITING GPS FIX');
      setStatus('LINKED // ' + locText + ' // BAT ' + batText, true);
      return true;
    } catch (err) {
      setPulseConnected(false, 'PULSE STANDBY');
      setStatus('SYNC FAILED: ' + (err && err.message ? err.message : err), false);
      return false;
    }
  }

  async function tryGpsUpgrade() {
    try {
      const position = await readPosition();
      lastCoords = position;
      await pulse('locked');
    } catch (err) {
      setStatus('GPS PENDING // ' + (err.message || err) + ' — pulse still active', true);
      setPulseConnected(true, 'PULSE CONNECTED');
    }
  }

  async function armLocationPulse() {
    locationArmed = true;
    pulseBtn.disabled = true;
    pulseBtn.textContent = '⚡ PULSE ARMED';
    await pulse('arming');
    await tryGpsUpgrade();
    if (!pulseTimer) pulseTimer = setInterval(() => pulse('interval'), 30000);
  }

  pulseBtn.addEventListener('click', armLocationPulse);

  // Immediate heartbeat on load — marks device ONLINE before iOS grants GPS.
  (async function bootPulse() {
    setStatus('SENDING IMMEDIATE PULSE...', false);
    const ok = await pulse('boot');
    if (ok) setStatus('BOOT PULSE SENT // TAP BUTTON FOR GPS', true);
  })();

  window.addEventListener('beforeunload', () => {
    if (pulseTimer) clearInterval(pulseTimer);
  });
})();
</script>
</body>
</html>
"""


@dataclass
class PhoneTelemetry:
    battery_level: int = -1
    is_charging: bool = False
    latitude: float = 0.0
    longitude: float = 0.0
    timestamp: str = ""
    location_name: str = ""
    last_update_mono: float = field(default_factory=time.monotonic)

    def to_dict(self) -> dict[str, Any]:
        return {
            "battery_level": self.battery_level,
            "is_charging": self.is_charging,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timestamp": self.timestamp,
            "location_name": self.location_name,
            "connected": self.is_online(),
            "connection_health": "online" if self.is_online() else "disconnected",
            "last_update": self.timestamp,
        }

    def is_online(self, stale_seconds: float = SYNC_STALE_SECONDS) -> bool:
        if not self.timestamp:
            return False
        return (time.monotonic() - self.last_update_mono) < stale_seconds


class PhoneState:
    """Thread-safe in-memory phone telemetry store."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data = PhoneTelemetry()

    def update(
        self,
        *,
        battery_level: int,
        is_charging: bool,
        latitude: float,
        longitude: float,
        timestamp: str,
        location_name: str = "",
    ) -> PhoneTelemetry:
        with self._lock:
            self._data = PhoneTelemetry(
                battery_level=battery_level,
                is_charging=is_charging,
                latitude=latitude,
                longitude=longitude,
                timestamp=timestamp,
                location_name=location_name,
                last_update_mono=time.monotonic(),
            )
            return PhoneTelemetry(**asdict(self._data))

    def snapshot(self) -> PhoneTelemetry:
        with self._lock:
            return PhoneTelemetry(**asdict(self._data))


def get_local_ip() -> str:
    """Detect the machine's LAN IP for QR pairing."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


async def reverse_geocode(latitude: float, longitude: float) -> str:
    """Resolve coordinates to a human-readable place name."""
    url = (
        "https://nominatim.openstreetmap.org/reverse"
        f"?lat={latitude}&lon={longitude}&format=json&zoom=14&addressdetails=1"
    )
    headers = {"User-Agent": GEOCODE_USER_AGENT}
    try:
        timeout = aiohttp.ClientTimeout(total=6)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return f"{latitude:.4f}, {longitude:.4f}"
                data = await resp.json()
    except Exception as exc:
        logger.debug("Reverse geocode failed: %s", exc)
        return f"{latitude:.4f}, {longitude:.4f}"

    address = data.get("address") or {}
    parts = [
        address.get("suburb"),
        address.get("town"),
        address.get("city"),
        address.get("village"),
        address.get("county"),
        address.get("state"),
    ]
    cleaned = [p for p in parts if p]
    if cleaned:
        return ", ".join(dict.fromkeys(cleaned))
    return data.get("display_name") or f"{latitude:.4f}, {longitude:.4f}"


def build_track_url(ip: str | None = None) -> str:
    host = ip or get_local_ip()
    return f"http://{host}:{TRACKER_PORT}/track"


def generate_qr_png(url: str) -> bytes:
    import qrcode

    qr = qrcode.QRCode(version=None, box_size=8, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class PhoneTrackerService:
    """aiohttp receiver for mobile telemetry + QR endpoint."""

    def __init__(self, event_bus: EventBus | None = None) -> None:
        self.state = PhoneState()
        self._event_bus = event_bus
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._started = False

    @property
    def track_url(self) -> str:
        return build_track_url()

    async def start(self) -> None:
        if self._started:
            return

        app = web.Application(middlewares=[self._cors_middleware])
        app.router.add_get("/track", self._handle_track)
        app.router.add_post("/api/phone/sync", self._handle_sync)
        app.router.add_get("/api/phone/qr", self._handle_qr)
        app.router.add_options("/api/phone/sync", self._handle_options)
        app.router.add_options("/track", self._handle_options)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, TRACKER_HOST, TRACKER_PORT)
        await self._site.start()
        self._started = True

        print(
            "[PHONE TRACKER READY] Web Receiver: "
            f"http://{TRACKER_HOST}:{TRACKER_PORT}/track | "
            "Dynamic QR: Ready | Cyberpunk HUD Map: ARMED",
            flush=True,
        )
        logger.info(
            "Phone tracker listening on %s (QR → %s)",
            f"http://{TRACKER_HOST}:{TRACKER_PORT}/track",
            self.track_url,
        )

    async def stop(self) -> None:
        if not self._started:
            return
        if self._site:
            await self._site.stop()
            self._site = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._started = False
        logger.info("Phone tracker stopped")

    @staticmethod
    @web.middleware
    async def _cors_middleware(request: web.Request, handler):
        if request.method == "OPTIONS":
            return web.Response(headers=_cors_headers())
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = exc
        if isinstance(response, web.StreamResponse):
            for key, value in _cors_headers().items():
                response.headers[key] = value
        return response

    async def _handle_options(self, _request: web.Request) -> web.Response:
        return web.Response(headers=_cors_headers())

    async def _handle_track(self, _request: web.Request) -> web.Response:
        return web.Response(text=TRACK_PAGE_HTML, content_type="text/html")

    async def _handle_sync(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid json"}, status=400)

        try:
            battery_level = int(payload.get("battery_level", -1))
            is_charging = bool(payload.get("is_charging", False))
            latitude = float(payload.get("latitude", 0.0))
            longitude = float(payload.get("longitude", 0.0))
            timestamp = str(payload.get("timestamp") or _utc_now_iso())
            gps_status = str(payload.get("gps_status", "unknown"))
        except (TypeError, ValueError):
            return web.json_response({"error": "invalid payload"}, status=400)

        if latitude == 0.0 and longitude == 0.0:
            location_name = "Awaiting GPS fix"
        else:
            location_name = await reverse_geocode(latitude, longitude)
        telemetry = self.state.update(
            battery_level=battery_level,
            is_charging=is_charging,
            latitude=latitude,
            longitude=longitude,
            timestamp=timestamp,
            location_name=location_name,
        )

        logger.info(
            "[PHONE SYNC SUCCESS] bat=%s%% charging=%s gps=%s lat=%.5f lon=%.5f",
            battery_level,
            is_charging,
            gps_status,
            latitude,
            longitude,
        )

        metrics = telemetry.to_dict()
        if self._event_bus:
            await self._event_bus.emit(Events.PHONE_METRICS_UPDATED, metrics)

        return web.json_response(
            {
                "ok": True,
                "location_name": location_name,
                "connected": True,
            }
        )

    async def _handle_qr(self, _request: web.Request) -> web.Response:
        png = generate_qr_png(self.track_url)
        return web.Response(body=png, content_type="image/png")


def _cors_headers() -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Module singleton (wired at startup)
# ---------------------------------------------------------------------------

_service: PhoneTrackerService | None = None


def get_phone_tracker_service() -> PhoneTrackerService | None:
    return _service


def set_phone_tracker_service(service: PhoneTrackerService | None) -> None:
    global _service
    _service = service


async def start_phone_tracker(event_bus: EventBus) -> PhoneTrackerService:
    service = PhoneTrackerService(event_bus)
    await service.start()
    set_phone_tracker_service(service)
    return service


async def stop_phone_tracker() -> None:
    service = get_phone_tracker_service()
    if service:
        await service.stop()
    set_phone_tracker_service(None)


# ---------------------------------------------------------------------------
# MCP tools (Jesty Voice)
# ---------------------------------------------------------------------------


def _require_service() -> PhoneTrackerService:
    service = get_phone_tracker_service()
    if service is None:
        raise RuntimeError("Phone tracker service is not running")
    return service


async def _emit_show_map() -> None:
    service = get_phone_tracker_service()
    if service and service._event_bus:
        await service._event_bus.emit(Events.PHONE_MAP_SHOW)


def get_phone_status_payload(_args: dict[str, Any]) -> str:
    service = _require_service()
    return json.dumps(service.state.snapshot().to_dict(), ensure_ascii=False)


async def get_phone_location_payload(_args: dict[str, Any]) -> str:
    service = _require_service()
    data = service.state.snapshot().to_dict()
    await _emit_show_map()
    return json.dumps(
        {
            "latitude": data["latitude"],
            "longitude": data["longitude"],
            "location_name": data["location_name"] or "Unknown",
            "connected": data["connected"],
            "timestamp": data["timestamp"],
        },
        ensure_ascii=False,
    )


def register_phone_tools(add_tool: Callable[[McpTool], None]) -> None:
    """Register Jesty Voice phone tracker MCP tools."""
    tools = [
        McpTool(
            "get_phone_status",
            (
                "Returns the linked phone's battery percentage, charging status, "
                "connection health, and last telemetry sync time."
            ),
            PropertyList([]),
            get_phone_status_payload,
        ),
        McpTool(
            "get_phone_location",
            (
                "Returns the phone's latitude, longitude, and formatted address. "
                "Also opens the Cyberpunk Map HUD panel on screen."
            ),
            PropertyList([]),
            get_phone_location_payload,
        ),
    ]
    for tool in tools:
        add_tool(tool)
    logger.info("Registered %d phone tracker MCP tools", len(tools))
