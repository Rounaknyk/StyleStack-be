import logging
from typing import Any

from fastapi import HTTPException, status
from supabase import Client

from app.core.config import get_settings

logger = logging.getLogger("stylestack.wardrobe")


def database_error(operation: str, exc: Exception) -> HTTPException:
    logger.error(
        "database_operation_failed operation=%s error_type=%s",
        operation.replace(" ", "_"),
        type(exc).__name__,
    )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"Unable to {operation} because the data service is unavailable",
    )


def build_profile_sync_payload(
    user: dict[str, Any], current_display_name: str | None
) -> dict[str, Any]:
    """Build a Firebase profile sync without replacing an onboarding name."""
    profile: dict[str, Any] = {"firebase_uid": user["uid"]}
    if user.get("email"):
        profile["email"] = user["email"]
    if user.get("picture"):
        profile["avatar_url"] = user["picture"]

    token_name = str(user.get("name") or "").strip()
    if not str(current_display_name or "").strip() and token_name:
        profile["display_name"] = token_name
    return profile


def ensure_profile(client: Client, user: dict[str, Any]) -> None:
    try:
        rows = (
            client.table("profiles")
            .select("display_name")
            .eq("firebase_uid", user["uid"])
            .limit(1)
            .execute()
            .data
            or []
        )
        current_display_name = rows[0].get("display_name") if rows else None
        profile = build_profile_sync_payload(user, current_display_name)
        client.table("profiles").upsert(
            profile, on_conflict="firebase_uid"
        ).execute()
        logger.debug("profile_synchronized uid=%s", user["uid"])
    except Exception as exc:
        raise database_error("create the user profile", exc) from exc


def get_owned_item(client: Client, item_id: str, uid: str) -> dict[str, Any]:
    try:
        response = (
            client.table("wardrobe_items")
            .select("*")
            .eq("id", item_id)
            .eq("owner_firebase_uid", uid)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        raise database_error("load the wardrobe item", exc) from exc

    if not response.data:
        # A single response prevents leaking whether another user owns this ID.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Wardrobe item not found",
        )
    return add_signed_image_url(client, response.data[0])


def add_signed_image_url(client: Client, item: dict[str, Any]) -> dict[str, Any]:
    """Add a one-hour URL for a private wardrobe image without storing the URL."""
    result = dict(item)
    result["image_url"] = None
    image_path = result.get("image_path")
    if not image_path:
        return result
    try:
        signed = client.storage.from_(get_settings().supabase_storage_bucket).create_signed_url(
            image_path, 3600
        )
        if isinstance(signed, dict):
            result["image_url"] = signed.get("signedURL") or signed.get("signedUrl")
    except Exception:
        # Item metadata remains useful if signing is temporarily unavailable.
        pass
    return result
