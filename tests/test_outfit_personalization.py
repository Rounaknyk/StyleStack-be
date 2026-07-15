import unittest
from datetime import date

from app.prompts.outfit_stylist import build_stylist_prompt
from app.services.outfits import _age_group, build_personal_style_context


class OutfitPersonalizationTests(unittest.TestCase):
    def test_age_group_uses_date_without_exposing_birth_date(self) -> None:
        self.assertEqual(_age_group("2000-08-15", date(2026, 7, 15)), "20s")
        self.assertIsNone(_age_group("not-a-date", date(2026, 7, 15)))

    def test_empty_or_uncertain_profile_stays_in_discovery_mode(self) -> None:
        context = build_personal_style_context(
            {
                "gender_identity": "prefer_not_to_say",
                "body_type": "not_sure",
                "style_preferences": ["not_sure"],
                "onboarding_goals": [],
            }
        )
        self.assertTrue(context["discovery_mode"])
        self.assertNotIn("gender_identity", context)
        self.assertNotIn("body_type", context)

    def test_explicit_style_and_fit_preferences_are_available_to_stylist(self) -> None:
        context = build_personal_style_context(
            {
                "gender_identity": "woman",
                "date_of_birth": "1990-01-02",
                "body_type": "athletic",
                "height_cm": 172,
                "style_preferences": ["minimal", "office"],
                "onboarding_goals": ["reduce_decision_fatigue"],
            }
        )
        self.assertFalse(context["discovery_mode"])
        self.assertEqual(context["preferred_styles"], ["minimal", "office"])
        self.assertEqual(context["height_cm"], 172)
        self.assertNotIn("date_of_birth", context)

    def test_prompt_includes_profile_as_context(self) -> None:
        prompt = build_stylist_prompt(
            wardrobe_json="[]",
            weather_json="{}",
            occasion="interview",
            profile_json='{"preferred_styles":["formal"]}',
        )
        self.assertIn("PERSONAL_STYLE_PROFILE", prompt)
        self.assertIn('"formal"', prompt)
        self.assertIn("Treat PERSONAL_STYLE_PROFILE as preference context", prompt)


if __name__ == "__main__":
    unittest.main()
