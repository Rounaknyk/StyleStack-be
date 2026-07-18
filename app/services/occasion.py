from datetime import date


_FIXED_INDIAN_OCCASIONS = {
    (1, 26): "Republic Day celebration",
    (8, 15): "Independence Day celebration",
    (10, 2): "Gandhi Jayanti",
    (12, 25): "Christmas celebration",
}


def today_occasion(today: date | None = None) -> str | None:
    """Return a culturally useful occasion for dates with a fixed day.

    Variable lunar festivals are intentionally not guessed here; Google/manual
    calendar events provide the reliable date and title for those occasions.
    """
    current = today or date.today()
    return _FIXED_INDIAN_OCCASIONS.get((current.month, current.day))
