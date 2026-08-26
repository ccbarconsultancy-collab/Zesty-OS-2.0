"""QWebChannel bridge between the HTML HUD and py-xiaozhi EventBus/UI layer."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal, Slot

from src.logging import get_logger

if TYPE_CHECKING:
    from src.ui.shared.bridge import EventBridge

logger = get_logger()

_STATUS_TO_DEVICE_STATE = {
    "待命": "idle",
    "聆听中...": "listening",
    "说话中...": "speaking",
    "未连接": "disconnected",
}

_ENGLISH_STATUS = {
    "idle": "IDLE",
    "wake_ready": "WAKE READY",
    "listening": "LISTENING",
    "processing": "PROCESSING",
    "speaking": "SPEAKING",
    "disconnected": "DISCONNECTED",
    "error": "ERROR",
}


class HtmlUiBridge(QObject):
    """Exposes py-xiaozhi UI commands to JavaScript via QWebChannel."""

    chatText = Signal(str)
    deviceState = Signal(str)
    connectionState = Signal(str)
    emotion = Signal(str)
    musicLine = Signal(str)
    errorMessage = Signal(str)
    systemNotice = Signal(str)
    buttonLabel = Signal(str)
    autoMode = Signal(bool)
    statusText = Signal(str)
    inputLevel = Signal(float)

    def __init__(self, event_bridge: "EventBridge", parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("htmlUiBridge")
        self._event_bridge = event_bridge
        self._connected = False

    # ----- HTML → Python -----

    @Slot(str)
    def sendText(self, text: str) -> None:
        if text and text.strip():
            logger.debug("HtmlUiBridge: sendText(%s...)", text[:40])
            self.notify_ui_phase("processing")
            self._event_bridge.onSendText(text)

    @Slot()
    def toggleListening(self) -> None:
        logger.debug("HtmlUiBridge: toggleListening")
        self._event_bridge.onManualToggle()

    @Slot()
    def abort(self) -> None:
        logger.debug("HtmlUiBridge: abort")
        self._event_bridge.onAbort()

    @Slot()
    def toggleAutoMode(self) -> None:
        logger.debug("HtmlUiBridge: toggleAutoMode")
        self._event_bridge.onAutoToggle()

    @Slot()
    def openSettings(self) -> None:
        logger.debug("HtmlUiBridge: openSettings")
        self._event_bridge.onOpenSettings()

    @Slot()
    def quit(self) -> None:
        logger.debug("HtmlUiBridge: quit")
        self._event_bridge.onQuitRequest()

    # ----- Python → HTML (called from GuiViewManager ViewPort) -----

    def notify_chat_text(self, text: str) -> None:
        self.chatText.emit(text or "")

    def notify_device_state(self, state: str) -> None:
        self.deviceState.emit(state)
        label = _ENGLISH_STATUS.get(state)
        if label:
            self.statusText.emit(label)

    def notify_ui_phase(self, phase: str) -> None:
        """Push a visual phase (listening / processing / speaking / wake_ready / idle)."""
        self.notify_device_state(phase)

    def notify_connection_state(self, state: str) -> None:
        self._connected = state == "connected"
        self.connectionState.emit(state)
        if state == "connected":
            self.notify_device_state("wake_ready")
        else:
            self.notify_device_state("disconnected")
            self.statusText.emit(_ENGLISH_STATUS["disconnected"])

    def notify_emotion(self, emotion: str) -> None:
        self.emotion.emit(emotion or "")

    def notify_music_line(self, text: str) -> None:
        self.musicLine.emit(text or "")

    def notify_error(self, message: str) -> None:
        self.errorMessage.emit(message or "")
        self.notify_device_state("error")
        self.statusText.emit(_ENGLISH_STATUS["error"])

    def notify_notice(self, message: str) -> None:
        self.systemNotice.emit(message or "")

    def notify_button_label(self, label: str) -> None:
        self.buttonLabel.emit(label or "")

    def notify_auto_mode(self, enabled: bool) -> None:
        self.autoMode.emit(bool(enabled))

    def notify_status(self, status: str, connected: bool) -> None:
        self._connected = connected
        self.connectionState.emit("connected" if connected else "disconnected")
        if not connected:
            self.notify_device_state("disconnected")
            self.statusText.emit(_ENGLISH_STATUS["disconnected"])
            return
        mapped = _STATUS_TO_DEVICE_STATE.get(status)
        if mapped == "idle":
            self.notify_device_state("wake_ready")
        elif mapped:
            self.notify_device_state(mapped)
        elif status:
            self.notify_notice(status)

    def notify_input_level(self, level: float) -> None:
        """Push normalized mic RMS (0–1) for mesh/waveform at IDLE."""
        self.inputLevel.emit(max(0.0, min(1.0, float(level))))
