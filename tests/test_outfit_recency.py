from datetime import datetime, timedelta, timezone

from app.services.outfits import filter_recently_worn_clothing


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
