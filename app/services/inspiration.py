import logging
import json
import re
import time
from threading import Lock
from typing import Any

import httpx

from app.core.config import get_settings
from app.services.clip_relevance import score_if_enabled

logger = logging.getLogger("stylestack.inspiration")
_cache_lock = Lock()
_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

_CATEGORY_TERMS = {
    "shirt": {"shirt", "top", "blouse", "button"}, "pants": {"pant", "trouser", "jean", "chino", "short"},
    "dress": {"dress", "gown"}, "jacket": {"jacket", "coat", "blazer", "hoodie", "sweater"},
    "shoes": {"shoe", "sneaker", "boot", "sandal", "loafer"}, "accessory": {"hat", "cap", "bag", "watch", "jewelry", "scarf"},
    "kurta": {"kurta", "kurti"}, "saree": {"saree", "sari"}, "lehenga": {"lehenga"}, "sherwani": {"sherwani"},
    "salwar": {"salwar", "suit"}, "dhoti": {"dhoti"}, "dupatta": {"dupatta", "scarf"}, "blouse": {"blouse"},
    "anarkali": {"anarkali"}, "ethnic_set": {"ethnic", "traditional"},
}
_HUMAN_TERMS = {"person", "people", "man", "men", "woman", "women", "model", "wearing", "wear", "outfit", "fashion", "street", "look", "dressed"}
_REJECT_TERMS = {"logo", "icon", "illustration", "graphic", "banner", "mockup", "product", "catalog", "catalogue", "screenshot", "collage", "flatlay", "mannequin", "hanger", "store display"}


def _metadata_score(photo: dict[str, Any], items: list[dict[str, Any]]) -> tuple[float, str]:
    text = " ".join(str(photo.get(field) or "") for field in ("alt", "url")).casefold()
    words = set(re.findall(r"[a-z0-9]+", text))
    if any(term in text for term in _REJECT_TERMS):
        return 0.0, "rejected_catalog_metadata"
    human = 1.0 if any(term in words for term in _HUMAN_TERMS) else 0.0
    categories = list(dict.fromkeys(str(item.get("category") or item.get("ai_category") or "").casefold() for item in items))
    colors = list(dict.fromkeys(str(item.get("color") or item.get("ai_color") or "").casefold() for item in items if str(item.get("color") or item.get("ai_color") or "").casefold() not in {"", "multicolor"}))
    category_hits = sum(any(term in words or f"{term}s" in words for term in _CATEGORY_TERMS.get(category, {category})) for category in categories)
    color_hits = sum(color in words for color in colors)
    pair_hits = 0
    for item in items:
        category = str(item.get("category") or item.get("ai_category") or "").casefold()
        color = str(item.get("color") or item.get("ai_color") or "").casefold()
        if not color or color == "multicolor":
            pair_hits += 1
            continue
        category_words = _CATEGORY_TERMS.get(category, {category})
        if any(re.search(rf"\b{re.escape(color)}\b(?:\s+\w+){{0,2}}\s+\b{re.escape(term)}s?\b", text) for term in category_words):
            pair_hits += 1
    category_coverage = category_hits / len(categories) if categories else 0.0
    color_coverage = color_hits / len(colors) if colors else 1.0
    pair_coverage = pair_hits / len(items) if items else 0.0
    score = (category_coverage * 0.45) + (color_coverage * 0.20) + (pair_coverage * 0.20) + (human * 0.15)
    if human == 0 or category_coverage < 0.5 or pair_coverage < 1.0 or score < 0.60:
        return score, "below_metadata_threshold"
    return score, "metadata_pass"

def _query_for(items: list[dict[str, Any]], occasion: str, profile: dict[str, Any] | None) -> str:
    categories = [str(item.get("category") or item.get("ai_category") or "") for item in items]
    colors = [str(item.get("color") or item.get("ai_color") or "") for item in items]
    ethnic = any(category.casefold() in {
        "kurta", "saree", "lehenga", "sherwani", "salwar", "dhoti",
        "dupatta", "blouse", "anarkali", "ethnic_set",
    } for category in categories)
    gender = str((profile or {}).get("gender_identity") or "").casefold()
    gender_term = "men's" if gender in {"man", "male", "men"} else "women's" if gender in {"woman", "female", "women"} else "unisex"
    garment_terms = " ".join(dict.fromkeys(category for category in categories if category))
    color_terms = " ".join(dict.fromkeys(color for color in colors if color))
    culture = "Indian ethnic fashion" if ethnic else "fashion street style"
    return f"{gender_term} {culture} {garment_terms} {color_terms} {occasion} outfit inspiration".strip()


