import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.supabase import get_supabase_client
from app.models.outfit import WeatherResponse
from app.prompts.outfit_stylist import build_stylist_ranking_prompt
from app.services.wardrobe import add_signed_image_url
from app.services.weather import get_current_weather
from app.services.inspiration import fetch_outfit_inspiration
from app.services.groq_rate_limit import groq_rate_gate
from app.services.stylist_engine import (
    OutfitCandidate,
    fallback_reasoning,
    generate_outfit_candidates,
    validate_candidate,
)

logger = logging.getLogger("stylestack.outfits")

RECENT_CLOTHING_COOLDOWN = timedelta(days=3)
REPEATABLE_ACCESSORY_CATEGORIES = {
    "accessory",
    "accessories",
    "bag",
    "bags",
    "backpack",
    "backpacks",
    "belt",
    "belts",
    "boot",
    "boots",
    "cap",
    "caps",
    "eyewear",
    "footwear",
    "handbag",
    "handbags",
    "hat",
    "hats",
    "jewellery",
    "jewelry",
    "sandal",
    "sandals",
    "scarf",
    "scarves",
    "shoe",
    "shoes",
    "slipper",
    "slippers",
    "sneaker",
    "sneakers",
    "sunglasses",
    "wallet",
    "wallets",
    "watch",
    "watches",
}


class StylingResult(BaseModel):
    candidate_id: str = Field(pattern=r"^C[1-9][0-9]*$")
    reasoning: str = Field(min_length=1, max_length=500)


def _item_category(item: dict[str, Any]) -> str:
    category = str(item.get("category") or "").strip()
    ai_category = str(item.get("ai_category") or "").strip()
    if category.casefold() in {"", "other", "unknown"} and ai_category:
        return ai_category
    return category or ai_category


