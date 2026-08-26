"""Hands-free wake-word pipeline tests."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from src.constants.constants import AudioConfig, DeviceState, ListeningMode


@pytest.mark.asyncio
async def test_wake_word_on_detected_sends_detect_and_starts_listening():
    from src.plugins.wake_word import WakeWordPlugin

    plugin = WakeWordPlugin()
    plugin._ctx = MagicMock()
    plugin._ctx.is_speaking.return_value = False
    plugin._ctx.is_listening.return_value = False
    plugin._ctx.get_config.return_value.get_config.return_value = False

    plugin._cmd = MagicMock()
    plugin._cmd.start_listening = AsyncMock()
    plugin._cmd.set_keep_listening = MagicMock()
    plugin._cmd.send_wake_word_detected = AsyncMock()
    plugin._cmd.connect_protocol = AsyncMock(return_value=True)

    plugin.detector = MagicMock()
    plugin.get_dep = MagicMock(return_value=None)

    await plugin._on_detected("HEYJESTY", "HEYJESTY")

    plugin._cmd.start_listening.assert_awaited_once_with(ListeningMode.AUTO_STOP)
    plugin._cmd.set_keep_listening.assert_called_once_with(False)
    plugin._cmd.send_wake_word_detected.assert_awaited_once_with("HEYJESTY")


@pytest.mark.asyncio
async def test_wake_word_resumes_on_start_listening_failure():
    from src.plugins.wake_word import WakeWordPlugin

    plugin = WakeWordPlugin()
    plugin._ctx = MagicMock()
    plugin._ctx.is_speaking.return_value = False
    plugin._ctx.is_listening.return_value = False
    plugin._ctx.get_config.return_value.get_config.return_value = False

    plugin._cmd = MagicMock()
    plugin._cmd.start_listening = AsyncMock(side_effect=RuntimeError("boom"))
    plugin._cmd.set_keep_listening = MagicMock()
    plugin._cmd.send_wake_word_detected = AsyncMock()

    plugin.detector = MagicMock()
    plugin.detector.resume = MagicMock()
    plugin._pause_detection = MagicMock()
    plugin._resume_detection = MagicMock()

    await plugin._on_detected("HEYJESTY", "HEYJESTY")

    plugin._resume_detection.assert_called_once()


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


def test_wake_word_processes_pcm_buffer_without_error():
    from src.audio_processing.wake_word_detect import WakeWordDetector

    detector = WakeWordDetector()
    detector.enabled = True
    detector._running = True
    detector._paused = False
    detector._audio_queue = __import__("queue").Queue(maxsize=10)
    detector._sample_rate = AudioConfig.INPUT_SAMPLE_RATE

    mock_spotter = MagicMock()
    mock_spotter.is_ready.return_value = False
    mock_stream = MagicMock()
    detector._keyword_spotter = mock_spotter
    detector._stream = mock_stream
    detector._onnx_lock = __import__("threading").Lock()

    frame = np.zeros(AudioConfig.INPUT_FRAME_SIZE, dtype=np.float32)
    detector.on_audio_data(frame)

    async def run_once():
        await detector._process_audio()

    asyncio.run(run_once())

    mock_stream.accept_waveform.assert_called_once()


def test_use_wake_word_defaults_to_enabled():
    from src.utils.config_manager import ConfigManager

    cm = ConfigManager()
    assert cm.get_config("WAKE_WORD_OPTIONS.USE_WAKE_WORD") is True