def fetch_outfit_inspiration(
    items: list[dict[str, Any]], occasion: str, profile: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Fetch optional visual references; never make outfit generation fail."""
    settings = get_settings()
    if not settings.pexels_inspiration_enabled:
        logger.info("outfit_inspiration_skipped reason=feature_disabled")
        return []
    if not settings.pexels_api_key:
        logger.warning("outfit_inspiration_skipped reason=PEXELS_API_KEY_missing")
        return []
    if not items:
        logger.info("outfit_inspiration_skipped reason=no_selected_items")
        return []
    query = _query_for(items, occasion, profile)
    request_payload = {
        "query": query,
        "per_page": settings.pexels_results_per_request,
        "orientation": "portrait",
    }
    logger.info(
        "outfit_inspiration_request_payload=%s",
        json.dumps(request_payload, ensure_ascii=False),
    )
    cache_key = json.dumps(request_payload, sort_keys=True)
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached and cached[0] > now:
            logger.info("outfit_inspiration_cache_hit")
            return [dict(item) for item in cached[1]]
    try:
        response = httpx.get(
            f"{settings.pexels_base_url}/search",
            params={
                "query": query,
                "per_page": settings.pexels_results_per_request,
                "orientation": "portrait",
            },
            headers={"Authorization": settings.pexels_api_key},
            timeout=settings.pexels_request_timeout_seconds,
        )
        if response.is_error:
            logger.warning(
                "outfit_inspiration_response_failed status=%s body=%s",
                response.status_code,
                response.text[:500].replace("\n", " "),
            )
            response.raise_for_status()
        photos = response.json().get("photos", [])
        results = []
        for photo in photos:
            result = {
                "id": photo.get("id"),
                "url": photo.get("url"),
                "image_url": (photo.get("src") or {}).get("original")
                or (photo.get("src") or {}).get("large")
                or (photo.get("src") or {}).get("medium"),
                "alt": photo.get("alt") or "Style inspiration",
                "photographer": photo.get("photographer") or "Pexels creator",
            }
            if not ((photo.get("src") or {}).get("original")
            or (photo.get("src") or {}).get("large")
            or (photo.get("src") or {}).get("medium")):
                continue
            clip_score = None
            metadata_score = 0.0
            if settings.inspiration_clip_enabled:
                try:
                    clip_score = score_if_enabled(result["image_url"], items, occasion)
                    accepted = clip_score is not None and clip_score >= settings.inspiration_clip_threshold
                    gate_reason = "clip_pass" if accepted else "clip_below_threshold"
                except Exception as exc:
                    accepted = False
                    gate_reason = f"clip_failed:{type(exc).__name__}"
                    logger.warning("outfit_inspiration_clip_failed id=%s error_type=%s", result["id"], type(exc).__name__)
            else:
                metadata_score, gate_reason = _metadata_score(result, items)
                accepted = metadata_score >= 0.60
            logger.info(
                "outfit_inspiration_candidate id=%s clip_score=%s metadata_gate=%s accepted=%s",
                result["id"],
                f"{clip_score:.3f}" if clip_score is not None else "disabled",
                gate_reason, accepted,
            )
            if accepted:
                results.append((clip_score if clip_score is not None else metadata_score, result))
        # Return every accepted candidate from this one Pexels request. The
        # The active CLIP or metadata gate decides quality; there is no
        # arbitrary two-image truncation.
        results = [result for _, result in sorted(results, key=lambda pair: pair[0], reverse=True)]
        with _cache_lock:
            _cache[cache_key] = (
                now + settings.free_pilot_inspiration_cache_seconds,
                [dict(result) for result in results],
            )
        logger.info(
            "outfit_inspiration_response_ok status=%s photos=%s usable=%s",
            response.status_code, len(photos), len(results),
        )
        for result in results:
            logger.info(
                "outfit_inspiration_image id=%s image_url=%s photographer=%s",
                result.get("id"), result.get("image_url"), result.get("photographer"),
            )
        return results
    except Exception as exc:
        logger.warning(
            "outfit_inspiration_failed query=%r error_type=%s",
            query, type(exc).__name__,
        )
        return []
