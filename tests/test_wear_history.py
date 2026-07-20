from app.api.router import api_router
from app.api.routes.wardrobe import _group_wear_history


def test_selfie_routes_are_not_mounted() -> None:
    paths = {route.path for route in api_router.routes}

    assert "/wardrobe/wear-history" in paths
    assert not any("/outfit-selfies" in path for path in paths)


def test_group_wear_history_combines_items_logged_as_one_outfit() -> None:
    worn_at = "2026-07-20T08:00:00+00:00"
    logs = [
        {
            "id": "log-1",
            "wardrobe_item_id": "shirt",
            "worn_at": worn_at,
            "notes": "Outfit outfit-1",
        },
        {
            "id": "log-2",
            "wardrobe_item_id": "pants",
            "worn_at": worn_at,
            "notes": "Outfit outfit-1",
        },
        {
            "id": "log-3",
            "wardrobe_item_id": "shoes",
            "worn_at": "2026-07-19T08:00:00+00:00",
            "notes": None,
        },
    ]
    items = {
        "shirt": {"id": "shirt", "name": "White shirt"},
        "pants": {"id": "pants", "name": "Black pants"},
        "shoes": {"id": "shoes", "name": "Loafers"},
    }

    result = _group_wear_history(logs, items)

    assert len(result) == 2
    assert result[0]["id"] == "log-1"
    assert [item["id"] for item in result[0]["items"]] == ["shirt", "pants"]
    assert [item["id"] for item in result[1]["items"]] == ["shoes"]


def test_group_wear_history_ignores_deleted_wardrobe_items() -> None:
    result = _group_wear_history(
        [
            {
                "id": "log-1",
                "wardrobe_item_id": "deleted",
                "worn_at": "2026-07-20T08:00:00+00:00",
                "notes": None,
            }
        ],
        {},
    )

    assert result == []
