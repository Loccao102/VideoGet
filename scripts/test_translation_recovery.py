#!/usr/bin/env python3
from __future__ import annotations

import ast
import unittest
from pathlib import Path


SOURCE = Path(__file__).with_name("localize_fast.py")
TEXT = SOURCE.read_text(encoding="utf-8")
TREE = ast.parse(TEXT)


class TranslationRecoveryTests(unittest.TestCase):
    def test_prompt_version_bumped(self) -> None:
        self.assertIn("TRANSLATION_PROMPT_VERSION = 3", TEXT)

    def test_forced_vietnamese_recovery_exists(self) -> None:
        self.assertIn("def force_vietnamese_prompt(", TEXT)
        self.assertIn("def repair_translation(", TEXT)
        self.assertIn("TRANSLATE_REPAIR_ON_HAN", TEXT)
        self.assertIn("TRANSLATE_REPAIR_MODEL", TEXT)
        self.assertIn("禁止复制中文原句", TEXT)

    def test_han_failure_splits_without_repeating_same_request(self) -> None:
        fn = next(
            node for node in TREE.body
            if isinstance(node, ast.FunctionDef) and node.name == "translate_resilient"
        )
        source = ast.get_source_segment(TEXT, fn) or ""
        self.assertIn('if "untranslated Chinese" in str(error):', source)
        self.assertIn("break", source)
        self.assertIn("Tách batch lỗi", source)

    def test_normal_path_switches_to_repair_on_han(self) -> None:
        fn = next(
            node for node in TREE.body
            if isinstance(node, ast.FunctionDef) and node.name == "translate_once"
        )
        source = ast.get_source_segment(TEXT, fn) or ""
        self.assertIn("repair_translation(batch, language, provider)", source)
        self.assertIn("assert_vietnamese_translation", source)


if __name__ == "__main__":
    unittest.main()
