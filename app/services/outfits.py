import json
import logging
from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.supabase import get_supabase_client
from app.models.outfit import WeatherResponse
from app.prompts.outfit_stylist import build_stylist_prompt
from app.services.wardrobe import add_signed_image_url
from app.services.weather import get_current_weather
from app.services.inspiration import fetch_outfit_inspiration
from app.services.groq_rate_limit import groq_rate_gate

logger = logging.getLogger("stylestack.outfits")


class StylingResult(BaseModel):
    item_ids: list[str] = Field(min_length=1, max_length=6)
    reasoning: str = Field(min_length=1, max_length=500)


def _call_stylist(
    items: list[dict[str, Any]],
    weather: WeatherResponse,
    occasion: str,
    style_profile: dict[str, Any] | None = None,
) -> StylingResult:
    settings = get_settings()
    if not settings.groq_api_key:
        raise RuntimeError("GROQ_API_KEY is not configured")
    compact_items = [
        {
            "id": item["id"],
            "name": item["name"],
            "category": item.get("category") or item.get("ai_category"),
            "color": item.get("color") or item.get("ai_color"),
            "season": item.get("season") or item.get("ai_season"),
            "formality": item.get("formality") or item.get("ai_formality"),
            "description": item.get("description") or item.get("ai_description"),
        }
        for item in items
    ]
    prompt = build_stylist_prompt(
        wardrobe_json=json.dumps(compact_items),
        weather_json=weather.model_dump_json(),
        occasion=occasion,
        profile_json=json.dumps(style_profile or {}),
    )
    response = groq_rate_gate.post(
        headers={"Authorization": f"Bearer {settings.groq_api_key}"},
        payload={
            "model": settings.groq_vision_model,
            "reasoning_effort": "none",
            "messages": [
                {
                    "role": "system",
                    "content": "You are StyleStack's style-first outfit intelligence.",
                },
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.7,
            "max_completion_tokens": 400,
        },
        timeout=settings.groq_request_timeout_seconds,
    )
    return StylingResult.model_validate_json(
        response.json()["choices"][0]["message"]["content"]
    )


def _age_group(date_of_birth: str | None, today: date | None = None) -> str | None:
    if not date_of_birth:
        return None
    try:
        born = date.fromisoformat(date_of_birth)
    except (TypeError, ValueError):
        return None
    current = today or date.today()
    age = current.year - born.year - (
        (current.month, current.day) < (born.month, born.day)
    )
    if age < 13:
        return "under_13"
    if age < 20:
        return "teen"
    if age < 30:
        return "20s"
    if age < 40:
        return "30s"
    if age < 50:
        return "40s"
    return "50s_plus"


def build_personal_style_context(profile: dict[str, Any] | None) -> dict[str, Any]:
    """Expose useful onboarding signals without sending DOB or empty answers."""
    if not profile:
        return {"discovery_mode": True}
    styles = [
        value
        for value in (profile.get("style_preferences") or [])
        if value not in {"not_sure", "explore"}
    ]
    result: dict[str, Any] = {
        "discovery_mode": not styles,
        "preferred_styles": styles,
        "goals": profile.get("onboarding_goals") or [],
    }
    gender = profile.get("gender_identity")
    if gender and gender != "prefer_not_to_say":
        result["gender_identity"] = gender
    body_type = profile.get("body_type")
    if body_type and body_type != "not_sure":
        result["body_type"] = body_type
    if profile.get("height_cm"):
        result["height_cm"] = profile["height_cm"]
    age_group = _age_group(profile.get("date_of_birth"))
    if age_group:
        result["age_group"] = age_group
    return result


def create_outfit_suggestion(
    uid: str, city: str, occasion: str = "daily"
) -> dict[str, Any]:
    client = get_supabase_client()
    weather = get_current_weather(city)
    items_response = (
        client.table("wardrobe_items")
        .select("*")
        .eq("owner_firebase_uid", uid)
        .execute()
    )
    items = items_response.data or []
    if not items:
        raise ValueError("Add wardrobe items before requesting an outfit")

    worn_response = (
        client.table("wear_logs")
        .select("wardrobe_item_id,worn_at")
        .eq("owner_firebase_uid", uid)
        .order("worn_at", desc=True)
        .limit(50)
        .execute()
    )
    recently_worn = {
        str(row["wardrobe_item_id"]) for row in (worn_response.data or [])[:10]
    }
    candidates = [item for item in items if str(item["id"]) not in recently_worn]
    if not candidates:
        candidates = items

    profile_rows = (
        client.table("profiles")
        .select(
            "gender_identity,date_of_birth,body_type,height_cm,"
            "style_preferences,onboarding_goals"
        )
        .eq("firebase_uid", uid)
        .limit(1)
        .execute()
        .data
        or []
    )
    style_profile = build_personal_style_context(
        profile_rows[0] if profile_rows else None
    )
    styling = _call_stylist(candidates, weather, occasion, style_profile)
    valid_ids = {str(item["id"]) for item in candidates}
    selected_ids = list(dict.fromkeys(i for i in styling.item_ids if i in valid_ids))
    if not selected_ids:
        raise RuntimeError("Stylist returned no valid wardrobe item IDs")

    outfit_response = client.table("outfits").insert(
        {
            "owner_firebase_uid": uid,
            "occasion": occasion,
            "reasoning": styling.reasoning,
            "weather": weather.model_dump(),
        }
    ).execute()
    outfit = outfit_response.data[0]
    client.table("outfit_items").insert(
        [
            {"outfit_id": outfit["id"], "wardrobe_item_id": item_id, "position": index}
            for index, item_id in enumerate(selected_ids)
        ]
    ).execute()
    items_by_id = {str(item["id"]): item for item in items}
    selected = [items_by_id[item_id] for item_id in selected_ids if item_id in items_by_id]
    outfit["item_ids"] = selected_ids
    outfit["items"] = [add_signed_image_url(client, item) for item in selected]
    outfit["inspiration_images"] = fetch_outfit_inspiration(selected, occasion, style_profile)
    logger.info("outfit_created uid=%s outfit_id=%s items=%s", uid, outfit["id"], len(selected_ids))
    return outfit


def get_outfit(client: Any, outfit_id: str, uid: str) -> dict[str, Any] | None:
    response = client.table("outfits").select("*").eq("id", outfit_id).eq(
        "owner_firebase_uid", uid
    ).limit(1).execute()
    if not response.data:
        return None
    outfit = response.data[0]
    links = client.table("outfit_items").select("wardrobe_item_id,position").eq(
        "outfit_id", outfit_id
    ).order("position").execute().data or []
    ids = [str(link["wardrobe_item_id"]) for link in links]
    items = []
    if ids:
        items = client.table("wardrobe_items").select("*").in_("id", ids).execute().data or []
    by_id = {str(item["id"]): add_signed_image_url(client, item) for item in items}
    outfit["item_ids"] = ids
    outfit["items"] = [by_id[item_id] for item_id in ids if item_id in by_id]
    return outfit
