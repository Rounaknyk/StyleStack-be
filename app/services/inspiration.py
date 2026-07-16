import logging
import json
import re
from typing import Any

import httpx

from app.core.config import get_settings
from app.services.clip_relevance import score_if_enabled

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

# Pexels metadata is not a vision model, so be deliberately conservative. We
# only show references that look like a person wearing an outfit and reject
# common catalog/logo/object results before they reach the app.
_HUMAN_TERMS = {
    "person", "people", "man", "men", "woman", "women", "model", "wearing",
    "wear", "outfit", "fashion", "streetstyle", "street", "look", "dressed",
}
_REJECT_TERMS = {
    "logo", "icon", "illustration", "graphic", "banner", "mockup", "product",
    "catalog", "catalogue", "screenshot", "collage", "still life", "flat lay",
    "flatlay", "mannequin", "hanger", "costume rack", "store display",
}
_COLOR_TERMS = {
    "black", "white", "red", "blue", "green", "yellow", "purple", "pink",
    "brown", "grey", "gray", "orange", "beige", "cream", "maroon", "navy",
}


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.casefold()))


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.casefold())


def _term_present(words: set[str], term: str) -> bool:
    """Match metadata terms without false positives such as 'coat' in 'coating'."""
    term_words = _words(term)
    if len(term_words) != 1:
        return term_words.issubset(words)
    word = next(iter(term_words))
    return word in words or f"{word}s" in words or f"{word}es" in words


def _metadata_gate(photo: dict[str, Any]) -> tuple[bool, str]:
    alt = str(photo.get("alt") or "")
    url = str(photo.get("url") or "")
    words = _words(f"{alt} {url}")
    if any(_term_present(words, term) for term in _REJECT_TERMS):
        return False, "catalog_or_nonfashion_metadata"
    if not any(_term_present(words, term) for term in _HUMAN_TERMS):
        return False, "no_worn_person_metadata"
    return True, "human_fashion_metadata"


def _category_color_relation(
    text: str,
    category_terms: set[str],
    color: str,
    all_category_terms: set[str],
) -> str:
    """Return match, conflict, or unknown for a garment's color metadata."""
    tokens = _tokens(text)
    category_words = {
        variant
        for term in all_category_terms
        for variant in (term, f"{term}s", f"{term}es")
    }
    target_words = {
        variant
        for term in category_terms
        for variant in (term, f"{term}s", f"{term}es")
    }
    category_positions = [index for index, token in enumerate(tokens) if token in category_words]
    target_positions = [index for index, token in enumerate(tokens) if token in target_words]
    expected_color = next(iter(_words(color)), "")
    color_positions = [index for index, token in enumerate(tokens) if token in _COLOR_TERMS]
    for color_index in color_positions:
        nearby = [
            category_index
            for category_index in category_positions
            if abs(category_index - color_index) <= 3
        ]
        if nearby and min(nearby, key=lambda index: abs(index - color_index)) in nearby:
            # Only the nearest garment token owns this color token. This keeps
            # "orange shirt and white pants" from being misread as a white
            # shirt merely because the words are close together.
            nearest = min(nearby, key=lambda index: abs(index - color_index))
            if nearest in target_positions and abs(nearest - color_index) <= 2:
                return "match" if tokens[color_index] == expected_color else "conflict"
    return "unknown"


