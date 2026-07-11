import json
from functools import lru_cache
from typing import Any

import firebase_admin
from firebase_admin import auth, credentials

from app.core.config import get_settings


def initialize_firebase() -> firebase_admin.App:
    """Initialize and return the process-wide Firebase Admin app."""
    try:
        return firebase_admin.get_app()
    except ValueError:
        settings = get_settings()
        try:
            service_account: dict[str, Any] = json.loads(
                settings.firebase_service_account_json
            )
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "FIREBASE_SERVICE_ACCOUNT_JSON must contain valid JSON"
            ) from exc

        return firebase_admin.initialize_app(credentials.Certificate(service_account))


@lru_cache
def get_firebase_app() -> firebase_admin.App:
    return initialize_firebase()


def verify_firebase_token(token: str) -> dict[str, Any]:
    """Verify a Firebase ID token and return its decoded claims."""
    app = get_firebase_app()
    return auth.verify_id_token(token, app=app, check_revoked=True)

