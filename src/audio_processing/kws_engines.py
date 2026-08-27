"""Production KWS engine bindings (Porcupine primary, Sherpa-ONNX secondary).

Snowboy is deprecated upstream; Porcupine provides the robust commercial-grade
fallback layer requested for hands-free wake-word on macOS/Linux/Windows.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

from src.logging import get_logger
from src.utils.config_manager import ConfigManager
from src.utils.resource_finder import get_app_root, get_user_data_dir

logger = get_logger()

# Built-in Porcupine keywords that map to Zesty hands-free aliases.
DEFAULT_PORCUPINE_KEYWORDS = ("jarvis", "computer")

# Wake phrase -> Porcupine built-in keyword (when available).
WAKE_PHRASE_TO_PORCUPINE = {
    "jarvis": "jarvis",
    "computer": "computer",
    "hey siri": "hey siri",
    "hey google": "hey google",
}


def _discover_porcupine_keyword_paths() -> list[Path]:
    """Collect custom .ppn files shipped with the app or in user data."""
    paths: list[Path] = []
    seen: set[str] = set()
    for root in (get_app_root() / "models" / "porcupine", get_user_data_dir() / "porcupine"):
        if not root.is_dir():
            continue
        for ppn in sorted(root.glob("*.ppn")):
            key = str(ppn.resolve())
            if key not in seen:
                paths.append(ppn)
                seen.add(key)
    return paths


def _resolve_porcupine_access_key(config: ConfigManager) -> str:
    return (
        os.environ.get("PICOVOICE_ACCESS_KEY", "").strip()
        or (config.get_config("WAKE_WORD_OPTIONS.PICOVOICE_ACCESS_KEY") or "").strip()
    )


def _resolve_builtin_keywords(config: ConfigManager) -> list[str]:
    import pvporcupine

    configured = config.get_config("WAKE_WORD_OPTIONS.PORCUPINE_KEYWORDS")
    if configured:
        candidates = list(configured)
    else:
        candidates = list(DEFAULT_PORCUPINE_KEYWORDS)

    primary = (config.get_config("WAKE_WORD_OPTIONS.WAKE_WORD") or "").strip().lower()
    mapped = WAKE_PHRASE_TO_PORCUPINE.get(primary)
    if mapped:
        candidates.insert(0, mapped)

    for alias in config.get_config("WAKE_WORD_OPTIONS.PHONETIC_ALIASES") or []:
        mapped_alias = WAKE_PHRASE_TO_PORCUPINE.get(str(alias).strip().lower())
        if mapped_alias:
            candidates.append(mapped_alias)

    valid: list[str] = []
    seen: set[str] = set()
    for keyword in candidates:
        key = str(keyword).strip().lower()
        if key and key in pvporcupine.KEYWORDS and key not in seen:
            valid.append(key)
            seen.add(key)
    return valid


class PorcupineKwsEngine:
    """Picovoice Porcupine keyword spotter (16 kHz int16 frames)."""

    name = "Porcupine"

    def __init__(self, porcupine, keyword_labels: Sequence[str]):
        self._porcupine = porcupine
        self._keyword_labels = list(keyword_labels)
        self._frame_length = int(porcupine.frame_length)
        self._buffer = np.array([], dtype=np.int16)

    @property
    def keyword_labels(self) -> list[str]:
        return list(self._keyword_labels)

    @classmethod
    def try_create(cls, config: ConfigManager) -> Optional["PorcupineKwsEngine"]:
        access_key = _resolve_porcupine_access_key(config)
        if not access_key:
            logger.info(
                "Porcupine KWS skipped: set PICOVOICE_ACCESS_KEY env or "
                "WAKE_WORD_OPTIONS.PICOVOICE_ACCESS_KEY in config.json"
            )
            return None

        try:
            import pvporcupine
        except ImportError:
            logger.warning("pvporcupine not installed — Porcupine KWS unavailable")
            return None

        builtin_keywords = _resolve_builtin_keywords(config)
        keyword_paths = _discover_porcupine_keyword_paths()
        if not builtin_keywords and not keyword_paths:
            logger.warning(
                "Porcupine KWS: no built-in keywords or custom .ppn files found"
            )
            return None

        sensitivity = float(
            config.get_config("WAKE_WORD_OPTIONS.PORCUPINE_SENSITIVITY", 0.65)
        )
        sensitivity = max(0.0, min(1.0, sensitivity))
        total = len(builtin_keywords) + len(keyword_paths)
        sensitivities = [sensitivity] * total

        create_kwargs: dict = {"access_key": access_key, "sensitivities": sensitivities}
        if builtin_keywords:
            create_kwargs["keywords"] = builtin_keywords
        if keyword_paths:
            create_kwargs["keyword_paths"] = [str(p) for p in keyword_paths]

        try:
            porcupine = pvporcupine.create(**create_kwargs)
        except Exception as e:
            logger.error(f"Porcupine init failed: {e}", exc_info=True)
            return None

        labels = [k.upper() for k in builtin_keywords]
        labels.extend(pp.stem.replace("_", " ").replace("-", " ").title() for pp in keyword_paths)

        logger.info(
            "Porcupine KWS ready: builtins=%s custom_ppn=%s frame_length=%s",
            builtin_keywords,
            [p.name for p in keyword_paths],
            porcupine.frame_length,
        )
        return cls(porcupine, labels)

    def process(self, audio_f32: np.ndarray) -> Optional[str]:
        if self._porcupine is None:
            return None

        pcm = (np.clip(audio_f32, -1.0, 1.0) * 32767.0).astype(np.int16)
        if pcm.size == 0:
            return None

        self._buffer = np.concatenate([self._buffer, pcm])
        while len(self._buffer) >= self._frame_length:
            frame = self._buffer[: self._frame_length]
            self._buffer = self._buffer[self._frame_length :]
            keyword_index = self._porcupine.process(frame)
            if keyword_index >= 0:
                label = self._keyword_labels[keyword_index]
                logger.info(f"PORCUPINE_WAKE_HIT: {label!r} (index={keyword_index})")
                return label
        return None

    def shutdown(self) -> None:
        if self._porcupine is not None:
            try:
                self._porcupine.delete()
            except Exception as e:
                logger.debug(f"Porcupine shutdown: {e}")
            self._porcupine = None
        self._buffer = np.array([], dtype=np.int16)


def format_engine_label(
    porcupine: Optional[PorcupineKwsEngine], sherpa_active: bool
) -> str:
    parts: list[str] = []
    if porcupine is not None:
        parts.append("Porcupine")
    if sherpa_active:
        parts.append("Sherpa-ONNX")
    return "+".join(parts) if parts else "None"
