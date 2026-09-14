#!/usr/bin/env python3
"""Subtitle retranslation compatibility entrypoint with approved context locks."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import retranslate_subtitles as legacy
import utterance_translate_v24 as translator


def _arg(name: str) -> str:
    try:
        index = sys.argv.index(name)
        return sys.argv[index + 1]
    except (ValueError, IndexError):
        return ""


def configure_overrides_path() -> None:
    input_value = _arg("--input")
    output_value = _arg("--output-dir")
    if not input_value or not output_value:
        return
    stem = Path(input_value).resolve().stem
    path = Path(output_value).resolve() / f"{stem}.translation-overrides.json"
    os.environ["TRANSLATE_CONTEXT_OVERRIDES_FILE"] = str(path)


configure_overrides_path()
legacy.contextual_translate = translator

if __name__ == "__main__":
    legacy.main()