def _relevance_score(photo: dict[str, Any], items: list[dict[str, Any]]) -> tuple[float, int, int, int, int, int, int]:
    """Score only text metadata; this intentionally makes no AI/image call."""
    text = " ".join(
        str(photo.get(field) or "") for field in ("alt", "url")
    ).casefold()
    words = _words(text)
    categories = list(dict.fromkeys(
        str(item.get("category") or item.get("ai_category") or "").casefold()
        for item in items
        if str(item.get("category") or item.get("ai_category") or "").strip()
    ))
    colors = [str(item.get("color") or item.get("ai_color") or "").casefold() for item in items]
    category_hits = sum(any(_term_present(words, term) for term in _CATEGORY_TERMS.get(category, {category})) for category in categories)
    distinct_colors = list(dict.fromkeys(color for color in colors if color and color != "multicolor"))
    color_hits = sum(_term_present(words, color) for color in distinct_colors)
    category_coverage = category_hits / len(categories) if categories else 0.0
    color_coverage = color_hits / len(distinct_colors) if distinct_colors else 1.0
    # A color match must be attached to a matching garment in the same
    # metadata, not merely appear somewhere in the caption. This is purposely
    # strict: if Pexels does not describe the exact white shirt + white pants,
    # we prefer no reference over a visibly wrong one.
    item_matches = 0
    all_category_terms = set().union(*(_CATEGORY_TERMS.get(category, {category}) for category in categories)) if categories else set()
    for item in items:
        category = str(item.get("category") or item.get("ai_category") or "").casefold()
        color = str(item.get("color") or item.get("ai_color") or "").casefold()
        category_match = any(
            _term_present(words, term) for term in _CATEGORY_TERMS.get(category, {category})
        )
        relation = "unknown"
        if color and color != "multicolor":
            relation = _category_color_relation(
                text,
                _CATEGORY_TERMS.get(category, {category}),
                color,
                all_category_terms,
            )
        # Missing color metadata is tolerated so Pexels images are not all
        # discarded. An explicitly described conflicting color is rejected.
        color_match = not color or color == "multicolor" or relation != "conflict"
        if category_match and color_match:
            item_matches += 1
    # Category evidence matters more than color: a white shirt reference is
    # useful even when the metadata omits the color, but a matching color alone
    # must never make an unrelated image pass.
    score = (category_coverage * 0.75) + (color_coverage * 0.25)
    return score, category_hits, color_hits, len(categories), len(distinct_colors), item_matches, len(items)


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
        "per_page": settings.pexels_results_per_request,
        "orientation": "portrait",
    }
    logger.info(
        "outfit_inspiration_request_payload=%s",
        json.dumps(request_payload, ensure_ascii=False),
    )
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
            metadata_ok, gate_reason = _metadata_gate(result)
            score, category_hits, color_hits, category_total, color_total, item_matches, item_total = _relevance_score(result, items)
            # For multi-piece outfits require every distinct category in the
            # metadata. This prevents a shirt-only photo from representing a
            # shirt-and-pants recommendation. A single-piece outfit still
            # requires an explicit category match.
            category_coverage = category_hits / category_total if category_total else 0.0
            item_coverage = item_matches / item_total if item_total else 0.0
            accepted = (
                metadata_ok
                and category_coverage >= 1.0
                and item_coverage >= 1.0
                and score >= 0.75
            )
            clip_score = None
            if accepted and settings.inspiration_clip_enabled:
                try:
                    clip_score = score_if_enabled(result["image_url"], items, occasion)
                    accepted = clip_score is not None and clip_score >= settings.inspiration_clip_threshold
                    gate_reason = "clip_pass" if accepted else "clip_below_threshold"
                except Exception as exc:
                    # Do not show an unverified image when visual validation is
                    # explicitly enabled. This is intentionally fail-closed.
                    accepted = False
                    gate_reason = f"clip_failed:{type(exc).__name__}"
                    logger.warning(
                        "outfit_inspiration_clip_failed id=%s error_type=%s",
                        result["id"], type(exc).__name__,
                    )
            logger.info(
                "outfit_inspiration_candidate id=%s metadata_score=%.2f clip_score=%s category_hits=%s/%s item_color_hits=%s/%s color_hits=%s/%s gate=%s accepted=%s alt=%r",
                result["id"], score,
                f"{clip_score:.3f}" if clip_score is not None else "disabled",
                category_hits, category_total, item_matches, item_total, color_hits, color_total,
                gate_reason, accepted, result["alt"],
            )
            if accepted:
                results.append((score, result))
        # Return every accepted candidate from this one Pexels request. The
        # metadata/CLIP gates decide quality; there is no arbitrary two-image
        # truncation after scoring.
        results = [result for _, result in sorted(results, key=lambda pair: pair[0], reverse=True)]
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
