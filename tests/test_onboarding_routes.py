from datetime import date
import unittest
from unittest.mock import patch

from fastapi import HTTPException, status
from postgrest.exceptions import APIError

from app.api.routes.users import (
    ONBOARDING_SCHEMA_ERROR_DETAIL,
    complete_onboarding,
    read_onboarding,
)
from app.models.onboarding import OnboardingCompleteRequest


class _FailingOnboardingQuery:
    def __init__(self, error: APIError) -> None:
        self.error = error

    def select(self, *_args, **_kwargs):
        return self

    def update(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def execute(self):
        raise self.error


class _FailingOnboardingClient:
    def __init__(self, error: APIError) -> None:
        self.query = _FailingOnboardingQuery(error)

    def table(self, _name: str) -> _FailingOnboardingQuery:
        return self.query


def _api_error(*, code: str, message: str) -> APIError:
    return APIError(
        {
            "message": message,
            "code": code,
            "hint": None,
            "details": None,
        }
    )


class OnboardingRouteSchemaErrorTests(unittest.TestCase):
    def _patch_client(self, error: APIError):
        client = _FailingOnboardingClient(error)
        return (
            patch("app.api.routes.users.get_supabase_client", return_value=client),
            patch("app.api.routes.users.ensure_profile"),
        )

    def test_read_maps_missing_onboarding_column_to_service_unavailable(self) -> None:
        error = _api_error(
            code="42703",
            message="column profiles.gender_identity does not exist",
        )
        client_patch, profile_patch = self._patch_client(error)

        with client_patch, profile_patch, self.assertRaises(HTTPException) as raised:
            read_onboarding({"uid": "firebase-user"})

        self.assertEqual(raised.exception.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(raised.exception.detail, ONBOARDING_SCHEMA_ERROR_DETAIL)

    def test_write_maps_postgrest_schema_cache_error_to_service_unavailable(self) -> None:
        error = _api_error(
            code="PGRST204",
            message=(
                "Could not find the 'onboarding_completed' column of 'profiles' "
                "in the schema cache"
            ),
        )
        payload = OnboardingCompleteRequest(
            display_name="Rounak",
            gender_identity="man",
            date_of_birth=date(1995, 8, 15),
        )
        client_patch, profile_patch = self._patch_client(error)

        with client_patch, profile_patch, self.assertRaises(HTTPException) as raised:
            complete_onboarding(payload, {"uid": "firebase-user"})

        self.assertEqual(raised.exception.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(raised.exception.detail, ONBOARDING_SCHEMA_ERROR_DETAIL)

    def test_unrelated_postgrest_error_is_not_reclassified(self) -> None:
        error = _api_error(
            code="42501",
            message="permission denied for table profiles",
        )
        client_patch, profile_patch = self._patch_client(error)

        with client_patch, profile_patch, self.assertRaises(APIError) as raised:
            read_onboarding({"uid": "firebase-user"})

        self.assertIs(raised.exception, error)


if __name__ == "__main__":
    unittest.main()
