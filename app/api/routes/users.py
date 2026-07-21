import logging
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, status
from firebase_admin import messaging
from postgrest.exceptions import APIError
from pydantic import BaseModel, Field

from app.dependencies.auth import CurrentUser
from app.core.supabase import get_supabase_client
from app.models.user_preferences import (
    DeviceTokenRequest,
    TestNotificationResponse,
    UserPreferences,
    UserPreferencesUpdate,
)
from app.models.onboarding import (
    OnboardingCompleteRequest,
    OnboardingProfileResponse,
)
from app.services.account_deletion import AccountDeletionError, delete_user_account
from app.services.notifications import notification_scheduler
from app.services.push_notifications import (
    build_multicast_message,
    sync_broadcast_topic,
)
from app.services.timezones import normalize_timezone_name, resolve_timezone
from app.services.wardrobe import ensure_profile

router = APIRouter()
logger = logging.getLogger("stylestack.users")

ONBOARDING_PROFILE_FIELDS = (
    "display_name,gender_identity,date_of_birth,body_type,height_cm,"
    "style_preferences,shopping_frequency,onboarding_goals,"
    "onboarding_completed,onboarding_completed_at,onboarding_version"
)
ONBOARDING_SCHEMA_COLUMNS = frozenset(
    {
        "gender_identity",
        "date_of_birth",
        "body_type",
        "height_cm",
        "style_preferences",
        "shopping_frequency",
        "onboarding_goals",
        "onboarding_completed",
        "onboarding_completed_at",
        "onboarding_version",
    }
)
ONBOARDING_SCHEMA_ERROR_DETAIL = (
    "Onboarding is temporarily unavailable because the Supabase database schema "
    "is out of date. Apply the latest StyleStack schema migration, then try again."
)


def _execute_onboarding_query(query):
    """Execute an onboarding query with a clear stale-schema response."""
    try:
        return query.execute()
    except APIError as exc:
        message = str(exc.message or "").lower()
        is_missing_column = exc.code in {"42703", "PGRST204"}
        is_onboarding_column = any(
            column in message for column in ONBOARDING_SCHEMA_COLUMNS
        )
        if is_missing_column and is_onboarding_column:
            logger.error(
                "onboarding_schema_outdated code=%s message=%s",
                exc.code,
                exc.message,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=ONBOARDING_SCHEMA_ERROR_DETAIL,
            ) from exc
        raise


class CurrentUserResponse(BaseModel):
    user_id: str


class SimulationResponse(BaseModel):
    kind: str
    notifications_sent: int
    outfit_ids: list[str] = Field(default_factory=list)
    detail: str


@router.get("/me", response_model=CurrentUserResponse)
def read_current_user(current_user: CurrentUser) -> CurrentUserResponse:
    """Return the Firebase UID represented by the caller's ID token."""
    return CurrentUserResponse(user_id=current_user["uid"])


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
def delete_current_user(current_user: CurrentUser) -> None:
    """Permanently delete all cloud data and authentication for the caller."""
    try:
        delete_user_account(get_supabase_client(), current_user["uid"])
    except AccountDeletionError as exc:
        logger.error(
            "account_deletion_incomplete uid=%s stage=%s",
            current_user["uid"],
            exc.stage,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "Account deletion could not be completed safely. "
                "Your account remains available so you can try again."
            ),
        ) from exc


@router.get("/me/onboarding", response_model=OnboardingProfileResponse)
def read_onboarding(current_user: CurrentUser) -> dict:
    """Return onboarding state, creating the Firebase-backed profile if needed."""
    client = get_supabase_client()
    ensure_profile(client, current_user)
    query = (
        client.table("profiles")
        .select(ONBOARDING_PROFILE_FIELDS)
        .eq("firebase_uid", current_user["uid"])
        .limit(1)
    )
    rows = _execute_onboarding_query(query).data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Profile not found")
    return rows[0]


@router.put("/me/onboarding", response_model=OnboardingProfileResponse)
def complete_onboarding(
    payload: OnboardingCompleteRequest, current_user: CurrentUser
) -> dict:
    """Persist the completed onboarding questionnaire for the signed-in user."""
    client = get_supabase_client()
    ensure_profile(client, current_user)
    updates = payload.model_dump(mode="json")
    updates.update(
        {
            "onboarding_completed": True,
            "onboarding_completed_at": datetime.now(timezone.utc).isoformat(),
            "onboarding_version": 1,
        }
    )
    query = (
        client.table("profiles")
        .update(updates)
        .eq("firebase_uid", current_user["uid"])
    )
    rows = _execute_onboarding_query(query).data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Profile not found")
    return rows[0]


@router.get("/me/preferences", response_model=UserPreferences)
def read_preferences(current_user: CurrentUser):
    response = get_supabase_client().table("profiles").select(
        "city,timezone,notification_enabled,notification_time"
    ).eq("firebase_uid", current_user["uid"]).limit(1).execute()
    if not response.data:
        raise HTTPException(status_code=404, detail="Profile not found")
    return response.data[0]


