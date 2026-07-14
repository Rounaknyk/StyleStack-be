import unittest

from app.api.routes.outfit_selfies import _candidate
from app.models.ai_tags import OutfitSelfieVisionResult


class OutfitSelfieTests(unittest.TestCase):
    def test_candidate_uses_hidden_visual_tags_and_manual_metadata(self) -> None:
        candidate = _candidate(
            {
                "id": "item-1",
                "name": "Black knit top",
                "category": "shirt",
                "color": "black",
                "description": "Long sleeve crew neck",
                "tags": ["minimal"],
                "ai_visual_tags": ["ribbed-knit", "crew-neck", "long-sleeve"],
            }
        )

        self.assertEqual(candidate["category"], "shirt")
        self.assertEqual(
            candidate["visual_tags"],
            ["ribbed-knit", "crew-neck", "long-sleeve"],
        )

    def test_low_quality_result_can_return_no_items(self) -> None:
        result = OutfitSelfieVisionResult.model_validate(
            {
                "quality_acceptable": False,
                "quality_score": 0.2,
                "quality_feedback": "Step back and use brighter light.",
                "items": [],
            }
        )

        self.assertFalse(result.quality_acceptable)
        self.assertEqual(result.items, [])

    def test_match_confidence_must_be_in_range(self) -> None:
        with self.assertRaises(ValueError):
            OutfitSelfieVisionResult.model_validate(
                {
                    "quality_acceptable": True,
                    "quality_score": 0.9,
                    "quality_feedback": "Clear outfit.",
                    "items": [
                        {
                            "detected_name": "Blue shirt",
                            "category": "shirt",
                            "color": "blue",
                            "description": "Blue button-down shirt",
                            "visual_tags": ["button-down"],
                            "matched_item_id": "item-1",
                            "confidence": 1.2,
                        }
                    ],
                }
            )


if __name__ == "__main__":
    unittest.main()
