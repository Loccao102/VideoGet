import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import context_overrides as overrides


class ContextOverridesTests(unittest.TestCase):
    def test_approved_character_replaces_generated_name(self):
        context = {
            "characters": [
                {"source": "王嘉仪", "vi": "Vương Giác Nghị", "gender": "unknown", "confidence": 0.6}
            ],
            "relationships": [],
            "terms": [],
        }
        approved = {
            "characters": [
                {
                    "source": "王嘉仪",
                    "preferredName": "Vương Gia Nghi",
                    "gender": "female",
                    "voiceGender": "female",
                }
            ],
            "relationships": [],
            "terms": [],
            "notes": "",
        }
        result = overrides.apply_overrides(context, approved)
        self.assertEqual(result["characters"][0]["vi"], "Vương Gia Nghi")
        self.assertTrue(result["characters"][0]["locked"])
        self.assertEqual(result["characters"][0]["confidence"], 1.0)

    def test_approved_relationship_overrides_generated_relation(self):
        context = {
            "characters": [],
            "relationships": [
                {"from": "王嘉仪", "to": "表叔", "relation": "người quen", "confidence": 0.4}
            ],
            "terms": [],
        }
        approved = {
            "characters": [],
            "relationships": [
                {"from": "王嘉仪", "to": "表叔", "relation": "chú họ - cháu"}
            ],
            "terms": [],
            "notes": "",
        }
        result = overrides.apply_overrides(context, approved)
        self.assertEqual(result["relationships"][0]["relation"], "chú họ - cháu")
        self.assertTrue(result["relationships"][0]["approvedByUser"])

    def test_voice_hint_is_attached_when_speaker_matches_locked_character(self):
        context = {
            "characters": [
                {"source": "王嘉仪", "vi": "Vương Gia Nghi", "voiceGender": "female"}
            ]
        }
        segments = [{"id": 1, "speaker": "Vương Gia Nghi", "vi": "Tôi hiểu rồi."}]
        result = overrides.apply_voice_hints(segments, context)
        self.assertEqual(result[0]["voiceGender"], "female")


if __name__ == "__main__":
    unittest.main()
