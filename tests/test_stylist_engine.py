from app.services.stylist_engine import (
    generate_outfit_candidates,
    normalize_garment,
    validate_candidate,
)


def item(item_id: str, category: str, **values):
    return {
        "id": item_id,
        "name": values.pop("name", item_id.replace("-", " ").title()),
        "category": category,
        **values,
    }


def test_candidates_are_complete_and_never_pair_duplicate_roles() -> None:
    candidates = generate_outfit_candidates(
        [
            item("white-shirt", "shirt", color="white", formality="casual"),
            item("blue-shirt", "shirt", color="blue", formality="casual"),
            item("navy-pants", "pants", color="navy", formality="casual"),
            item("sneakers", "shoes", color="white", formality="casual"),
        ],
        "casual everyday look",
    )

    assert candidates
    for candidate in candidates:
        roles = [garment.role for garment in candidate.garments]
        assert roles.count("top") == 1
        assert roles.count("bottom") == 1
        assert validate_candidate(candidate.garments)[0]


def test_normalization_exposes_structured_styling_metadata() -> None:
    garment = normalize_garment(
        item(
            "linen-shirt",
            "other",
            name="Men's relaxed striped linen shirt",
            color="blue",
            season=["summer"],
            tags=["casual", "woven"],
        )
    )
    assert garment.role == "top"
    assert garment.fabric == "linen"
    assert garment.texture == "woven"
    assert garment.fit == "relaxed"
    assert garment.audience == "menswear"
    assert garment.prompt_details["season"] == ["summer"]


def test_formal_and_sporty_primary_pieces_are_rejected() -> None:
    blazer = normalize_garment(
        item("blazer", "blazer", formality="formal", tags=["office"])
    )
    joggers = normalize_garment(
        item("joggers", "joggers", formality="sporty", tags=["sporty"])
    )
    assert validate_candidate((blazer, joggers)) == (False, "missing top or bottom")

    shirt = normalize_garment(
        item("formal-shirt", "shirt", formality="formal", tags=["office"])
    )
    assert not validate_candidate((shirt, joggers))[0]


def test_indian_formulas_include_kurta_bottom_and_saree_blouse() -> None:
    candidates = generate_outfit_candidates(
        [
            item("kurta", "kurta", color="blue", tags=["ethnic"]),
            item("salwar", "salwar", color="white", tags=["ethnic"]),
            item("saree", "saree", color="red", tags=["ethnic", "festive"]),
            item("blouse", "blouse", color="red", tags=["ethnic", "festive"]),
        ],
        "Diwali celebration",
    )
    combinations = {tuple(candidate.item_ids) for candidate in candidates}
    assert ("kurta", "salwar") in combinations
    assert ("saree", "blouse") in combinations


def test_neutral_colour_pair_outranks_unrelated_saturated_pair() -> None:
    candidates = generate_outfit_candidates(
        [
            item("blue-shirt", "shirt", color="blue"),
            item("white-pants", "pants", color="white"),
            item("purple-pants", "pants", color="purple"),
        ],
        "casual everyday look",
    )
    assert candidates[0].item_ids == ["blue-shirt", "white-pants"]


def test_incomplete_wardrobe_returns_no_candidate() -> None:
    assert generate_outfit_candidates(
        [item("shirt", "shirt", color="white")],
        "casual everyday look",
    ) == []


def test_positive_item_affinity_can_break_a_close_tie() -> None:
    candidates = generate_outfit_candidates(
        [
            item("white-shirt", "shirt", color="white", tags=["minimal"]),
            item("cream-shirt", "shirt", color="cream", tags=["minimal"]),
            item("navy-pants", "pants", color="navy", tags=["minimal"]),
        ],
        "casual everyday look",
        {"preferred_styles": ["minimal"]},
        {"cream-shirt": 1.0, "white-shirt": -1.0},
    )
    assert candidates[0].item_ids[0] == "cream-shirt"
