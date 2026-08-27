"""Hands-free wake-word pipeline tests."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from src.constants.constants import AudioConfig, DeviceState


@pytest.mark.asyncio
async def test_wake_word_on_detected_sends_detect_and_starts_listening():
    from src.plugins.wake_word import WakeWordPlugin

    plugin = WakeWordPlugin()
    plugin._ctx = MagicMock()
    plugin._ctx.is_speaking.return_value = False
    plugin._ctx.is_listening.return_value = False
    plugin._ctx.get_config.return_value.get_config.return_value = False

    plugin._cmd = MagicMock()
    plugin._cmd.activate_on_wake_word = AsyncMock(return_value=True)

    plugin.detector = MagicMock()
    plugin.get_dep = MagicMock(return_value=None)

    await plugin._on_detected("HEYJESTY", "HEYJESTY")

    plugin._cmd.activate_on_wake_word.assert_awaited_once_with("HEYJESTY")


@pytest.mark.asyncio
async def test_wake_word_resumes_on_start_listening_failure():
    from src.plugins.wake_word import WakeWordPlugin

    plugin = WakeWordPlugin()
    plugin._ctx = MagicMock()
    plugin._ctx.is_speaking.return_value = False
    plugin._ctx.is_listening.return_value = False
    plugin._ctx.get_config.return_value.get_config.return_value = False

    plugin._cmd = MagicMock()
    plugin._cmd.activate_on_wake_word = AsyncMock(return_value=False)

    plugin.detector = MagicMock()
    plugin.detector.resume = MagicMock()
    plugin._pause_detection = MagicMock()
    plugin._resume_detection = MagicMock()

    await plugin._on_detected("HEYJESTY", "HEYJESTY")

    plugin._resume_detection.assert_called_once()


@pytest.mark.asyncio
async def test_activate_on_wake_word_sets_listening_before_protocol():
    from src.bootstrap.session import ConversationSession
    from src.constants.constants import ListeningMode

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

    ok = await session.activate_on_wake_word("Hey Jesty")

    assert ok is True
    state.prepare_wake_activation.assert_called_once()
    state.set_device_state.assert_any_await(DeviceState.LISTENING)
    plugins.notify_device_state_changed.assert_any_await(DeviceState.LISTENING)
    protocol.send_start_listening.assert_awaited_once_with(ListeningMode.AUTO_STOP)
    protocol.send_wake_word_detected.assert_awaited_once_with("Hey Jesty")


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
    assert cm.get_config("WAKE_WORD_OPTIONS.WAKE_WORD") == "Hey Jesty"


def test_wake_word_phonetic_aliases_keywords_file(tmp_path, monkeypatch):
    from src.audio_processing.wake_word_detect import _sync_wake_word_assets
    from src.utils.config_manager import ConfigManager, reset_config

    reset_config()
    monkeypatch.setattr(
        "src.audio_processing.wake_word_detect.get_user_keywords_path",
        lambda lang: tmp_path / f"{lang}_keywords.txt",
    )

    cm = ConfigManager()
    cm.ensure_zesty_wake_word_profile()
    _sync_wake_word_assets(cm)

    body = (tmp_path / "en_keywords.txt").read_text(encoding="utf-8")
    assert "@HEYJESTY" in body
    assert "@HEYJISTRY" in body
    assert "@JESTY" in body
    assert "@HIJESTY" in body
    assert "@JESSIE" in body
    assert "@CHEST" in body
    assert body.count("\n") >= 6


def test_fuzzy_wake_match_accepts_partial_jesty_tokens():
    from src.audio_processing.wake_word_detect import (
        _fuzzy_wake_match,
        _resolve_fuzzy_wake_label,
    )

    tokens = ["▁HE", "Y", "▁JE", "S", "TY"]
    assert _fuzzy_wake_match("", tokens) is True
    assert _resolve_fuzzy_wake_label("", tokens, "Hey Jesty") == "HEYJESTY"


def test_fuzzy_wake_match_rejects_unrelated_tokens():
    from src.audio_processing.wake_word_detect import _fuzzy_wake_match

    assert _fuzzy_wake_match("", ["▁HEL", "LO"]) is False
