"""Tests for Porcupine KWS engine binding."""

from unittest.mock import MagicMock, patch

import numpy as np


def test_porcupine_skips_without_access_key():
    from src.audio_processing.kws_engines import PorcupineKwsEngine
    from src.utils.config_manager import ConfigManager

    cm = ConfigManager()
    cm.update_config("WAKE_WORD_OPTIONS.PICOVOICE_ACCESS_KEY", "", save=False)

    with patch.dict("os.environ", {}, clear=True):
        assert PorcupineKwsEngine.try_create(cm) is None


def test_porcupine_processes_buffered_frames():
    from src.audio_processing.kws_engines import PorcupineKwsEngine

    mock_porcupine = MagicMock()
    mock_porcupine.frame_length = 4
    mock_porcupine.process.side_effect = [-1, -1, 0]

    engine = PorcupineKwsEngine(mock_porcupine, ["JARVIS", "COMPUTER"])
    assert engine.process(np.zeros(8, dtype=np.float32)) is None
    hit = engine.process(np.ones(4, dtype=np.float32) * 0.1)
    assert hit == "JARVIS"
    assert mock_porcupine.process.call_count == 3


def test_format_engine_label_dual_stack():
    from src.audio_processing.kws_engines import format_engine_label

    assert format_engine_label(MagicMock(), True) == "Porcupine+Sherpa-ONNX"
    assert format_engine_label(None, True) == "Sherpa-ONNX"
    assert format_engine_label(MagicMock(), False) == "Porcupine"
