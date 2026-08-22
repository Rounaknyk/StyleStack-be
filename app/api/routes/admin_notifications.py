from __future__ import annotations

import logging
from uuid import uuid4
from typing import Literal

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from firebase_admin import messaging
from pydantic import BaseModel, Field, HttpUrl, model_validator

from app.core.config import get_settings
from app.core.supabase import get_supabase_client
from app.dependencies.auth import OwnerUser
from app.services.push_notifications import build_topic_message, sync_broadcast_topic

router = APIRouter()
logger = logging.getLogger("stylestack.admin_notifications")

BroadcastDestination = Literal[
    "today",
    "wardrobe",
    "planner",
    "profile",
    "notifications",
    "saved_styles",
    "outfit",
]


class BroadcastNotificationRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=500)
    destination: BroadcastDestination = "today"
    outfit_id: str | None = Field(default=None, max_length=100)
    image_url: HttpUrl | None = None
    dry_run: bool = False

    @model_validator(mode="after")
    def validate_destination(self):
        if not self.title.strip() or not self.body.strip():
            raise ValueError("title and body cannot be blank")
        if self.destination == "outfit" and not self.outfit_id:
            raise ValueError("outfit_id is required when destination is outfit")
        if self.destination != "outfit" and self.outfit_id:
            raise ValueError("outfit_id is only valid for the outfit destination")
        if self.image_url and self.image_url.scheme != "https":
            raise ValueError("image_url must use HTTPS")
        return self


class BroadcastNotificationResponse(BaseModel):
    message_id: str
    topic: str
    subscribed_devices: int
    dry_run: bool


class BroadcastMediaResponse(BaseModel):
    image_url: str


def _get_eligible_broadcast_tokens() -> list[str]:
    """Backfill existing opted-in devices before each owner broadcast."""
    client = get_supabase_client()
    enabled_profiles = (
        client.table("profiles")
        .select("firebase_uid")
        .eq("notification_enabled", True)
        .execute()
        .data
        or []
    )
    enabled_uids = [row["firebase_uid"] for row in enabled_profiles]
    if not enabled_uids:
        return []

    tokens: list[str] = []
    # PostgREST URL length stays bounded while FCM accepts at most 1,000 topic
    # membership changes per request.
    for offset in range(0, len(enabled_uids), 200):
        rows = (
            client.table("device_tokens")
            .select("token")
            .in_("owner_firebase_uid", enabled_uids[offset : offset + 200])
            .execute()
            .data
            or []
        )
        tokens.extend(str(row["token"]) for row in rows if row.get("token"))
    return list(set(tokens))


@router.post("/media", response_model=BroadcastMediaResponse)
async def upload_broadcast_media(
    current_user: OwnerUser,
    image: UploadFile = File(description="JPEG, PNG, or WebP notification image"),
) -> BroadcastMediaResponse:
    content_type = (image.content_type or "").lower()
    if content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Image must be JPEG, PNG, or WebP")
    payload = await image.read(5 * 1024 * 1024 + 1)
    await image.close()
    if not payload:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Image is empty")
    if len(payload) > 5 * 1024 * 1024:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Image must be 5 MB or smaller")

    extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[content_type]
    path = f"admin/broadcast/{uuid4()}{extension}"
    bucket = get_supabase_client().storage.from_(get_settings().supabase_storage_bucket)
    try:
        bucket.upload(path=path, file=payload, file_options={"content-type": content_type, "upsert": "false"})
        signed = bucket.create_signed_url(path, 7 * 24 * 60 * 60)
        image_url = (
            signed.get("signedURL") or signed.get("signedUrl")
        ) if isinstance(signed, dict) else None
        if not image_url:
            raise RuntimeError("Storage did not return a signed URL")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("broadcast_media_upload_failed error_type=%s", type(exc).__name__)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Could not upload notification media") from exc
    logger.info("broadcast_media_uploaded has_media=true")
    return BroadcastMediaResponse(image_url=image_url)


@router.post(
    "/broadcast",
    response_model=BroadcastNotificationResponse,
)
def send_broadcast_notification(
    payload: BroadcastNotificationRequest,
    current_user: OwnerUser,
) -> BroadcastNotificationResponse:
    tokens = _get_eligible_broadcast_tokens()
    if not tokens:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No opted-in notification devices are registered.",
        )
    subscribed_devices = len(tokens)

    image_url = str(payload.image_url) if payload.image_url else None
    deep_link = f"stylestack://{payload.destination}"
    data = {
        "type": "broadcast",
        "destination": payload.destination,
        "deep_link": deep_link,
    }
    if payload.outfit_id:
        data["outfit_id"] = payload.outfit_id
    if image_url:
        data["image_url"] = image_url

    try:
        # Multicast can send to up to 500 tokens per batch
        message_id = "multicast"
        from app.services.push_notifications import build_multicast_message
        for i in range(0, len(tokens), 500):
            batch = tokens[i : i + 500]
            response = messaging.send_each_for_multicast(
                build_multicast_message(
                    tokens=batch,
                    title=payload.title.strip(),
                    body=payload.body.strip(),
                    data=data,
                    image_url=image_url,
                ),
                dry_run=payload.dry_run,
            )
            if response.failure_count > 0:
                for idx, res in enumerate(response.responses):
                    if not res.success:
                        logger.warning(
                            "broadcast_notification_token_failed token=%s error=%s",
                            batch[idx],
                            res.exception,
                        )
    except Exception as exc:
        logger.exception(
            "broadcast_notification_failed destination=%s has_media=%s dry_run=%s",
            payload.destination,
            bool(image_url),
            payload.dry_run,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Firebase could not send the broadcast notification.",
        ) from exc

    logger.info(
        "broadcast_notification_sent destination=%s has_media=%s devices=%s dry_run=%s",
        payload.destination,
        bool(image_url),
        subscribed_devices,
        payload.dry_run,
    )
    return BroadcastNotificationResponse(
        message_id=message_id,
        topic=get_settings().broadcast_notification_topic,
        subscribed_devices=subscribed_devices,
        dry_run=payload.dry_run,
    )
