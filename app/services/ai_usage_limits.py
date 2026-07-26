"""Durable per-user allowances for costly AI-backed actions."""
from dataclasses import dataclass
from datetime import date
import logging
from fastapi import HTTPException, status
from app.core.config import get_settings
from app.core.supabase import get_supabase_client

logger = logging.getLogger("stylestack.ai_limits")

@dataclass(frozen=True, slots=True)
class DailyQuota:
    operation: str
    limit: int
    used: int
    remaining: int
    is_first_upload_day: bool = False

    def public(self) -> dict[str, object]:
        return {
            "operation": self.operation, "limit": self.limit,
            "used": self.used, "remaining": self.remaining,
            "is_first_upload_day": self.is_first_upload_day,
            "message": upload_allowance_message(self) if self.operation == "wardrobe_upload" else f"{self.remaining} uses remaining today",
        }

def upload_allowance_message(quota: DailyQuota) -> str:
    if quota.is_first_upload_day:
        return f"You can upload {quota.remaining} of 30 outfits today."
    return f"You can upload {quota.remaining} of {quota.limit} outfits today."

def _rows(uid: str, operation: str) -> list[dict[str, object]]:
    response = (get_supabase_client().table("ai_usage_daily")
        .select("usage_date,used").eq("owner_firebase_uid", uid)
        .eq("operation", operation).order("usage_date").execute())
    return list(response.data or [])

def wardrobe_upload_quota(uid: str) -> DailyQuota:
    rows = _rows(uid, "wardrobe_upload")
    today = date.today().isoformat()
    today_row = next((row for row in rows if row.get("usage_date") == today), None)
    # Existing users must not receive a fresh first-day allowance merely because
    # this quota table was introduced after their wardrobe already existed.
    first_item_response = (
        get_supabase_client()
        .table("wardrobe_items")
        .select("created_at")
        .eq("owner_firebase_uid", uid)
        .order("created_at")
        .limit(1)
        .execute()
    )
    first_item = (first_item_response.data or [None])[0]
    first_item_day = (
        str(first_item.get("created_at", ""))[:10]
        if isinstance(first_item, dict)
        else None
    )
    first_day = (
        first_item_day == today
        or (first_item is None and not rows)
    )
    settings = get_settings()
    limit = settings.wardrobe_upload_first_day_limit if first_day else settings.wardrobe_upload_daily_limit
    used = int(today_row.get("used") or 0) if today_row else 0
    return DailyQuota("wardrobe_upload", limit, used, max(0, limit - used), first_day)

def consume(uid: str, operation: str, limit: int, *, units: int = 1) -> DailyQuota:
    response = get_supabase_client().rpc("consume_ai_daily_quota", {
        "p_owner_firebase_uid": uid, "p_operation": operation,
        "p_limit": max(1, limit), "p_units": max(1, units),
    }).execute()
    row = (response.data or [{}])[0]
    quota = DailyQuota(operation, limit, int(row.get("used") or 0), max(0, int(row.get("remaining") or 0)), operation == "wardrobe_upload" and limit == get_settings().wardrobe_upload_first_day_limit)
    if bool(row.get("allowed")):
        logger.info("ai_quota_consumed uid=%s operation=%s used=%s remaining=%s", uid, operation, quota.used, quota.remaining)
        return quota
    logger.info("ai_quota_exhausted uid=%s operation=%s limit=%s used=%s", uid, operation, limit, quota.used)
    detail = upload_allowance_message(quota) if operation == "wardrobe_upload" else f"You have used today's {operation.replace('_', ' ')} allowance. Please try again tomorrow."
    raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail)
