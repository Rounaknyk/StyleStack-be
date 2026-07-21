from __future__ import annotations

import logging
from collections.abc import Iterable

from firebase_admin import messaging

from app.core.config import get_settings

logger = logging.getLogger("stylestack.push")

ANDROID_NOTIFICATION_ICON = "ic_stat_stylestack"
ANDROID_NOTIFICATION_COLOR = "#006B6B"


def _chunks(values: list[str], size: int = 1000) -> Iterable[list[str]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def sync_broadcast_topic(tokens: list[str], *, subscribed: bool) -> None:
    """Keep FCM topic membership aligned with the user's notification opt-in."""
    cleaned = list(dict.fromkeys(token for token in tokens if token))
    if not cleaned:
        return
    topic = get_settings().broadcast_notification_topic
    operation = (
        messaging.subscribe_to_topic if subscribed else messaging.unsubscribe_from_topic
    )
    for batch in _chunks(cleaned):
        response = operation(batch, topic)
        logger.info(
            "broadcast_topic_membership_updated subscribed=%s success=%s failure=%s",
            subscribed,
            response.success_count,
            response.failure_count,
        )


def platform_message_options(*, image_url: str | None = None) -> dict:
    """Return consistent Android/iOS appearance for every StyleStack push."""
    return {
        "android": messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(
                icon=ANDROID_NOTIFICATION_ICON,
                color=ANDROID_NOTIFICATION_COLOR,
                sound="default",
                image=image_url,
            ),
        ),
        "apns": messaging.APNSConfig(
            headers={"apns-priority": "10"},
            payload=messaging.APNSPayload(
                aps=messaging.Aps(
                    sound="default",
                    mutable_content=bool(image_url),
                )
            ),
            fcm_options=(
                messaging.APNSFCMOptions(image=image_url) if image_url else None
            ),
        ),
    }


def build_multicast_message(
    *,
    title: str,
    body: str,
    data: dict[str, str],
    tokens: list[str],
    image_url: str | None = None,
) -> messaging.MulticastMessage:
    return messaging.MulticastMessage(
        notification=messaging.Notification(title=title, body=body, image=image_url),
        data=data,
        tokens=tokens,
        **platform_message_options(image_url=image_url),
    )


def build_topic_message(
    *,
    title: str,
    body: str,
    data: dict[str, str],
    image_url: str | None = None,
) -> messaging.Message:
    return messaging.Message(
        notification=messaging.Notification(title=title, body=body, image=image_url),
        data=data,
        topic=get_settings().broadcast_notification_topic,
        **platform_message_options(image_url=image_url),
    )
