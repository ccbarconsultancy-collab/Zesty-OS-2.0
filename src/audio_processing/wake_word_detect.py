"""Offline OpenWakeWord detector for Zesty OS hands-free wake."""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from src.constants.constants import AudioConfig
from src.logging import get_logger
from src.utils.config_manager import ConfigManager, get_config
from src.utils.resource_finder import get_app_root, get_user_data_dir

logger = get_logger()

_STOP_SENTINEL = object()

DEFAULT_WAKE_WORD = "Zesty"
DEFAULT_PHONETIC_ALIASES = ("Zesty", "Hey Zesty", "hey zesty", "hi zesty")

# Bundled OpenWakeWord models used when custom zesty.onnx is absent.
BUNDLED_FALLBACK_MODELS = ("alexa", "hey_jarvis")
FALLBACK_WAKE_LABELS = {
    "alexa": "alexa",
    "hey_jarvis": "hey jarvis",
    "jarvis": "hey jarvis",
}


def _collect_wake_aliases(config: ConfigManager, primary: str) -> list[str]:
    aliases = config.get_config("WAKE_WORD_OPTIONS.PHONETIC_ALIASES") or []
    if not aliases:
        aliases = list(DEFAULT_PHONETIC_ALIASES)
    else:
        aliases = [*aliases, *DEFAULT_PHONETIC_ALIASES]

    phrases: list[str] = []
    seen: set[str] = set()
    for raw in (primary, *aliases):
        phrase = (raw or "").strip()
        key = phrase.lower()
        if phrase and key not in seen:
            phrases.append(phrase)
            seen.add(key)
    return phrases


def resolve_openwakeword_model_path(config: ConfigManager) -> Optional[Path]:
    """Resolve custom Zesty .onnx model path (app bundle or user data)."""
    configured = config.get_config("WAKE_WORD_OPTIONS.OPENWAKEWORD_MODEL_PATH")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(str(configured)))
    candidates.extend(
        [
            get_app_root() / "models" / "openwakeword" / "zesty.onnx",
            get_app_root() / "models" / "openwakeword" / "hey_zesty.onnx",
            get_user_data_dir() / "openwakeword" / "zesty.onnx",
            get_user_data_dir() / "openwakeword" / "hey_zesty.onnx",
        ]
    )

    for path in candidates:
        resolved = path.expanduser()
        if not resolved.is_absolute():
            resolved = (get_app_root() / resolved).resolve()
        if resolved.is_file():
            return resolved
    return None


