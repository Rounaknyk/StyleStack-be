"""Permanent, server-owned deletion of a StyleStack user account."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from firebase_admin import auth

from app.core.config import get_settings
from app.core.firebase import get_firebase_app
from app.services.google_calendar import revoke_google_token

logger = logging.getLogger("stylestack.account_deletion")

_STORAGE_PAGE_SIZE = 100
_STORAGE_DELETE_BATCH_SIZE = 100


class AccountDeletionError(RuntimeError):
    """Raised when a required account-deletion stage could not complete."""

    def __init__(self, stage: str) -> None:
        super().__init__(stage)
        self.stage = stage


def _is_storage_folder(entry: dict[str, Any]) -> bool:
    """Supabase represents virtual folders without an object id or metadata."""
    return entry.get("id") is None and entry.get("metadata") is None


def _list_owned_storage_paths(bucket: Any, prefix: str) -> list[str]:
    """Recursively list every object below a user's private storage prefix."""
    paths: list[str] = []
    offset = 0
    while True:
        entries = bucket.list(
            prefix,
            {
                "limit": _STORAGE_PAGE_SIZE,
                "offset": offset,
                "sortBy": {"column": "name", "order": "asc"},
            },
        )
        if not entries:
            break
        for entry in entries:
            name = entry.get("name")
            if not name:
                continue
            child_path = f"{prefix}/{name}"
            if _is_storage_folder(entry):
                paths.extend(_list_owned_storage_paths(bucket, child_path))
            else:
                paths.append(child_path)
        if len(entries) < _STORAGE_PAGE_SIZE:
            break
        offset += len(entries)
    return paths


def _delete_owned_storage(client: Any, uid: str) -> int:
    bucket = client.storage.from_(get_settings().supabase_storage_bucket)
    paths = _list_owned_storage_paths(bucket, uid)
    for start in range(0, len(paths), _STORAGE_DELETE_BATCH_SIZE):
        bucket.remove(paths[start : start + _STORAGE_DELETE_BATCH_SIZE])
    return len(paths)


def _revoke_calendar_access_best_effort(client: Any, uid: str) -> None:
    """Stop future Calendar access without blocking deletion on Google outages."""
    rows = (
        client.table("profiles")
        .select("google_calendar_refresh_token")
        .eq("firebase_uid", uid)
        .limit(1)
        .execute()
        .data
        or []
    )
    token = rows[0].get("google_calendar_refresh_token") if rows else None
    if not token:
        return
    try:
        revoke_google_token(token)
    except (httpx.HTTPError, RuntimeError):
        logger.warning("google_calendar_revoke_failed uid=%s", uid)


def delete_user_account(client: Any, uid: str) -> int:
    """Delete storage, relational data, and the Firebase Authentication user.

    Storage is deleted first so a storage failure never leaves unreferenced
    private objects after the database profile has gone. Deleting the profile
    then clears all owner-scoped relational rows through database cascades.
    Firebase is last, allowing an authenticated retry if an earlier stage fails.
    """
    try:
        _revoke_calendar_access_best_effort(client, uid)
    except Exception:
        # Reading an optional OAuth token must not make permanent deletion
        # unavailable. The subsequent profile deletion still erases the token.
        logger.warning("google_calendar_revoke_lookup_failed uid=%s", uid)

    try:
        deleted_objects = _delete_owned_storage(client, uid)
    except Exception as exc:
        logger.exception("account_storage_deletion_failed uid=%s", uid)
        raise AccountDeletionError("storage") from exc

    try:
        client.table("profiles").delete().eq("firebase_uid", uid).execute()
    except Exception as exc:
        logger.exception("account_database_deletion_failed uid=%s", uid)
        raise AccountDeletionError("database") from exc

    try:
        auth.delete_user(uid, app=get_firebase_app())
    except auth.UserNotFoundError:
        # Makes the final stage safe to retry after a prior partial success.
        pass
    except Exception as exc:
        logger.exception("account_auth_deletion_failed uid=%s", uid)
        raise AccountDeletionError("authentication") from exc

    logger.info(
        "account_deleted uid=%s storage_objects_deleted=%d",
        uid,
        deleted_objects,
    )
    return deleted_objects
