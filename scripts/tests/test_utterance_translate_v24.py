import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import utterance_translate_v24 as v24


class UtteranceTranslateV24Tests(unittest.TestCase):
    def test_normalize_source_response_requires_exact_id_coverage(self):
        rows = [
            {"id": 10, "text": "我就是想看看"},
            {"id": 11, "text": "往死路上逼的人"},
        ]
        values = [
            {
                "id": 10,
                "normalizedText": "我就是想看看",
                "changed": False,
                "confidence": 0.98,
                "reason": "",
                "meaningHint": "想看看那些人",
            },
            {
                "id": 11,
                "normalizedText": "往死路上逼的人",
                "changed": False,
                "confidence": 0.97,
                "reason": "",
                "meaningHint": "把人逼到绝境的人",
            },
        ]
        normalized = v24._normalize_source_response(values, rows)
        self.assertEqual([item["id"] for item in normalized], [10, 11])
        self.assertEqual(normalized[1]["meaningHint"], "把人逼到绝境的人")

        with self.assertRaises(RuntimeError):
            v24._normalize_source_response(values[:1], rows)

    def test_review_reason_is_preserved_only_when_translation_changed(self):
        draft = [
            {"sourceIds": [1, 2], "vi": "Lên đường chết", "confidence": 0.6},
            {"sourceIds": [3], "vi": "Tôi hiểu rồi", "confidence": 0.9},
        ]
        normalized = [
            {"sourceIds": [1, 2], "vi": "Dồn vào đường cùng", "confidence": 0.92},
            {"sourceIds": [3], "vi": "Tôi hiểu rồi", "confidence": 0.9},
        ]
        raw = [
            {
                "sourceIds": [1, 2],
                "vi": "Dồn vào đường cùng",
                "changed": True,
                "reviewReason": "dịch literal thành ngữ",
            },
            {
                "sourceIds": [3],
                "vi": "Tôi hiểu rồi",
                "changed": False,
                "reviewReason": "",
            },
        ]
        result = v24._merge_review_extras(normalized, raw, draft)
        self.assertTrue(result[0]["reviewChanged"])
        self.assertIn("literal", result[0]["reviewReason"])
        self.assertNotIn("reviewChanged", result[1])


if __name__ == "__main__":
    unittest.main()
