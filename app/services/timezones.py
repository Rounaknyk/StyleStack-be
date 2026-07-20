import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger("stylestack.timezones")

TIMEZONE_ALIASES = {
    "GMT": "UTC",
    "IST": "Asia/Kolkata",
    "UTC": "UTC",
}


def normalize_timezone_name(value: str | None) -> str:
    """Return a valid IANA timezone, accepting common device abbreviations."""
    requested = str(value or "UTC").strip() or "UTC"
    candidate = TIMEZONE_ALIASES.get(requested.upper(), requested)
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError:
        logger.warning(
            "invalid_timezone_fallback requested=%s fallback=UTC",
            requested,
        )
        return "UTC"
    return candidate


def resolve_timezone(value: str | None) -> ZoneInfo:
    return ZoneInfo(normalize_timezone_name(value))
