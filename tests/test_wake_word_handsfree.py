"""Hands-free OpenWakeWord pipeline tests."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from src.constants.constants import AudioConfig, DeviceState, ListeningMode


@pytest.mark.asyncio
async def test_wake_word_on_detected_calls_activate_on_wake_word():
    from src.plugins.wake_word import WakeWordPlugin

    plugin = WakeWordPlugin()
    plugin._cmd = MagicMock()
    plugin._cmd.activate_on_wake_word = AsyncMock(return_value=True)
    plugin.detector = MagicMock()
    plugin.get_dep = MagicMock(return_value=None)
    plugin._pause_detection = MagicMock()
    plugin._resume_detection = MagicMock()

    await plugin._on_detected("Zesty", "Zesty")

    plugin._cmd.activate_on_wake_word.assert_awaited_once_with("Zesty")


@pytest.mark.asyncio
async def test_wake_word_resumes_on_activation_failure():
    from src.plugins.wake_word import WakeWordPlugin

    plugin = WakeWordPlugin()
    plugin._cmd = MagicMock()
    plugin._cmd.activate_on_wake_word = AsyncMock(return_value=False)
    plugin.detector = MagicMock()
    plugin._pause_detection = MagicMock()
    plugin._resume_detection = MagicMock()

    await plugin._on_detected("Zesty", "Zesty")

    plugin._resume_detection.assert_called_once()


@pytest.mark.asyncio
async def test_activate_on_wake_word_sets_listening_before_protocol():
    from src.bootstrap.session import ConversationSession

    state = MagicMock()
    state.is_speaking.return_value = False
    state.prepare_wake_activation = MagicMock()
    state.set_device_state = AsyncMock()

    protocol = MagicMock()
    protocol.send_start_listening = AsyncMock()
    protocol.send_wake_word_detected = AsyncMock()

    plugins = MagicMock()
    plugins.notify_device_state_changed = AsyncMock()

    session = ConversationSession(
        state=state,
        protocol=protocol,
        plugins=plugins,
        event_bus=MagicMock(),
    )
    session.connect_protocol = AsyncMock(return_value=True)

    ok = await session.activate_on_wake_word("Zesty")

    assert ok is True
    state.prepare_wake_activation.assert_called_once()
    state.set_device_state.assert_any_await(DeviceState.LISTENING)
    plugins.notify_device_state_changed.assert_any_await(DeviceState.LISTENING)
    protocol.send_start_listening.assert_awaited_once_with(ListeningMode.AUTO_STOP)
    protocol.send_wake_word_detected.assert_awaited_once_with("Zesty")


@pytest.mark.asyncio
async def test_wake_word_device_state_idle_resumes_detection():
    from src.plugins.wake_word import WakeWordPlugin

    plugin = WakeWordPlugin()
    plugin.detector = MagicMock()
    plugin.detector.resume = MagicMock()

    await plugin.on_device_state_changed(DeviceState.IDLE)

    plugin.detector.resume.assert_called_once()


@pytest.mark.asyncio
async def test_wake_word_device_state_listening_pauses_detection():
    from src.plugins.wake_word import WakeWordPlugin

    plugin = WakeWordPlugin()
    plugin.detector = MagicMock()
    plugin.detector.pause = MagicMock()

    await plugin.on_device_state_changed(DeviceState.LISTENING)

    plugin.detector.pause.assert_called_once()


@pytest.mark.asyncio
async def test_openwakeword_initialize_loads_model(tmp_path, monkeypatch):
    from src.audio_processing import wake_word_detect
    from src.utils.config_manager import ConfigManager, reset_config

    reset_config()
    model_file = tmp_path / "zesty.onnx"
    model_file.write_bytes(b"onnx")

    cm = ConfigManager()
    cm.update_config("WAKE_WORD_OPTIONS.OPENWAKEWORD_MODEL_PATH", str(model_file), save=False)

    mock_model = MagicMock()
    mock_model.models = {"zesty": object()}

    monkeypatch.setattr(
        wake_word_detect,
        "get_config",
        lambda: cm,
    )

    with patch("openwakeword.utils.download_models"), patch(
        "openwakeword.model.Model", return_value=mock_model
    ):
        detector = wake_word_detect.WakeWordDetector()
        ok = await detector.initialize()

    assert ok is True
    assert detector.enabled is True
    assert detector._oww_model_keys == ["zesty"]


@pytest.mark.asyncio
async def test_openwakeword_process_triggers_on_threshold(tmp_path, monkeypatch):
    from src.audio_processing.wake_word_detect import WakeWordDetector

    detector = WakeWordDetector()
    detector.enabled = True
    detector._running = True
    detector._paused = False
    detector._threshold = 0.4
    detector._primary_wake_phrase = "Zesty"
    detector._audio_queue = __import__("queue").Queue(maxsize=10)
    detector._oww_model = MagicMock()
    detector._oww_model.predict.return_value = {"zesty": 0.85}
    detector._oww_model_keys = ["zesty"]
    detector._handle_detection = AsyncMock()

    frame = np.zeros(AudioConfig.INPUT_FRAME_SIZE, dtype=np.float32)
    detector.on_audio_data(frame)
    await detector._process_audio()

    detector._handle_detection.assert_awaited_once_with("Zesty")


def test_use_wake_word_defaults_to_zesty():
    from src.utils.config_manager import ConfigManager

    cm = ConfigManager()
    assert cm.get_config("WAKE_WORD_OPTIONS.USE_WAKE_WORD") is True
    assert cm.get_config("WAKE_WORD_OPTIONS.WAKE_WORD") == "Zesty"
    assert cm.get_config("WAKE_WORD_OPTIONS.OPENWAKEWORD_MODEL_PATH") == (
        "models/openwakeword/zesty.onnx"
    )


def test_resolve_openwakeword_model_path_prefers_existing_file(tmp_path, monkeypatch):
    from src.audio_processing.wake_word_detect import resolve_openwakeword_model_path
    from src.utils.config_manager import ConfigManager

    model = tmp_path / "zesty.onnx"
    model.write_bytes(b"x")

    cm = ConfigManager()
    cm.update_config("WAKE_WORD_OPTIONS.OPENWAKEWORD_MODEL_PATH", str(model), save=False)

    assert resolve_openwakeword_model_path(cm) == model
