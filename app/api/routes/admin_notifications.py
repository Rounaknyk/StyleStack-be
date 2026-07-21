from __future__ import annotations

import logging
import secrets
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, status
from firebase_admin import messaging
from pydantic import BaseModel, Field, HttpUrl, model_validator

from app.core.config import get_settings
from app.core.supabase import get_supabase_client
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


def _require_admin_key(x_stylestack_admin_key: str = Header(default="")) -> None:
    configured = get_settings().admin_notification_key
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Broadcast notifications are not configured.",
        )
    if not secrets.compare_digest(x_stylestack_admin_key, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin notification key.",
        )


def _refresh_eligible_topic_members() -> int:
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
        return 0

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
    sync_broadcast_topic(tokens, subscribed=True)
    return len(set(tokens))


@router.post(
    "/broadcast",
    response_model=BroadcastNotificationResponse,
)
def send_broadcast_notification(
    payload: BroadcastNotificationRequest,
    x_stylestack_admin_key: str = Header(default=""),
) -> BroadcastNotificationResponse:
    _require_admin_key(x_stylestack_admin_key)
    subscribed_devices = _refresh_eligible_topic_members()
    if subscribed_devices == 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No opted-in notification devices are registered.",
        )

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
        message_id = messaging.send(
            build_topic_message(
                title=payload.title.strip(),
                body=payload.body.strip(),
                data=data,
                image_url=image_url,
            ),
            dry_run=payload.dry_run,
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
