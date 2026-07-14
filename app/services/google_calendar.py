from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.core.config import get_settings

TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
CALENDAR_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


def exchange_server_auth_code(code: str) -> dict[str, Any]:
    settings = get_settings()
    if not settings.google_oauth_client_id or not settings.google_oauth_client_secret:
        raise RuntimeError("Google OAuth server credentials are not configured")
    response = httpx.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "grant_type": "authorization_code",
            "redirect_uri": "",
        },
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def refresh_access_token(refresh_token: str) -> str:
    settings = get_settings()
    if not settings.google_oauth_client_id or not settings.google_oauth_client_secret:
        raise RuntimeError("Google OAuth server credentials are not configured")
    response = httpx.post(
        TOKEN_URL,
        data={
            "refresh_token": refresh_token,
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "grant_type": "refresh_token",
        },
        timeout=20,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def revoke_google_token(token: str) -> None:
    response = httpx.post(REVOKE_URL, params={"token": token}, timeout=15)
    if response.status_code not in (200, 400):
        response.raise_for_status()


def _google_datetime(value: dict) -> tuple[str, bool]:
    if value.get("dateTime"):
        return value["dateTime"], False
    date = value.get("date")
    if not date:
        raise ValueError("Calendar event has no date")
    return f"{date}T00:00:00+00:00", True


def sync_google_events(client: Any, uid: str, access_token: str, days_ahead: int = 90) -> int:
    now = datetime.now(timezone.utc)
    response = httpx.get(
        CALENDAR_EVENTS_URL,
        params={
            "timeMin": (now - timedelta(days=30)).isoformat(),
            "timeMax": (now + timedelta(days=days_ahead)).isoformat(),
            "singleEvents": "true",
            "showDeleted": "true",
            "orderBy": "startTime",
            "maxResults": "250",
        },
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    response.raise_for_status()
    rows = []
    cancelled_ids: list[str] = []
    for event in response.json().get("items", []):
        if not event.get("id"):
            continue
        if event.get("status") == "cancelled":
            cancelled_ids.append(event["id"])
            continue
        try:
            start_at, all_day = _google_datetime(event.get("start", {}))
            end_at, _ = _google_datetime(event.get("end", {}))
        except ValueError:
            continue
        rows.append(
            {
                "owner_firebase_uid": uid,
                "source": "google",
                "external_id": event["id"],
                "title": event.get("summary") or "Untitled event",
                "description": event.get("description"),
                "location": event.get("location"),
                "start_at": start_at,
                "end_at": end_at,
                "all_day": all_day,
                "occasion": "event",
            }
        )
    if rows:
        client.table("calendar_events").upsert(
            rows,
            on_conflict="owner_firebase_uid,source,external_id",
            default_to_null=False,
        ).execute()
    for external_id in cancelled_ids:
        client.table("calendar_events").delete().eq(
            "owner_firebase_uid", uid
        ).eq("source", "google").eq("external_id", external_id).execute()
    client.table("profiles").update(
        {"google_calendar_last_synced_at": now.isoformat()}
    ).eq("firebase_uid", uid).execute()
    return len(rows)
