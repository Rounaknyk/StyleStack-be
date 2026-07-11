from typing import Any

from fastapi import HTTPException, status
from supabase import Client

from app.core.config import get_settings


def database_error(operation: str, exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"Unable to {operation} because the data service is unavailable",
    )


def ensure_profile(client: Client, user: dict[str, Any]) -> None:
    profile = {
        "firebase_uid": user["uid"],
        "email": user.get("email"),
        "display_name": user.get("name"),
        "avatar_url": user.get("picture"),
    }
    try:
        client.table("profiles").upsert(
            profile, on_conflict="firebase_uid"
        ).execute()
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
