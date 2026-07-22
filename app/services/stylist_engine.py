"""Deterministic candidate construction and scoring for outfit suggestions.

The language model is deliberately not responsible for basic outfit validity.
This module turns wardrobe metadata into garment roles, builds only wearable
formulas, rejects incoherent combinations, and ranks the survivors before the
model sees them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
import re
from typing import Any, Iterable


NEUTRALS = {"black", "white", "grey", "gray", "beige", "cream", "brown", "navy"}
COLOUR_PARTNERS = {
    "blue": {"white", "grey", "gray", "beige", "brown", "orange", "navy"},
    "red": {"white", "black", "beige", "grey", "gray", "navy"},
    "green": {"white", "black", "beige", "brown", "navy"},
    "yellow": {"white", "black", "grey", "gray", "navy", "purple"},
    "purple": {"white", "black", "grey", "gray", "beige", "yellow"},
    "pink": {"white", "black", "grey", "gray", "beige", "navy", "brown"},
    "orange": {"white", "black", "beige", "brown", "blue", "navy"},
}
FORMALITY = {"sporty": 1, "casual": 2, "semi-formal": 3, "semiformal": 3, "formal": 4}
PATTERNS = {"striped", "stripe", "checked", "check", "plaid", "printed", "graphic", "floral"}
FITS = {"slim", "fitted", "regular", "relaxed", "oversized", "wide-leg", "straight", "cropped"}
FABRICS = {
    "cotton", "linen", "denim", "silk", "wool", "leather", "suede",
    "polyester", "nylon", "rayon", "viscose", "velvet", "chiffon",
}
TEXTURES = {
    "smooth", "ribbed", "knitted", "knit", "quilted", "woven", "sheer",
    "textured", "embroidered", "sequined", "distressed",
}
STYLE_WORDS = {
    "minimal", "classic", "casual", "formal", "office", "smart", "sporty",
    "streetwear", "ethnic", "traditional", "bohemian", "glam", "preppy",
    "utility", "vintage", "festive", "contemporary",
}

TOP_TERMS = {
    "shirt", "tshirt", "t-shirt", "tee", "top", "blouse", "polo", "sweater", "sweatshirt",
    "hoodie", "kurta", "tunic", "tank", "camisole",
}
BOTTOM_TERMS = {
    "pants", "pant", "trousers", "trouser", "jeans", "jean", "shorts", "short",
    "skirt", "salwar", "dhoti", "churidar", "leggings", "joggers", "cargo",
}
LAYER_TERMS = {
    "jacket", "blazer", "coat", "cardigan", "overshirt", "shrug", "waistcoat",
    "outerwear",
}
ONE_PIECE_TERMS = {"dress", "saree", "lehenga", "anarkali", "jumpsuit", "ethnic_set", "sherwani"}
FOOTWEAR_TERMS = {
    "shoes", "shoe", "sneakers", "sneaker", "boots", "boot", "sandals", "sandal",
    "slippers", "slipper", "loafers", "loafer", "heels", "heel", "mojari", "juttis", "jutti",
}
ACCESSORY_TERMS = {
    "accessory", "watch", "bag", "handbag", "backpack", "belt", "cap", "hat",
    "scarf", "dupatta", "jewellery", "jewelry", "necklace", "bracelet", "sunglasses", "wallet",
}
ETHNIC_TERMS = {
    "kurta", "saree", "lehenga", "sherwani", "salwar", "dhoti", "dupatta",
    "blouse", "anarkali", "churidar", "mojari", "juttis", "jutti", "ethnic_set",
}


def _tokens(*values: Any) -> set[str]:
    text = " ".join(
        " ".join(str(part) for part in value) if isinstance(value, (list, tuple, set)) else str(value or "")
        for value in values
    ).casefold()
    return {token for token in re.split(r"[^a-z0-9-]+", text) if token}


def _first_token_match(tokens: set[str], choices: Iterable[str]) -> str | None:
    return next((choice for choice in choices if choice in tokens), None)


@dataclass(frozen=True)
class Garment:
    id: str
    name: str
    category: str
    role: str
    colours: tuple[str, ...]
    formality: int
    styles: frozenset[str]
    pattern: str | None
    fit: str | None
    fabric: str | None
    texture: str | None
    season: frozenset[str]
    ethnic: bool
    audience: str
    prompt_details: dict[str, Any] = field(compare=False, hash=False, repr=False)


@dataclass(frozen=True)
class OutfitCandidate:
    candidate_id: str
    garments: tuple[Garment, ...]
    score: float
    breakdown: dict[str, float] = field(compare=False, hash=False)

    @property
    def item_ids(self) -> list[str]:
        return [garment.id for garment in self.garments]

    def prompt_payload(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "local_score": round(self.score, 1),
            "score_breakdown": {key: round(value, 2) for key, value in self.breakdown.items()},
            "items": [garment.prompt_details for garment in self.garments],
        }


def normalize_garment(item: dict[str, Any]) -> Garment:
    category = str(item.get("category") or item.get("ai_category") or "other").strip().casefold()
    combined = _tokens(
        category,
        item.get("subcategory"),
        item.get("name"),
        item.get("description"),
        item.get("ai_description"),
        item.get("tags"),
        item.get("ai_visual_tags"),
    )
    category_tokens = _tokens(category, item.get("subcategory"))
    role_tokens = (
        combined
        if category in {"", "other", "unknown", "clothing", "apparel"}
        else category_tokens
    )
    if role_tokens & ONE_PIECE_TERMS or category in ONE_PIECE_TERMS:
        role = "one_piece"
    elif role_tokens & LAYER_TERMS or category in LAYER_TERMS:
        role = "layer"
    elif role_tokens & FOOTWEAR_TERMS or category in FOOTWEAR_TERMS:
        role = "footwear"
    elif role_tokens & ACCESSORY_TERMS or category in ACCESSORY_TERMS:
        role = "accessory"
    elif role_tokens & BOTTOM_TERMS or category in BOTTOM_TERMS:
        role = "bottom"
    elif role_tokens & TOP_TERMS or category in TOP_TERMS:
        role = "top"
    else:
        role = "unknown"

    colour_tokens = _tokens(item.get("color"), item.get("ai_color"))
    colours = tuple(sorted(colour_tokens & (NEUTRALS | set(COLOUR_PARTNERS) | {"multicolor"})))
    if not colours:
        colours = ("unknown",)
    raw_formality = str(item.get("formality") or item.get("ai_formality") or "casual").casefold()
    formality = FORMALITY.get(raw_formality, 2)
    styles = frozenset(combined & STYLE_WORDS)
    ethnic = bool(combined & ETHNIC_TERMS)
    if ethnic:
        styles = frozenset(set(styles) | {"ethnic"})
    pattern = _first_token_match(combined, PATTERNS)
    fit = _first_token_match(combined, FITS)
    fabric = _first_token_match(combined, FABRICS)
    texture = _first_token_match(combined, TEXTURES)
    if combined & {"women", "womens", "woman", "female"}:
        audience = "womenswear"
    elif combined & {"men", "mens", "man", "male"}:
        audience = "menswear"
    elif "unisex" in combined:
        audience = "unisex"
    else:
        audience = "unspecified"
    raw_season = item.get("season") or item.get("ai_season") or []
    seasons = frozenset(_tokens(raw_season)) or frozenset({"all"})
    details = {
        "id": str(item["id"]),
        "name": str(item.get("name") or category.title()),
        "role": role,
        "category": category,
        "subcategory": item.get("subcategory"),
        "colours": list(colours),
        "formality_level": formality,
        "styles": sorted(styles),
        "pattern": pattern or "solid_or_unknown",
        "fit": fit or "unknown",
        "fabric": fabric or "unknown",
        "texture": texture or "unknown",
        "season": sorted(seasons),
        "audience": audience,
        "description": item.get("description") or item.get("ai_description"),
        "visual_tags": item.get("ai_visual_tags") or [],
    }
    return Garment(
        id=str(item["id"]),
        name=details["name"],
        category=category,
        role=role,
        colours=colours,
        formality=formality,
        styles=styles,
        pattern=pattern,
        fit=fit,
        fabric=fabric,
        texture=texture,
        season=seasons,
        ethnic=ethnic,
        audience=audience,
        prompt_details=details,
    )


def colour_harmony(first: Garment, second: Garment) -> float:
    a = next((colour for colour in first.colours if colour != "unknown"), "unknown")
    b = next((colour for colour in second.colours if colour != "unknown"), "unknown")
    if "unknown" in {a, b} or "multicolor" in {a, b}:
        return 0.62
    if a == b:
        return 0.82 if a in NEUTRALS else 0.68
    if a in NEUTRALS or b in NEUTRALS:
        return 0.94
    if b in COLOUR_PARTNERS.get(a, set()) or a in COLOUR_PARTNERS.get(b, set()):
        return 0.88
    return 0.38


def _silhouette_score(first: Garment, second: Garment) -> float:
    relaxed = {"relaxed", "oversized", "wide-leg"}
    fitted = {"slim", "fitted", "cropped"}
    if not first.fit or not second.fit:
        return 0.72
    if first.fit in relaxed and second.fit in relaxed:
        return 0.52
    if (first.fit in relaxed and second.fit in fitted) or (first.fit in fitted and second.fit in relaxed):
        return 0.95
    return 0.8


def _style_score(garments: tuple[Garment, ...]) -> float:
    primary = [garment for garment in garments if garment.role not in {"footwear", "accessory"}]
    known = [garment.styles for garment in primary if garment.styles]
    if len(known) < 2:
        return 0.72
    overlap = set.intersection(*(set(styles) for styles in known))
    if overlap:
        return 0.96
    has_sporty = any("sporty" in styles for styles in known)
    has_formal = any(styles & {"formal", "office"} for styles in known)
    has_ethnic = any("ethnic" in styles for styles in known)
    has_western = any("ethnic" not in styles for styles in known)
    if (has_sporty and has_formal) or (has_ethnic and has_western):
        return 0.3
    return 0.64


def _texture_score(garments: tuple[Garment, ...]) -> float:
    primary = [item for item in garments if item.role not in {"accessory", "footwear"}]
    known_fabrics = [item.fabric for item in primary if item.fabric]
    known_textures = [item.texture for item in primary if item.texture]
    if not known_fabrics and not known_textures:
        return 0.72
    # Repetition reads as intentional tonal dressing; one textured piece also
    # works as a controlled point of interest. Multiple unrelated statement
    # textures are deliberately scored lower.
    if len(set(known_fabrics)) <= 1 and len(set(known_textures)) <= 1:
        return 0.9
    if len(set(known_textures)) <= 1:
        return 0.82
    return 0.58


def _occasion_target(occasion: str) -> tuple[int, set[str]]:
    tokens = _tokens(occasion)
    if tokens & {"wedding", "reception", "festival", "festive", "diwali", "eid", "navratri", "puja"}:
        return 4, {"ethnic", "formal", "festive", "glam"}
    if tokens & {"interview", "office", "meeting", "corporate", "work", "formal"}:
        return 4, {"office", "formal", "classic", "minimal"}
    if tokens & {"gym", "workout", "sport", "running"}:
        return 1, {"sporty"}
    if tokens & {"party", "date", "dinner", "cocktail"}:
        return 3, {"glam", "smart", "contemporary"}
    return 2, {"casual", "minimal", "classic", "streetwear"}


def _hard_compatible(first: Garment, second: Garment) -> bool:
    if abs(first.formality - second.formality) > 2:
        return False
    if first.pattern and second.pattern:
        # Pattern mixing is an advanced styling choice. Keep it out of the
        # automatic engine unless it is clearly a coordinated ethnic pairing.
        if not (first.ethnic and second.ethnic and first.pattern == second.pattern):
            return False
    styles = set(first.styles) | set(second.styles)
    if "sporty" in styles and styles & {"formal", "office", "ethnic"}:
        return False
    if first.ethnic != second.ethnic and "contemporary" not in styles:
        # Neutral western trousers are commonly worn with kurtas; preserve that
        # intentional formula but reject arbitrary ethnic/western mixing.
        if not ({first.category, second.category} & {"kurta"} and {first.role, second.role} == {"top", "bottom"}):
            return False
    return True


def validate_candidate(garments: Iterable[Garment]) -> tuple[bool, str]:
    values = tuple(garments)
    if not values:
        return False, "empty outfit"
    if len({garment.id for garment in values}) != len(values):
        return False, "duplicate item"
    roles = [garment.role for garment in values]
    one_pieces = [garment for garment in values if garment.role == "one_piece"]
    if len(one_pieces) > 1 or roles.count("bottom") > 1 or roles.count("layer") > 1:
        return False, "duplicate garment role"
    if one_pieces:
        extra_tops = [garment for garment in values if garment.role == "top"]
        if extra_tops and not (one_pieces[0].ethnic and all(item.category == "blouse" for item in extra_tops)):
            return False, "one-piece combined with unrelated top"
    elif not ("top" in roles and "bottom" in roles):
        return False, "missing top or bottom"
    primary = [garment for garment in values if garment.role not in {"accessory", "footwear"}]
    for index, first in enumerate(primary):
        for second in primary[index + 1 :]:
            if not _hard_compatible(first, second):
                return False, "incompatible formality, pattern or style"
    return True, "valid"


def _score_candidate(
    garments: tuple[Garment, ...],
    occasion: str,
    profile: dict[str, Any],
    item_affinity: dict[str, float],
) -> tuple[float, dict[str, float]]:
    primary = [garment for garment in garments if garment.role not in {"accessory", "footwear", "layer"}]
    completeness = 1.0
    formality_values = [garment.formality for garment in garments if garment.role != "accessory"]
    coherence = max(0.0, 1 - ((max(formality_values) - min(formality_values)) / 3))
    colour = colour_harmony(primary[0], primary[1]) if len(primary) > 1 else 0.82
    silhouette = _silhouette_score(primary[0], primary[1]) if len(primary) > 1 else 0.82
    style = _style_score(garments)
    texture = _texture_score(garments)
    target_formality, target_styles = _occasion_target(occasion)
    average_formality = sum(formality_values) / len(formality_values)
    occasion_score = max(0.0, 1 - abs(average_formality - target_formality) / 3)
    if any(garment.styles & target_styles for garment in garments):
        occasion_score = min(1.0, occasion_score + 0.12)
    preferred = _tokens(profile.get("preferred_styles") or [])
    if preferred:
        personal = sum(bool(garment.styles & preferred) for garment in garments) / len(garments)
        personal = 0.55 + (personal * 0.45)
    else:
        personal = 0.72
    affinity = sum(item_affinity.get(garment.id, 0.0) for garment in garments) / len(garments)
    personal = max(0.0, min(1.0, personal + affinity * 0.08))
    breakdown = {
        "completeness": completeness,
        "formality": coherence,
        "colour": colour,
        "silhouette": silhouette,
        "style": style,
        "texture": texture,
        "occasion_personal": (occasion_score + personal) / 2,
    }
    score = 100 * (
        completeness * 0.22
        + coherence * 0.16
        + colour * 0.18
        + silhouette * 0.13
        + texture * 0.09
        + style * 0.10
        + breakdown["occasion_personal"] * 0.12
    )
    if any(garment.role == "footwear" for garment in garments):
        score += 1.5
    return min(score, 100), breakdown


def _best_addition(base: tuple[Garment, ...], choices: list[Garment]) -> Garment | None:
    compatible = [choice for choice in choices if all(_hard_compatible(item, choice) for item in base)]
    if not compatible:
        return None
    return max(
        compatible,
        key=lambda choice: sum(colour_harmony(item, choice) for item in base) - abs(
            choice.formality - (sum(item.formality for item in base) / len(base))
        ),
    )


def generate_outfit_candidates(
    items: list[dict[str, Any]],
    occasion: str,
    profile: dict[str, Any] | None = None,
    item_affinity: dict[str, float] | None = None,
    *,
    limit: int = 10,
) -> list[OutfitCandidate]:
    profile = profile or {}
    item_affinity = item_affinity or {}
    garments = [normalize_garment(item) for item in items]
    roles = {
        role: [garment for garment in garments if garment.role == role]
        for role in {"top", "bottom", "one_piece", "layer", "footwear", "accessory"}
    }
    bases: list[tuple[Garment, ...]] = []
    for top, bottom in product(roles["top"], roles["bottom"]):
        if _hard_compatible(top, bottom):
            bases.append((top, bottom))
    for one_piece in roles["one_piece"]:
        if one_piece.category in {"saree", "lehenga"}:
            blouses = [item for item in roles["top"] if item.category == "blouse" and _hard_compatible(one_piece, item)]
            bases.extend((one_piece, blouse) for blouse in blouses)
        bases.append((one_piece,))

    expanded: list[tuple[Garment, ...]] = []
    for base in bases:
        outfit = base
        footwear = _best_addition(outfit, roles["footwear"])
        if footwear:
            outfit += (footwear,)
        target_formality, _ = _occasion_target(occasion)
        if target_formality >= 3:
            layer = _best_addition(outfit, roles["layer"])
            if layer:
                outfit += (layer,)
        accessory = _best_addition(outfit, roles["accessory"])
        if accessory and len(outfit) < 5:
            outfit += (accessory,)
        expanded.append(outfit)
        # Keep a simpler version too; restraint often produces a stronger look.
        if outfit != base:
            expanded.append(base)

    unique: dict[tuple[str, ...], tuple[Garment, ...]] = {}
    for garments_tuple in expanded:
        valid, _ = validate_candidate(garments_tuple)
        if valid:
            unique.setdefault(tuple(item.id for item in garments_tuple), garments_tuple)
    ranked: list[OutfitCandidate] = []
    for garments_tuple in unique.values():
        score, breakdown = _score_candidate(garments_tuple, occasion, profile, item_affinity)
        ranked.append(OutfitCandidate("", garments_tuple, score, breakdown))
    ranked.sort(key=lambda candidate: candidate.score, reverse=True)
    return [
        OutfitCandidate(f"C{index}", candidate.garments, candidate.score, candidate.breakdown)
        for index, candidate in enumerate(ranked[:limit], start=1)
    ]


def fallback_reasoning(candidate: OutfitCandidate, occasion: str) -> str:
    names = [garment.name for garment in candidate.garments]
    if len(names) == 1:
        pieces = names[0]
    else:
        pieces = ", ".join(names[:-1]) + f" and {names[-1]}"
    return (
        f"Wear {pieces}. The pieces form a complete, coherent look for {occasion}, "
        "with balanced formality, colour and silhouette from your own wardrobe."
    )