@router.put("/me/preferences", response_model=UserPreferences)
def update_preferences(payload: UserPreferencesUpdate, current_user: CurrentUser):
    updates = payload.model_dump(exclude_unset=True, mode="json")
    if not updates:
        raise HTTPException(status_code=422, detail="At least one preference is required")
    if "timezone" in updates:
        updates["timezone"] = normalize_timezone_name(updates["timezone"])
    response = get_supabase_client().table("profiles").update(updates).eq(
        "firebase_uid", current_user["uid"]
    ).execute()
    if not response.data:
        raise HTTPException(status_code=404, detail="Profile not found")
    if "notification_enabled" in updates:
        tokens = (
            get_supabase_client()
            .table("device_tokens")
            .select("token")
            .eq("owner_firebase_uid", current_user["uid"])
            .execute()
            .data
            or []
        )
        try:
            sync_broadcast_topic(
                [row["token"] for row in tokens],
                subscribed=bool(updates["notification_enabled"]),
            )
        except Exception:
            logger.exception(
                "broadcast_topic_preference_sync_failed uid=%s",
                current_user["uid"],
            )
    return response.data[0]


@router.post("/me/devices", status_code=204)
def register_device(payload: DeviceTokenRequest, current_user: CurrentUser) -> None:
    client = get_supabase_client()
    client.table("device_tokens").upsert(
        {
            "owner_firebase_uid": current_user["uid"],
            "token": payload.token,
            "platform": payload.platform,
        },
        on_conflict="token",
    ).execute()
    profile = (
        client.table("profiles")
        .select("notification_enabled")
        .eq("firebase_uid", current_user["uid"])
        .limit(1)
        .execute()
        .data
        or []
    )
    if profile and profile[0].get("notification_enabled"):
        try:
            sync_broadcast_topic([payload.token], subscribed=True)
        except Exception:
            logger.exception(
                "broadcast_topic_device_subscribe_failed uid=%s",
                current_user["uid"],
            )


@router.delete("/me/devices", status_code=204)
def unregister_device(payload: DeviceTokenRequest, current_user: CurrentUser) -> None:
    try:
        sync_broadcast_topic([payload.token], subscribed=False)
    except Exception:
        logger.exception(
            "broadcast_topic_device_unsubscribe_failed uid=%s",
            current_user["uid"],
        )
    get_supabase_client().table("device_tokens").delete().eq(
        "owner_firebase_uid", current_user["uid"]
    ).eq("token", payload.token).execute()


@router.post("/me/test-notification", response_model=TestNotificationResponse)
def send_test_notification(current_user: CurrentUser) -> TestNotificationResponse:
    tokens = get_supabase_client().table("device_tokens").select("token").eq(
        "owner_firebase_uid", current_user["uid"]
    ).execute().data or []
    if not tokens:
        raise HTTPException(
            status_code=422,
            detail="No notification device is registered. Enable notifications on a physical device first.",
        )
    message = build_multicast_message(
        title="StyleStack test notification",
        body="Push notifications are configured correctly 🎉",
        data={"type": "test_notification", "destination": "today"},
        tokens=[row["token"] for row in tokens],
    )
    try:
        response = messaging.send_each_for_multicast(message)
        return TestNotificationResponse(
            success_count=response.success_count,
            failure_count=response.failure_count,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail="Firebase could not send the test notification. Check APNs and Cloud Messaging configuration.",
        ) from exc


def _simulation_profile(uid: str) -> tuple[object, dict, datetime]:
    client = get_supabase_client()
    rows = client.table("profiles").select(
        "firebase_uid,city,timezone,notification_time,last_notification_date"
    ).eq("firebase_uid", uid).limit(1).execute().data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Profile not found")
    profile = rows[0]
    normalized_timezone = normalize_timezone_name(profile.get("timezone"))
    if normalized_timezone != profile.get("timezone"):
        client.table("profiles").update({"timezone": normalized_timezone}).eq(
            "firebase_uid", uid
        ).execute()
        profile["timezone"] = normalized_timezone
    now = datetime.now(resolve_timezone(normalized_timezone))
    return client, profile, now


@router.post("/me/simulations/daily-outfit", response_model=SimulationResponse)
def simulate_daily_outfit(current_user: CurrentUser) -> SimulationResponse:
    """Run the same daily-outfit delivery function used by the 8 AM scheduler."""
    client, profile, now = _simulation_profile(current_user["uid"])
    try:
        outfit_id = notification_scheduler.process_daily_outfit(client, profile, now)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Daily outfit simulation failed") from exc
    return SimulationResponse(
        kind="daily_outfit",
        notifications_sent=1,
        outfit_ids=[outfit_id],
        detail="The production 8 AM outfit flow ran successfully.",
    )


@router.post("/me/simulations/daily-outfit-delay", response_model=SimulationResponse)
def simulate_delayed_daily_outfit(current_user: CurrentUser) -> SimulationResponse:
    """Schedule the same daily notification flow ten seconds from now."""
    client, profile, _ = _simulation_profile(current_user["uid"])
    notification_scheduler.schedule_daily_outfit_test(client, profile, 10)
    return SimulationResponse(
        kind="daily_outfit_delayed",
        notifications_sent=0,
        detail="The production 8 AM outfit flow is scheduled for 10 seconds from now.",
    )


@router.post("/me/simulations/tomorrow-events", response_model=SimulationResponse)
def simulate_tomorrow_events(current_user: CurrentUser) -> SimulationResponse:
    """Run the same tomorrow-event delivery function used by the scheduler."""
    client, profile, now = _simulation_profile(current_user["uid"])
    try:
        event_ids = notification_scheduler.process_event_reminders(
            client, profile, now, force=True
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Event reminder simulation failed") from exc
    return SimulationResponse(
        kind="tomorrow_events",
        notifications_sent=len(event_ids),
        detail=(
            f"Sent {len(event_ids)} tomorrow-event reminder(s)."
            if event_ids
            else "No calendar events were found for tomorrow."
        ),
    )