def _call_stylist(
    candidates: list[OutfitCandidate],
    weather: WeatherResponse,
    occasion: str,
    style_profile: dict[str, Any] | None = None,
    learned_preferences: dict[str, Any] | None = None,
) -> StylingResult:
    settings = get_settings()
    if not settings.groq_api_key:
        raise RuntimeError("GROQ_API_KEY is not configured")
    prompt = build_stylist_ranking_prompt(
        candidates_json=json.dumps(
            [candidate.prompt_payload() for candidate in candidates]
        ),
        weather_json=weather.model_dump_json(),
        occasion=occasion,
        profile_json=json.dumps(style_profile or {}),
        learned_preferences_json=json.dumps(learned_preferences or {}),
    )
    response = groq_rate_gate.post(
        headers={"Authorization": f"Bearer {settings.groq_api_key}"},
        payload={
            "model": settings.groq_stylist_model,
            "reasoning_effort": "none",
            "messages": [
                {
                    "role": "system",
                    "content": "You are StyleStack's style-first outfit intelligence.",
                },
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_completion_tokens": 450,
        },
        timeout=settings.groq_request_timeout_seconds,
    )
    return StylingResult.model_validate_json(
        response.json()["choices"][0]["message"]["content"]
    )


FEEDBACK_WEIGHTS = {
    "worn": 1.0,
    "liked": 0.75,
    "refreshed": -0.25,
    "wore_something_else": -0.8,
    "disliked": -1.0,
}


def load_item_affinity(
    client: Any, uid: str
) -> tuple[dict[str, float], dict[str, Any]]:
    """Summarize stored feedback locally without spending an AI request."""
    try:
        feedback = (
            client.table("outfit_feedback")
            .select("outfit_id,signal,created_at")
            .eq("owner_firebase_uid", uid)
            .order("created_at", desc=True)
            .limit(100)
            .execute()
            .data
            or []
        )
        outfit_ids = list({str(row["outfit_id"]) for row in feedback})
        if not outfit_ids:
            return {}, {"signals_seen": 0}
        links = (
            client.table("outfit_items")
            .select("outfit_id,wardrobe_item_id")
            .in_("outfit_id", outfit_ids)
            .execute()
            .data
            or []
        )
    except Exception as exc:
        # The migration may not have been applied yet. Suggestions must remain
        # usable, so learning degrades safely to a neutral profile.
        logger.warning(
            "outfit_feedback_unavailable error_type=%s", type(exc).__name__
        )
        return {}, {"signals_seen": 0}

    weights_by_outfit: dict[str, float] = {}
    for row in feedback:
        outfit_id = str(row["outfit_id"])
        weights_by_outfit[outfit_id] = max(
            -1.0,
            min(
                1.0,
                weights_by_outfit.get(outfit_id, 0.0)
                + FEEDBACK_WEIGHTS.get(str(row.get("signal")), 0.0),
            ),
        )
    affinity: dict[str, float] = {}
    counts: dict[str, int] = {}
    for link in links:
        item_id = str(link["wardrobe_item_id"])
        affinity[item_id] = affinity.get(item_id, 0.0) + weights_by_outfit.get(
            str(link["outfit_id"]), 0.0
        )
        counts[item_id] = counts.get(item_id, 0) + 1
    for item_id, total in list(affinity.items()):
        affinity[item_id] = max(-1.0, min(1.0, total / counts[item_id]))
    positives = sum(
        1
        for row in feedback
        if FEEDBACK_WEIGHTS.get(str(row.get("signal")), 0) > 0
    )
    negatives = len(feedback) - positives
    return affinity, {
        "signals_seen": len(feedback),
        "positive_signals": positives,
        "negative_signals": negatives,
    }


def record_outfit_feedback(
    client: Any,
    uid: str,
    outfit_id: str,
    signal: str,
    reason: str | None = None,
) -> None:
    if signal not in FEEDBACK_WEIGHTS:
        raise ValueError("Unsupported outfit feedback signal")
    if signal in {"liked", "disliked"}:
        opposite = "disliked" if signal == "liked" else "liked"
        client.table("outfit_feedback").delete().eq(
            "owner_firebase_uid", uid
        ).eq("outfit_id", outfit_id).eq("signal", opposite).execute()
    client.table("outfit_feedback").upsert(
        {
            "owner_firebase_uid": uid,
            "outfit_id": outfit_id,
            "signal": signal,
            "reason": reason,
        },
        on_conflict="owner_firebase_uid,outfit_id,signal",
    ).execute()


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


def _is_repeatable_accessory(item: dict[str, Any]) -> bool:
    category = _item_category(item).casefold()
    if category in REPEATABLE_ACCESSORY_CATEGORIES:
        return True
    descriptive_text = " ".join(
        str(item.get(field) or "")
        for field in ("name", "subcategory", "ai_description")
    )
    tokens = {
        token
        for token in "".join(
            character if character.isalnum() else " "
            for character in descriptive_text.casefold()
        ).split()
        if token
    }
    return bool(tokens & REPEATABLE_ACCESSORY_CATEGORIES)


def _parse_worn_at(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def filter_recently_worn_clothing(
    items: list[dict[str, Any]],
    wear_logs: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Exclude garments worn during the rolling three-day cooldown.

    Accessories intentionally remain reusable because they commonly complete
    several consecutive looks. The returned ID set is useful for observability
    and tests without exposing wear history to the stylist model.
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    cutoff = current - RECENT_CLOTHING_COOLDOWN
    items_by_id = {str(item["id"]): item for item in items}
    excluded_ids: set[str] = set()
    for row in wear_logs:
        item_id = str(row.get("wardrobe_item_id") or "")
        item = items_by_id.get(item_id)
        worn_at = _parse_worn_at(row.get("worn_at"))
        if (
            item is not None
            and worn_at is not None
            and worn_at >= cutoff
            and not _is_repeatable_accessory(item)
        ):
            excluded_ids.add(item_id)
    return (
        [item for item in items if str(item["id"]) not in excluded_ids],
        excluded_ids,
    )


def create_outfit_suggestion(
    uid: str, city: str, occasion: str = "daily"
) -> dict[str, Any]:
    client = get_supabase_client()
    try:
        weather = get_current_weather(city)
    except Exception as exc:
        logger.warning(
            "weather_unavailable city=%s error_type=%s",
            city,
            type(exc).__name__,
        )
        weather = WeatherResponse(
            city=city,
            condition="Unavailable",
            description=(
                "Local weather is unavailable. Prioritize styling, occasion, "
                "and generally comfortable choices."
            ),
        )
    items_response = (
        client.table("wardrobe_items")
        .select("*")
        .eq("owner_firebase_uid", uid)
        .execute()
    )
    items = items_response.data or []
    if not items:
        raise ValueError("Add wardrobe items before requesting an outfit")

    now = datetime.now(timezone.utc)
    worn_response = (
        client.table("wear_logs")
        .select("wardrobe_item_id,worn_at")
        .eq("owner_firebase_uid", uid)
        .gte("worn_at", (now - RECENT_CLOTHING_COOLDOWN).isoformat())
        .order("worn_at", desc=True)
        .execute()
    )
    candidates, excluded_ids = filter_recently_worn_clothing(
        items,
        worn_response.data or [],
        now=now,
    )
    clothing_items = [item for item in items if not _is_repeatable_accessory(item)]
    clothing_candidates = [
        item for item in candidates if not _is_repeatable_accessory(item)
    ]
    if clothing_items and not clothing_candidates:
        raise ValueError(
            "All clothing in your wardrobe was worn within the last 3 days. "
            "Add another piece or try again after the cooldown."
        )
    logger.info(
        "outfit_recency_applied uid=%s cooldown_days=3 clothing_excluded=%s "
        "accessories_available=%s",
        uid,
        len(excluded_ids),
        sum(1 for item in candidates if _is_repeatable_accessory(item)),
    )

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
    item_affinity, learned_preferences = load_item_affinity(client, uid)
    outfit_candidates = generate_outfit_candidates(
        candidates,
        occasion,
        style_profile,
        item_affinity,
        limit=10,
    )
    if not outfit_candidates:
        raise ValueError(
            "Your wardrobe does not yet contain a compatible complete look. "
            "Add at least one top and bottom, or a complete one-piece outfit."
        )

    selected_candidate = outfit_candidates[0]
    reasoning = fallback_reasoning(selected_candidate, occasion)
    selection_source = "deterministic_fallback"
    try:
        styling = _call_stylist(
            outfit_candidates,
            weather,
            occasion,
            style_profile,
            learned_preferences,
        )
        by_candidate_id = {
            candidate.candidate_id: candidate for candidate in outfit_candidates
        }
        ranked_choice = by_candidate_id.get(styling.candidate_id)
        if ranked_choice is not None:
            valid, rejection_reason = validate_candidate(ranked_choice.garments)
            if valid:
                selected_candidate = ranked_choice
                reasoning = styling.reasoning
                selection_source = "ai_ranked"
            else:
                logger.warning(
                    "stylist_final_validation_failed uid=%s candidate=%s reason=%s",
                    uid,
                    styling.candidate_id,
                    rejection_reason,
                )
        else:
            logger.warning(
                "stylist_unknown_candidate uid=%s candidate=%s",
                uid,
                styling.candidate_id,
            )
    except Exception as exc:
        # Candidate generation is deterministic, so a provider outage should
        # reduce creative ranking quality rather than break Today's Outfit.
        logger.warning(
            "stylist_ai_ranking_unavailable uid=%s error_type=%s fallback=%s",
            uid,
            type(exc).__name__,
            selected_candidate.candidate_id,
        )

    selected_ids = selected_candidate.item_ids

    outfit_response = client.table("outfits").insert(
        {
            "owner_firebase_uid": uid,
            "occasion": occasion,
            "reasoning": reasoning,
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
    settings = get_settings()
    outfit["inspiration_enabled"] = bool(
        settings.pexels_inspiration_enabled and settings.pexels_api_key
    )
    outfit["inspiration_images"] = fetch_outfit_inspiration(
        selected, occasion, style_profile
    )
    logger.info(
        "outfit_created uid=%s outfit_id=%s items=%s source=%s candidate=%s local_score=%.1f",
        uid,
        outfit["id"],
        len(selected_ids),
        selection_source,
        selected_candidate.candidate_id,
        selected_candidate.score,
    )
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
    settings = get_settings()
    outfit["inspiration_enabled"] = bool(
        settings.pexels_inspiration_enabled and settings.pexels_api_key
    )
    outfit.setdefault("inspiration_images", [])
    return outfit
