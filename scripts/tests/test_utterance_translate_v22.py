import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import utterance_translate as v21
import utterance_translate_v22 as v22


class UtteranceTranslateV22Tests(unittest.TestCase):
    def test_translation_signature_records_role_models(self):
        env = {
            "TRANSLATE_PROVIDER": "ollama",
            "OLLAMA_MODEL": "base-model",
            "TRANSLATE_MODEL": "translate-model",
            "TRANSLATE_CONTEXT_MODEL": "context-model",
            "TRANSLATE_REVIEW_MODEL": "review-model",
            "TRANSLATE_GLOBAL_REVIEW_MODEL": "global-model",
        }
        with patch.dict(os.environ, env, clear=False):
            signature = v22.translation_signature({"version": 1}, profile="drama")
        self.assertEqual(signature["promptVersion"], 5)
        self.assertEqual(signature["contextModel"], "context-model")
        self.assertEqual(signature["translateModel"], "translate-model")
        self.assertEqual(signature["reviewModel"], "review-model")
        self.assertEqual(signature["globalReviewModel"], "global-model")

    def test_utterance_to_segment_keeps_raw_source_ids_and_scene(self):
        source = {
            10: {"id": 10, "start": 1.0, "end": 1.8, "text": "我就是想看看"},
            11: {"id": 11, "start": 1.8, "end": 2.6, "text": "那些人"},
        }
        rows = v21._utterances_to_segments(
            [{"sourceIds": [10, 11], "vi": "Tôi chỉ muốn xem những người đó.", "confidence": 0.9}],
            scene_id=2,
            source_by_id=source,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sourceSegmentIds"], [10, 11])
        self.assertEqual(rows[0]["sceneId"], 3)
        self.assertEqual(rows[0]["utteranceId"], "s3:u1")
        self.assertEqual(rows[0]["start"], 1.0)
        self.assertEqual(rows[0]["end"], 2.6)

    def test_global_review_can_be_disabled_without_mutating_rows(self):
        rows = [{"id": 1, "vi": "Xin chào"}, {"id": 2, "vi": "Tạm biệt"}]
        with patch.dict(os.environ, {"TRANSLATE_GLOBAL_REVIEW_ENABLED": "false"}, clear=False):
            result = v22.global_consistency_review(rows, {})
        self.assertIs(result, rows)


if __name__ == "__main__":
    unittest.main()
