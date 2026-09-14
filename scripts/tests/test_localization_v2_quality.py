import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import localization_v2_quality as quality


class LocalizationV2QualityTests(unittest.TestCase):
    def test_qa_detects_missing_number_and_high_reading_speed(self):
        report = quality.analyze_segments([
            {
                "id": 1,
                "start": 0.0,
                "end": 0.7,
                "text": "这款 X10 售价 299 元",
                "vi": "Mẫu X10 này có mức giá cực kỳ hấp dẫn và rất đáng để mọi người cân nhắc ngay hôm nay",
            }
        ])
        codes = {item["code"] for item in report["issues"]}
        self.assertIn("number_changed_or_missing", codes)
        self.assertIn("reading_speed_critical", codes)
        self.assertEqual(report["status"], "error")

    def test_semantic_blocks_merge_close_fragments_and_split_on_sentence_boundary(self):
        blocks = quality.build_semantic_blocks([
            {"id": 0, "start": 0.0, "end": 1.0, "text": "今天给大家看", "vi": "Hôm nay cho mọi người xem"},
            {"id": 1, "start": 1.1, "end": 2.2, "text": "这款新品。", "vi": "mẫu mới này."},
            {"id": 2, "start": 2.6, "end": 3.5, "text": "先看外观", "vi": "Đầu tiên nhìn ngoại hình"},
        ])
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0]["segmentIds"], [0, 1])
        self.assertEqual(blocks[1]["segmentIds"], [2])

    def test_qa_passes_clean_translation(self):
        report = quality.analyze_segments([
            {
                "id": 0,
                "start": 0.0,
                "end": 3.0,
                "text": "Xiaomi 15 有 5000mAh 电池",
                "vi": "Xiaomi 15 có pin 5000mAh.",
            }
        ])
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["issues"], [])


if __name__ == "__main__":
    unittest.main()
