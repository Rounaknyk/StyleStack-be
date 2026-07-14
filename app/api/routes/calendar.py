from datetime import datetime, timezone
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Query, Response

from app.core.supabase import get_supabase_client
from app.dependencies.auth import CurrentUser
from app.models.calendar import (
    AppNotificationResponse,
    CalendarEventCreate,
    CalendarEventResponse,
    GoogleCalendarConnectRequest,
    GoogleCalendarConnectionResponse,
)
from app.services.google_calendar import (
    exchange_server_auth_code,
    refresh_access_token,
    revoke_google_token,
    sync_google_events,
)

router = APIRouter()


@router.get("/events", response_model=list[CalendarEventResponse])
def list_events(
    current_user: CurrentUser,
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
):
    query = get_supabase_client().table("calendar_events").select("*").eq(
        "owner_firebase_uid", current_user["uid"]
    )
    if start:
        query = query.gte("start_at", start.isoformat())
    if end:
        query = query.lt("start_at", end.isoformat())
    return query.order("start_at").execute().data or []


@router.post("/events", response_model=CalendarEventResponse, status_code=201)
def create_event(payload: CalendarEventCreate, current_user: CurrentUser):
    if payload.end_at and payload.end_at < payload.start_at:
        raise HTTPException(status_code=422, detail="End time must be after start time")
    row = payload.model_dump(mode="json") | {
        "owner_firebase_uid": current_user["uid"],
        "source": "manual",
    }
    return get_supabase_client().table("calendar_events").insert(row).execute().data[0]


@router.delete("/events/{event_id}", status_code=204)
def delete_event(event_id: UUID, current_user: CurrentUser) -> Response:
    result = get_supabase_client().table("calendar_events").delete().eq(
        "id", str(event_id)
    ).eq("owner_firebase_uid", current_user["uid"]).eq("source", "manual").execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Manual calendar event not found")
    return Response(status_code=204)


@router.get("/google/status", response_model=GoogleCalendarConnectionResponse)
def google_calendar_status(current_user: CurrentUser):
    rows = get_supabase_client().table("profiles").select(
        "google_calendar_connected,google_calendar_email,google_calendar_last_synced_at"
    ).eq("firebase_uid", current_user["uid"]).limit(1).execute().data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Profile not found")
    profile = rows[0]
    return GoogleCalendarConnectionResponse(
        connected=bool(profile.get("google_calendar_connected")),
        email=profile.get("google_calendar_email"),
        last_synced_at=profile.get("google_calendar_last_synced_at"),
    )


@router.post("/google/connect", response_model=GoogleCalendarConnectionResponse)
def connect_google_calendar(payload: GoogleCalendarConnectRequest, current_user: CurrentUser):
    client = get_supabase_client()
    try:
        tokens = exchange_server_auth_code(payload.server_auth_code)
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            raise HTTPException(
                status_code=422,
                detail="Google did not return long-term Calendar access. Disconnect Google and connect again.",
            )
        client.table("profiles").update(
            {
                "google_calendar_connected": True,
                "google_calendar_refresh_token": refresh_token,
                "google_calendar_email": payload.email,
            }
        ).eq("firebase_uid", current_user["uid"]).execute()
        imported = sync_google_events(client, current_user["uid"], tokens["access_token"])
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (400, 401, 403):
            raise HTTPException(
                status_code=422,
                detail="Google Calendar permission was denied or the OAuth credentials do not match.",
            ) from exc
        raise HTTPException(status_code=502, detail="Google authorization failed") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Could not connect Google Calendar") from exc
    return GoogleCalendarConnectionResponse(
        connected=True,
        email=payload.email,
        last_synced_at=datetime.now(timezone.utc),
        imported=imported,
    )


@router.post("/google/sync", response_model=GoogleCalendarConnectionResponse)
def sync_connected_google_calendar(current_user: CurrentUser):
    client = get_supabase_client()
    rows = client.table("profiles").select(
        "google_calendar_connected,google_calendar_refresh_token,google_calendar_email"
    ).eq("firebase_uid", current_user["uid"]).limit(1).execute().data or []
    profile = rows[0] if rows else {}
    refresh_token = profile.get("google_calendar_refresh_token")
    if not profile.get("google_calendar_connected") or not refresh_token:
        raise HTTPException(status_code=409, detail="Google Calendar is not connected")
    try:
        access_token = refresh_access_token(refresh_token)
        imported = sync_google_events(client, current_user["uid"], access_token)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Google Calendar sync failed") from exc
    return GoogleCalendarConnectionResponse(
        connected=True,
        email=profile.get("google_calendar_email"),
        last_synced_at=datetime.now(timezone.utc),
        imported=imported,
    )


@router.delete("/google/connection", status_code=204)
def disconnect_google_calendar(current_user: CurrentUser) -> None:
    client = get_supabase_client()
    rows = client.table("profiles").select("google_calendar_refresh_token").eq(
        "firebase_uid", current_user["uid"]
    ).limit(1).execute().data or []
    token = rows[0].get("google_calendar_refresh_token") if rows else None
    if token:
        try:
            revoke_google_token(token)
        except httpx.HTTPError:
            pass
    client.table("profiles").update(
        {
            "google_calendar_connected": False,
            "google_calendar_refresh_token": None,
            "google_calendar_email": None,
            "google_calendar_last_synced_at": None,
        }
    ).eq("firebase_uid", current_user["uid"]).execute()
    client.table("calendar_events").delete().eq(
        "owner_firebase_uid", current_user["uid"]
    ).eq("source", "google").execute()


@router.get("/notifications", response_model=list[AppNotificationResponse])
def list_notifications(current_user: CurrentUser, limit: int = Query(default=50, ge=1, le=100)):
    return get_supabase_client().table("app_notifications").select("*").eq(
        "owner_firebase_uid", current_user["uid"]
    ).order("created_at", desc=True).limit(limit).execute().data or []


@router.post("/notifications/{notification_id}/read", status_code=204)
def mark_notification_read(notification_id: UUID, current_user: CurrentUser) -> None:
    get_supabase_client().table("app_notifications").update(
        {"read_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", str(notification_id)).eq(
        "owner_firebase_uid", current_user["uid"]
    ).execute()
