from datetime import datetime, timedelta, timezone

from app.services.outfits import (
    filter_recently_worn_clothing,
    rotate_recent_outfit_candidates,
)
from app.services.stylist_engine import generate_outfit_candidates


def test_recent_clothing_is_excluded_but_accessories_remain() -> None:
    now = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
    items = [
        {"id": "shirt", "category": "shirt"},
        {"id": "shoes", "category": "shoes"},
        {"id": "bag", "category": "other", "name": "Leather bag"},
        {"id": "watch", "category": "other", "ai_category": "watch"},
    ]
    logs = [
        {
            "wardrobe_item_id": item["id"],
            "worn_at": (now - timedelta(hours=12)).isoformat(),
        }
        for item in items
    ]

    candidates, excluded = filter_recently_worn_clothing(items, logs, now=now)

    assert excluded == {"shirt"}
    assert [item["id"] for item in candidates] == ["shoes", "bag", "watch"]


def test_clothing_becomes_available_after_three_days() -> None:
    now = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
    items = [{"id": "kurta", "category": "kurta"}]
    logs = [
        {
            "wardrobe_item_id": "kurta",
            "worn_at": (now - timedelta(days=3, seconds=1)).isoformat(),
        }
    ]

    candidates, excluded = filter_recently_worn_clothing(items, logs, now=now)

    assert excluded == set()
    assert candidates == items


def test_clothing_at_three_day_boundary_is_still_excluded() -> None:
    now = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
    items = [{"id": "pants", "category": "pants"}]
    logs = [
        {
            "wardrobe_item_id": "pants",
            "worn_at": (now - timedelta(days=3)).isoformat(),
        }
    ]

    candidates, excluded = filter_recently_worn_clothing(items, logs, now=now)

    assert candidates == []
    assert excluded == {"pants"}


def test_refresh_rotation_excludes_recent_clothing_but_not_accessory_variants() -> None:
    candidates = generate_outfit_candidates(
        [
            {"id": "white-shirt", "name": "White Shirt", "category": "shirt", "color": "white"},
            {"id": "blue-shirt", "name": "Blue Shirt", "category": "shirt", "color": "blue"},
            {"id": "navy-pants", "name": "Navy Pants", "category": "pants", "color": "navy"},
            {"id": "cream-pants", "name": "Cream Pants", "category": "pants", "color": "cream"},
            {"id": "watch", "name": "Watch", "category": "watch", "color": "black"},
        ],
        "casual everyday look",
        limit=10,
    )
    recent = [candidates[0].clothing_signature]

    rotated, removed = rotate_recent_outfit_candidates(candidates, recent)

    assert removed == 1
    assert rotated[0].clothing_signature != recent[0]
    assert [candidate.candidate_id for candidate in rotated] == [
        f"C{index}" for index in range(1, len(rotated) + 1)
    ]


def test_rotation_falls_back_when_every_valid_combination_was_recent() -> None:
    candidates = generate_outfit_candidates(
        [
            {"id": "shirt", "name": "Shirt", "category": "shirt"},
            {"id": "pants", "name": "Pants", "category": "pants"},
        ],
        "casual everyday look",
    )

    rotated, removed = rotate_recent_outfit_candidates(
        candidates, [candidates[0].clothing_signature]
    )

    assert removed == len(candidates)
    assert rotated[0].clothing_signature == candidates[0].clothing_signature
