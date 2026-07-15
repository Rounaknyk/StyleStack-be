from datetime import date, timedelta
import unittest

from pydantic import ValidationError

from app.models.onboarding import OnboardingCompleteRequest, OnboardingProfileResponse
from app.services.wardrobe import build_profile_sync_payload


class OnboardingModelTests(unittest.TestCase):
    def test_complete_request_normalizes_name_and_deduplicates_selections(self) -> None:
        payload = OnboardingCompleteRequest.model_validate(
            {
                "display_name": "  Rounak   Naik ",
                "gender_identity": "man",
                "date_of_birth": "1995-08-15",
                "body_type": "athletic",
                "height_cm": 173,
                "style_preferences": ["casual", "minimal", "casual"],
                "shopping_frequency": "every_2_3_months",
                "onboarding_goals": [
                    "daily_outfit_ideas",
                    "daily_outfit_ideas",
                    "reduce_decision_fatigue",
                ],
            }
        )

        self.assertEqual(payload.display_name, "Rounak Naik")
        self.assertEqual(payload.style_preferences, ["casual", "minimal"])
        self.assertEqual(
            payload.onboarding_goals,
            ["daily_outfit_ideas", "reduce_decision_fatigue"],
        )

    def test_invalid_enum_and_height_are_rejected(self) -> None:
        base = {
            "display_name": "Rounak",
            "gender_identity": "man",
            "date_of_birth": "1995-08-15",
        }
        with self.assertRaises(ValidationError):
            OnboardingCompleteRequest.model_validate(
                {**base, "style_preferences": ["streetwear"]}
            )
        with self.assertRaises(ValidationError):
            OnboardingCompleteRequest.model_validate({**base, "height_cm": 250})

    def test_uncertain_style_choices_are_exclusive(self) -> None:
        with self.assertRaises(ValidationError):
            OnboardingCompleteRequest.model_validate(
                {
                    "display_name": "Rounak",
                    "gender_identity": "man",
                    "date_of_birth": "1995-08-15",
                    "style_preferences": ["not_sure", "casual"],
                }
            )

    def test_future_and_implausibly_old_birth_dates_are_rejected(self) -> None:
        base = {"display_name": "Rounak", "gender_identity": "man"}
        with self.assertRaises(ValidationError):
            OnboardingCompleteRequest.model_validate(
                {**base, "date_of_birth": date.today() + timedelta(days=1)}
            )
        with self.assertRaises(ValidationError):
            OnboardingCompleteRequest.model_validate(
                {**base, "date_of_birth": date(1900, 1, 1)}
            )

    def test_new_profile_response_is_incomplete_by_default(self) -> None:
        profile = OnboardingProfileResponse.model_validate({})

        self.assertFalse(profile.onboarding_completed)
        self.assertEqual(profile.style_preferences, [])
        self.assertEqual(profile.onboarding_goals, [])


class ProfileSyncTests(unittest.TestCase):
    def test_existing_onboarding_name_is_never_replaced_by_token_name(self) -> None:
        payload = build_profile_sync_payload(
            {
                "uid": "firebase-1",
                "name": "Google Account Name",
                "email": "person@example.com",
                "picture": "https://example.com/avatar.jpg",
            },
            current_display_name="Chosen StyleStack Name",
        )

        self.assertNotIn("display_name", payload)
        self.assertEqual(payload["email"], "person@example.com")
        self.assertEqual(payload["avatar_url"], "https://example.com/avatar.jpg")

    def test_token_name_only_initializes_an_empty_profile_name(self) -> None:
        payload = build_profile_sync_payload(
            {"uid": "firebase-1", "name": "Google Account Name"},
            current_display_name=None,
        )

        self.assertEqual(payload["display_name"], "Google Account Name")

    def test_missing_phone_auth_claims_do_not_write_nulls(self) -> None:
        payload = build_profile_sync_payload(
            {"uid": "phone-user", "name": None, "email": None, "picture": None},
            current_display_name="Rounak",
        )

        self.assertEqual(payload, {"firebase_uid": "phone-user"})


if __name__ == "__main__":
    unittest.main()
