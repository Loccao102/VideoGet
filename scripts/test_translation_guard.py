#!/usr/bin/env python3
from __future__ import annotations

import unittest

import translation_guard


class TranslationGuardTests(unittest.TestCase):
    def test_identical_chinese_sentence_is_rejected(self) -> None:
        self.assertTrue(
            translation_guard.looks_untranslated_chinese(
                "这个东西真的非常好用",
                "这个东西真的非常好用",
            )
        )

    def test_han_dominant_output_is_rejected(self) -> None:
        self.assertTrue(
            translation_guard.looks_untranslated_chinese(
                "今天给大家分享一个非常实用的方法",
                "今天分享一个非常实用的方法",
            )
        )

    def test_vietnamese_output_is_allowed(self) -> None:
        self.assertFalse(
            translation_guard.looks_untranslated_chinese(
                "这个东西真的非常好用",
                "Món này dùng thật sự rất tiện.",
            )
        )

    def test_short_proper_name_is_allowed(self) -> None:
        self.assertFalse(
            translation_guard.looks_untranslated_chinese(
                "杨幂",
                "杨幂",
            )
        )

    def test_batch_guard_raises_for_echoed_chinese(self) -> None:
        with self.assertRaises(RuntimeError):
            translation_guard.assert_vietnamese_translation(
                [{"id": 7, "text": "这个方法真的很简单"}],
                [{"id": 7, "text": "这个方法真的很简单"}],
                "zh",
            )


if __name__ == "__main__":
    unittest.main()
