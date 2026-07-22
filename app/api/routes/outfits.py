from datetime import datetime, timezone
import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from app.core.supabase import get_supabase_client
from app.dependencies.auth import CurrentUser
from pydantic import BaseModel, Field

from app.models.outfit import (
    OutfitFeedbackRequest,
    OutfitFeedbackResponse,
    OutfitResponse,
    OutfitSuggestionRequest,
    OutfitWearResponse,
)
from app.services.outfits import (
    create_outfit_suggestion,
    get_outfit,
    record_outfit_feedback,
)
from app.services.occasion import today_occasion

router = APIRouter()
logger = logging.getLogger("stylestack.outfits")


class OutfitChatRequest(BaseModel):
    message: str = Field(min_length=3, max_length=500)
    city: str | None = Field(default=None, min_length=2, max_length=120)


@router.post("/suggest", response_model=OutfitResponse, status_code=201)
def suggest_outfit(payload: OutfitSuggestionRequest, current_user: CurrentUser):
    client = get_supabase_client()
    if payload.calendar_event_id:
        event = client.table("calendar_events").select("id").eq(
            "id", str(payload.calendar_event_id)
        ).eq("owner_firebase_uid", current_user["uid"]).limit(1).execute().data
        if not event:
            raise HTTPException(status_code=404, detail="Calendar event not found")
    city = payload.city
    if not city:
        profile = client.table("profiles").select("city").eq(
            "firebase_uid", current_user["uid"]
        ).limit(1).execute().data
        city = profile[0].get("city") if profile else None
    if not city:
        raise HTTPException(status_code=422, detail="Set a city before requesting an outfit")
    occasion = payload.occasion
    if occasion.strip().lower() in {"daily", "today"}:
        occasion = today_occasion() or "casual everyday look"
    try:
        outfit = create_outfit_suggestion(current_user["uid"], city, occasion)
        if payload.calendar_event_id:
            client.table("calendar_events").update(
                {"outfit_id": str(outfit["id"])}
            ).eq("id", str(payload.calendar_event_id)).eq(
                "owner_firebase_uid", current_user["uid"]
            ).execute()
            logger.info(
                "calendar_event_outfit_linked uid=%s event_id=%s outfit_id=%s",
                current_user["uid"],
                payload.calendar_event_id,
                outfit["id"],
            )
        return outfit
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "outfit_generation_failed uid=%s error_type=%s",
            current_user["uid"],
            type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="Could not generate an outfit") from exc


@router.post("/chat", response_model=OutfitResponse, status_code=201)
def stylist_chat(payload: OutfitChatRequest, current_user: CurrentUser):
    """Turn a natural-language event request into the same saved outfit flow."""
    client = get_supabase_client()
    city = payload.city
    if not city:
        profile = client.table("profiles").select("city").eq(
            "firebase_uid", current_user["uid"]
        ).limit(1).execute().data
        city = profile[0].get("city") if profile else None
    if not city:
        raise HTTPException(status_code=422, detail="Set a city before asking your stylist")
    occasion = f"Stylist chat: {payload.message.strip()}"[:180]
    try:
        return create_outfit_suggestion(current_user["uid"], city, occasion)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "stylist_chat_failed uid=%s error_type=%s",
            current_user["uid"], type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="Could not prepare your stylist answer") from exc


@router.get("/{outfit_id}", response_model=OutfitResponse)
def read_outfit(outfit_id: UUID, current_user: CurrentUser):
    outfit = get_outfit(get_supabase_client(), str(outfit_id), current_user["uid"])
    if not outfit:
        raise HTTPException(status_code=404, detail="Outfit not found")
    return outfit


@router.post("/{outfit_id}/wear", response_model=OutfitWearResponse)
def wear_outfit(outfit_id: UUID, current_user: CurrentUser):
    client = get_supabase_client()
    outfit = get_outfit(client, str(outfit_id), current_user["uid"])
    if not outfit:
        raise HTTPException(status_code=404, detail="Outfit not found")
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {
            "wardrobe_item_id": str(item_id),
            "owner_firebase_uid": current_user["uid"],
            "worn_at": now,
            "notes": f"Outfit {outfit_id}",
        }
        for item_id in outfit["item_ids"]
    ]
    if rows:
        client.table("wear_logs").insert(rows).execute()
    try:
        record_outfit_feedback(
            client, current_user["uid"], str(outfit_id), "worn"
        )
    except Exception as exc:
        # Wear history is the source of truth; learning is additive and must
        # not make the user's logging action fail.
        logger.warning(
            "outfit_worn_feedback_failed uid=%s outfit_id=%s error_type=%s",
            current_user["uid"],
            outfit_id,
            type(exc).__name__,
        )
    return OutfitWearResponse(outfit_id=outfit_id, logged_items=len(rows))


@router.post("/{outfit_id}/feedback", response_model=OutfitFeedbackResponse)
def save_outfit_feedback(
    outfit_id: UUID,
    payload: OutfitFeedbackRequest,
    current_user: CurrentUser,
):
    client = get_supabase_client()
    owned = (
        client.table("outfits")
        .select("id")
        .eq("id", str(outfit_id))
        .eq("owner_firebase_uid", current_user["uid"])
        .limit(1)
        .execute()
        .data
    )
    if not owned:
        raise HTTPException(status_code=404, detail="Outfit not found")
    try:
        record_outfit_feedback(
            client,
            current_user["uid"],
            str(outfit_id),
            payload.signal,
            payload.reason,
        )
    except Exception as exc:
        logger.exception(
            "outfit_feedback_failed uid=%s outfit_id=%s signal=%s",
            current_user["uid"],
            outfit_id,
            payload.signal,
        )
        raise HTTPException(
            status_code=503, detail="Could not save outfit feedback"
        ) from exc
    return OutfitFeedbackResponse(outfit_id=outfit_id, signal=payload.signal)