class WakeWordDetector:
    """Always-on local wake-word detector using OpenWakeWord (100% offline)."""

    def __init__(self) -> None:
        self.audio_codec = None
        self._running = False
        self._paused = False
        self._detection_task: Optional[asyncio.Task] = None
        self._audio_queue: Optional[queue.Queue] = None

        self._last_detection_time = 0.0
        self._detection_cooldown = 1.5

        self.on_detected_callback: Optional[Callable] = None
        self.on_error: Optional[Callable] = None

        self.enabled = False
        self._model_loaded = False
        self._stopping = False
        self._model_lock = threading.Lock()

        self._oww_model = None
        self._oww_model_keys: list[str] = []
        self._threshold = 0.45
        self._inference_framework = "onnx"

        self._sample_rate = AudioConfig.INPUT_SAMPLE_RATE
        self._primary_wake_phrase = DEFAULT_WAKE_WORD
        self._wake_aliases: list[str] = list(DEFAULT_PHONETIC_ALIASES)

        self._logged_first_audio_frame = False
        self._speech_rms_threshold = 0.006
        self._speech_active = False
        self._last_mic_active_print = 0.0
        self._mic_active_print_interval = 2.0
        self._last_candidate_print = 0.0
        self._fallback_mode = False

    async def initialize(self, model_path: Optional[str] = None) -> bool:
        try:
            config = get_config()
            if not config.get_config("WAKE_WORD_OPTIONS.USE_WAKE_WORD", True):
                logger.info("唤醒词功能已禁用")
                self.enabled = False
                return False

            self._load_config(config)
            self._primary_wake_phrase = (
                config.get_config("WAKE_WORD_OPTIONS.WAKE_WORD") or DEFAULT_WAKE_WORD
            ).strip()
            self._wake_aliases = _collect_wake_aliases(config, self._primary_wake_phrase)

            if model_path:
                config.update_config(
                    "WAKE_WORD_OPTIONS.OPENWAKEWORD_MODEL_PATH",
                    model_path,
                    save=False,
                )

            if self._running:
                await self.stop()
            self._release_model()

            ok = await asyncio.to_thread(self._load_openwakeword_model, config)
            if not ok:
                self.enabled = False
                return False

            self.enabled = True
            self._model_loaded = True
            self._print_startup_banner()
            logger.info(
                "OpenWakeWord 初始化成功: model_keys=%s threshold=%.2f",
                self._oww_model_keys,
                self._threshold,
            )
            return True
        except Exception as e:
            logger.error(f"唤醒词检测器初始化失败: {e}", exc_info=True)
            self.enabled = False
            return False

    def _load_config(self, config: ConfigManager) -> None:
        self._threshold = float(
            config.get_config("WAKE_WORD_OPTIONS.OPENWAKEWORD_THRESHOLD", 0.45)
        )
        self._threshold = max(0.05, min(0.99, self._threshold))
        self._inference_framework = (
            config.get_config("WAKE_WORD_OPTIONS.OPENWAKEWORD_INFERENCE", "onnx")
            or "onnx"
        ).strip().lower()
        if self._inference_framework not in ("onnx", "tflite"):
            self._inference_framework = "onnx"

    def _load_openwakeword_model(self, config: ConfigManager) -> bool:
        from openwakeword.model import Model
        from openwakeword.utils import download_models

        model_path = resolve_openwakeword_model_path(config)
        # Feature / VAD models (one-time network fetch, then offline).
        download_models(model_names=[])

        with self._model_lock:
            if model_path is None:
                print(
                    "[OWW NOTICE] Custom zesty.onnx missing. Loading bundled OpenWakeWord "
                    "models (alexa, hey_siri) as fallback.",
                    flush=True,
                )
                logger.warning(
                    "zesty.onnx not found — using bundled fallback models %s",
                    BUNDLED_FALLBACK_MODELS,
                )
                configured = config.get_config("WAKE_WORD_OPTIONS.OPENWAKEWORD_FALLBACK_MODELS")
                fallback_models = list(configured or BUNDLED_FALLBACK_MODELS)
                download_models(model_names=fallback_models)
                self._fallback_mode = True
                self._oww_model = Model(
                    wakeword_models=fallback_models,
                    inference_framework=self._inference_framework,
                )
            else:
                logger.info("Loading OpenWakeWord model: %s", model_path)
                self._fallback_mode = False
                self._oww_model = Model(
                    wakeword_models=[str(model_path)],
                    inference_framework=self._inference_framework,
                )
            self._oww_model_keys = list(self._oww_model.models.keys())
        return bool(self._oww_model_keys)

    def _label_for_detection(self, model_key: str) -> str:
        """Map OWW model key to phrase sent to activate_on_wake_word()."""
        key = (model_key or "").strip().lower()
        if self._fallback_mode:
            return FALLBACK_WAKE_LABELS.get(key, key.replace("_", " "))
        return self._primary_wake_phrase

    def _print_startup_banner(self) -> None:
        if self._fallback_mode:
            targets = ", ".join(
                FALLBACK_WAKE_LABELS.get(k, k) for k in self._oww_model_keys
            )
            banner = (
                "[CLEAN KWS READY] Legacy Engines Purged | "
                "Active Engine: OpenWakeWord (bundled fallback) | "
                f"Target Wake-Words: {targets} | "
                "Status: UNLOCKED"
            )
        else:
            banner = (
                "[CLEAN KWS READY] Legacy Engines Purged | "
                "Active Engine: OpenWakeWord | "
                f"Target Wake-Word: '{self._primary_wake_phrase.upper()}' | "
                "Status: UNLOCKED"
            )
        print(banner, flush=True)
        logger.info(banner)

    def _release_model(self) -> None:
        with self._model_lock:
            self._oww_model = None
            self._oww_model_keys = []
        self._model_loaded = False

    def on_detected(self, callback: Callable) -> None:
        self.on_detected_callback = callback

    def on_audio_data(self, audio_data: np.ndarray) -> None:
        if not self.enabled or not self._running or self._paused:
            return
        if self._audio_queue is None:
            return

        if not self._logged_first_audio_frame:
            self._logged_first_audio_frame = True
            logger.debug("OpenWakeWord: first audio frame queued")

        try:
            self._audio_queue.put_nowait(audio_data.copy())
        except queue.Full:
            try:
                self._audio_queue.get_nowait()
                self._audio_queue.put_nowait(audio_data.copy())
            except (queue.Empty, queue.Full):
                pass

    async def start(self, audio_codec) -> bool:
        if not self.enabled or not self._oww_model:
            logger.error("OpenWakeWord 未初始化")
            return False

        try:
            self.audio_codec = audio_codec
            self._running = True
            self._paused = False
            self._audio_queue = queue.Queue(maxsize=100)
            self.audio_codec.add_audio_listener(self)
            self._detection_task = asyncio.create_task(self._detection_loop())
            logger.info("OpenWakeWord 检测器已启动")
            return True
        except Exception as e:
            logger.error(f"启动检测器失败: {e}", exc_info=True)
            return False

    async def stop(self) -> None:
        self._stopping = True
        self._running = False

        if self.audio_codec:
            self.audio_codec.remove_audio_listener(self)
            self.audio_codec = None

        if self._audio_queue:
            try:
                self._audio_queue.put_nowait(_STOP_SENTINEL)
            except queue.Full:
                try:
                    self._audio_queue.get_nowait()
                    self._audio_queue.put_nowait(_STOP_SENTINEL)
                except (queue.Empty, queue.Full):
                    pass

        if self._detection_task:
            try:
                await asyncio.wait_for(self._detection_task, timeout=1.0)
            except asyncio.TimeoutError:
                self._detection_task.cancel()
                try:
                    await self._detection_task
                except asyncio.CancelledError:
                    pass
            self._detection_task = None

        if self._audio_queue:
            while not self._audio_queue.empty():
                try:
                    self._audio_queue.get_nowait()
                except queue.Empty:
                    break
            self._audio_queue = None

        self._stopping = False
        logger.info("OpenWakeWord 检测器已停止")

    async def reload(self, model_path: Optional[str] = None) -> bool:
        was_running = self._running
        codec = self.audio_codec
        if not await self.initialize(model_path):
            return False
        if was_running and codec:
            return await self.start(codec)
        return True

    async def shutdown(self) -> None:
        await self.stop()
        self._release_model()
        self.enabled = False

    def pause(self) -> None:
        if not self._paused:
            self._paused = True
            logger.debug("OpenWakeWord 检测已暂停")

    def resume(self) -> None:
        if self._paused:
            self._paused = False
            logger.debug("OpenWakeWord 检测已恢复")

    async def _detection_loop(self) -> None:
        error_count = 0
        while self._running and not self._stopping:
            try:
                if self._paused:
                    await asyncio.sleep(0.1)
                    continue
                await self._process_audio()
                await asyncio.sleep(0.005)
                error_count = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                error_count += 1
                logger.error(
                    f"OpenWakeWord 检测循环错误 ({error_count}/5): {e}",
                    exc_info=True,
                )
                if self.on_error:
                    try:
                        if asyncio.iscoroutinefunction(self.on_error):
                            await self.on_error(e)
                        else:
                            self.on_error(e)
                    except Exception as cb_error:
                        logger.error(f"错误回调失败: {cb_error}")
                if error_count >= 5:
                    break
                await asyncio.sleep(1)

    async def _process_audio(self) -> None:
        if self._stopping or not self._audio_queue or not self._oww_model:
            return

        try:
            audio_data = self._audio_queue.get_nowait()
        except queue.Empty:
            return

        if audio_data is _STOP_SENTINEL:
            return
        if audio_data is None or len(audio_data) == 0:
            return

        audio_data = np.ascontiguousarray(audio_data, dtype=np.float32).reshape(-1)
        rms = float(np.sqrt(np.mean(np.square(audio_data))))
        peak = float(np.max(np.abs(audio_data)))
        self._log_speech_activity(rms, peak)

        detected_label: Optional[str] = None
        best_score = 0.0
        best_key = ""

        with self._model_lock:
            if self._oww_model is None:
                return
            try:
                predictions = self._oww_model.predict(audio_data)
            except Exception as e:
                logger.debug(f"OpenWakeWord predict error: {e}")
                return

        if isinstance(predictions, dict):
            for key, score in predictions.items():
                score_f = float(score)
                if score_f > best_score:
                    best_score = score_f
                    best_key = str(key)
                if score_f >= self._threshold and detected_label is None:
                    detected_label = self._label_for_detection(str(key))

        if best_score > 0.01:
            now = time.time()
            if now - self._last_candidate_print >= 0.25:
                print(
                    f"[OWW CANDIDATE] model={best_key} score={best_score:.3f} "
                    f"threshold={self._threshold:.3f}",
                    flush=True,
                )
                self._last_candidate_print = now

        if detected_label:
            print(
                f"[OWW HIT] '{detected_label}' score={best_score:.3f} "
                f"→ activate_on_wake_word",
                flush=True,
            )
            await self._handle_detection(detected_label)

    def _log_speech_activity(self, rms: float, peak: float) -> None:
        is_speech = (
            rms >= self._speech_rms_threshold
            or peak >= self._speech_rms_threshold * 3
        )
        now = time.time()
        if is_speech:
            if not self._speech_active or (
                now - self._last_mic_active_print >= self._mic_active_print_interval
            ):
                print(
                    f"[MIC AUDIO ACTIVE] Hearing speech/audio input... "
                    f"(RMS={rms:.5f} peak={peak:.5f})",
                    flush=True,
                )
                self._last_mic_active_print = now
            self._speech_active = True
        elif self._speech_active and rms < self._speech_rms_threshold * 0.5:
            self._speech_active = False

    async def _handle_detection(self, result: str) -> None:
        current_time = time.time()
        if current_time - self._last_detection_time < self._detection_cooldown:
            return

        self._last_detection_time = current_time
        logger.info(f"WAKE_WORD_DETECTED: {result!r}")

        self._paused = True
        handed_off = False
        try:
            if self.on_detected_callback:
                if asyncio.iscoroutinefunction(self.on_detected_callback):
                    await self.on_detected_callback(result, result)
                else:
                    self.on_detected_callback(result, result)
                handed_off = True
        except Exception as e:
            logger.error(f"唤醒词回调执行失败: {e}", exc_info=True)
        finally:
            if self._stopping:
                self._paused = False
                return
            if handed_off:
                self._drain_audio_queue()
                return
            await asyncio.sleep(0.3)
            self._drain_audio_queue()
            self._paused = False

    def _drain_audio_queue(self) -> None:
        if not self._audio_queue:
            return
        while True:
            try:
                self._audio_queue.get_nowait()
            except queue.Empty:
                break
