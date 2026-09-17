#!/usr/bin/env python3
"""Dependency-free regression checks for the OCR decode proxy command builder.

This intentionally inspects the source AST instead of importing the OCR module so CI
can run it without installing OpenCV/RapidOCR. It guards the exact runtime regression
where the thread count was converted to str before being compared with integer zero.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path


SOURCE = Path(__file__).with_name("localize_ocr_subtitles.py")


class OCRDecodeProxySourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        cls.prepare = next(
            node
            for node in cls.tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "prepare_ocr_input"
        )

    def test_threads_stays_numeric_until_ffmpeg_args(self) -> None:
        assignments = [
            node
            for node in ast.walk(self.prepare)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "threads" for target in node.targets)
        ]
        self.assertEqual(len(assignments), 1)
        value = assignments[0].value
        self.assertIsInstance(value, ast.Call)
        self.assertIsInstance(value.func, ast.Attribute)
        self.assertIsInstance(value.func.value, ast.Name)
        self.assertEqual(value.func.value.id, "ocr")
        self.assertEqual(value.func.attr, "env_int")

    def test_threads_comparison_is_numeric(self) -> None:
        comparisons = [
            node.test
            for node in ast.walk(self.prepare)
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "threads"
        ]
        self.assertTrue(comparisons, "expected numeric threads guard")
        comparison = comparisons[0]
        self.assertIsInstance(comparison.ops[0], ast.Gt)
        self.assertIsInstance(comparison.comparators[0], ast.Constant)
        self.assertEqual(comparison.comparators[0].value, 0)


if __name__ == "__main__":
    unittest.main()
