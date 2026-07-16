import logging
import json
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger("stylestack.inspiration")

_CATEGORY_TERMS = {
    "shirt": {"shirt", "top", "blouse", "button"},
    "pants": {"pant", "trouser", "jean", "chino", "short"},
    "dress": {"dress", "gown"},
    "jacket": {"jacket", "coat", "blazer", "hoodie", "sweater"},
    "shoes": {"shoe", "sneaker", "boot", "sandal", "loafer"},
    "accessory": {"hat", "cap", "bag", "watch", "jewelry", "scarf"},
    "kurta": {"kurta", "kurti"}, "saree": {"saree", "sari"},
    "lehenga": {"lehenga"}, "sherwani": {"sherwani"},
    "salwar": {"salwar", "suit"}, "dhoti": {"dhoti"},
    "dupatta": {"dupatta", "scarf"}, "blouse": {"blouse"},
    "anarkali": {"anarkali"}, "ethnic_set": {"ethnic", "traditional"},
}


def _relevance_score(photo: dict[str, Any], items: list[dict[str, Any]]) -> tuple[float, int, int]:
    """Score only text metadata; this intentionally makes no AI/image call."""
    text = " ".join(
        str(photo.get(field) or "") for field in ("alt", "url")
    ).casefold()
    categories = [str(item.get("category") or item.get("ai_category") or "").casefold() for item in items]
    colors = [str(item.get("color") or item.get("ai_color") or "").casefold() for item in items]
    category_hits = sum(
        any(term in text for term in _CATEGORY_TERMS.get(category, {category}))
        for category in categories
    )
    color_hits = sum(color in text for color in colors if color and color != "multicolor")
    total_signals = len(categories) + sum(bool(color and color != "multicolor") for color in colors)
    matched_signals = category_hits + color_hits
    score = matched_signals / total_signals if total_signals else 0.0
    return score, category_hits, color_hits


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
    if not settings.pexels_api_key:
        logger.warning("outfit_inspiration_skipped reason=PEXELS_API_KEY_missing")
        return []
    if not items:
        logger.info("outfit_inspiration_skipped reason=no_selected_items")
        return []
    query = _query_for(items, occasion, profile)
    request_payload = {
        "query": query,
        "per_page": 4,
        "orientation": "portrait",
    }
    logger.info(
        "outfit_inspiration_request_payload=%s",
        json.dumps(request_payload, ensure_ascii=False),
    )
    try:
        response = httpx.get(
            f"{settings.pexels_base_url}/search",
            params={"query": query, "per_page": 4, "orientation": "portrait"},
            headers={"Authorization": settings.pexels_api_key},
            timeout=settings.pexels_request_timeout_seconds,
        )
        if response.is_error:
            logger.warning(
                "outfit_inspiration_response_failed status=%s body=%s",
                response.status_code,
                response.text[:2000].replace("\n", " "),
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
            score, category_hits, color_hits = _relevance_score(result, items)
            logger.info(
                "outfit_inspiration_candidate id=%s score=%.2f category_hits=%s color_hits=%s accepted=%s alt=%r",
                result["id"], score, category_hits, color_hits, score >= 0.5, result["alt"],
            )
            if score >= 0.5:
                results.append((score, result))
        results = [result for _, result in sorted(results, key=lambda pair: pair[0], reverse=True)[:2]]
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
