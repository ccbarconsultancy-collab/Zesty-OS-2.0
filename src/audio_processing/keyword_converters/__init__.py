# -*- coding: utf-8 -*-
import re
from typing import Optional, Tuple

from .base import KeywordConverter
from .bpe_converter import BpeConverter
from .pinyin_converter import PinyinConverter

__all__ = [
    "KeywordConverter",
    "PinyinConverter",
    "BpeConverter",
    "detect_language",
    "get_converter",
    "convert_wake_word",
    "convert_wake_word_phrases",
]

# Singleton converters
_pinyin_converter: Optional[PinyinConverter] = None
_bpe_converter: Optional[BpeConverter] = None


def _get_pinyin_converter() -> PinyinConverter:
    """Get or create PinyinConverter singleton."""
    global _pinyin_converter
    if _pinyin_converter is None:
        _pinyin_converter = PinyinConverter()
    return _pinyin_converter


def _get_bpe_converter() -> BpeConverter:
    """Get or create BpeConverter singleton."""
    global _bpe_converter
    if _bpe_converter is None:
        _bpe_converter = BpeConverter()
    return _bpe_converter


def detect_language(text: str) -> str:
    # Check for Chinese characters
    chinese_pattern = re.compile(r"[\u4e00-\u9fff]")
    if chinese_pattern.search(text):
        return "zh"
    return "en"


def get_converter(language: str) -> KeywordConverter:
    if language == "zh":
        return _get_pinyin_converter()
    elif language == "en":
        return _get_bpe_converter()
    else:
        raise ValueError(f"Unsupported language: {language}")


def convert_wake_word(text: str) -> Tuple[str, str, str]:
    language = detect_language(text)
    converter = get_converter(language)
    keyword_line = converter.convert(text)
    return keyword_line, language, converter.model_path


def convert_wake_word_phrases(phrases: list[str]) -> Tuple[str, str, str]:
    """Convert multiple wake phrases into a multi-line sherpa keywords file body."""
    if not phrases:
        raise ValueError("phrases must not be empty")

    lines: list[str] = []
    lang: str | None = None
    model_path: str | None = None
    for text in phrases:
        phrase = (text or "").strip()
        if not phrase:
            continue
        line, lang, model_path = convert_wake_word(phrase)
        if line not in lines:
            lines.append(line)

    if not lines or lang is None or model_path is None:
        raise ValueError("no valid wake phrases to convert")

    return "\n".join(lines) + "\n", lang, model_path
