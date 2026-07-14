import logging
from typing import Any, Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from firebase_admin import auth

from app.core.firebase import verify_firebase_token

bearer_scheme = HTTPBearer(auto_error=False)
logger = logging.getLogger("stylestack.auth")


def get_current_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ],
) -> dict[str, Any]:
    """Validate a Bearer Firebase ID token and return its decoded claims."""
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired authentication token",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise unauthorized

    try:
        decoded_token = verify_firebase_token(credentials.credentials)
        logger.debug("firebase_user_authenticated uid=%s", decoded_token["uid"])
        return decoded_token
    except (
        auth.InvalidIdTokenError,
        auth.ExpiredIdTokenError,
        auth.RevokedIdTokenError,
        auth.UserDisabledError,
        ValueError,
    ) as exc:
        logger.warning("firebase_authentication_failed reason=%s", type(exc).__name__)
        raise unauthorized from exc


CurrentUser = Annotated[dict[str, Any], Depends(get_current_user)]
