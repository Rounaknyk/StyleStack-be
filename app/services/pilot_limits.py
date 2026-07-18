"""Small process-local guardrails for the zero-cost pilot.

These limits intentionally fail open across process restarts: they protect a
single free API instance from accidental bursts, but are not a replacement
for a shared rate limiter when the app scales horizontally.
"""

from collections import defaultdict
from datetime import date
from threading import Lock

from app.core.config import get_settings

_lock = Lock()
_usage: dict[tuple[str, date, str], int] = defaultdict(int)


def consume_ai_slot(uid: str, operation: str = "tagging") -> bool:
    settings = get_settings()
    if not settings.free_pilot_mode:
        return True
    key = (uid, date.today(), operation)
    with _lock:
        if _usage[key] >= settings.free_pilot_ai_daily_limit:
            return False
        _usage[key] += 1
        return True


def pilot_gmail_limit(requested: int) -> int:
    settings = get_settings()
    if settings.free_pilot_mode:
        return min(requested, settings.free_pilot_gmail_max_messages)
    return requested
