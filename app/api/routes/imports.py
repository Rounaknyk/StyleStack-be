import logging

from fastapi import APIRouter, HTTPException

from app.core.supabase import get_supabase_client
from app.dependencies.auth import CurrentUser
from app.models.imports import GmailImportRequest, GmailImportResponse
from app.services.gmail_import import import_gmail_orders
from app.services.wardrobe import ensure_profile

router = APIRouter()
logger = logging.getLogger("stylestack.imports")


@router.post("/gmail", response_model=GmailImportResponse)
def sync_gmail_orders(payload: GmailImportRequest, current_user: CurrentUser) -> GmailImportResponse:
    """Use a short-lived, user-consented Gmail token; the token is never stored."""
    client = get_supabase_client()
    ensure_profile(client, current_user)
    try:
        scanned, imported, skipped = import_gmail_orders(
            client, current_user["uid"], payload.access_token, payload.max_messages
        )
        logger.debug(
            "gmail_import_completed uid=%s scanned=%s imported=%s skipped=%s",
            current_user["uid"], scanned, imported, skipped,
        )
        return GmailImportResponse(
            scanned_messages=scanned, imported_items=imported, skipped_items=skipped
        )
    except Exception as exc:
        logger.error("gmail_import_failed uid=%s error_type=%s", current_user["uid"], type(exc).__name__)
        raise HTTPException(
            status_code=502,
            detail="Could not scan Gmail. Reconnect your account and try again.",
        ) from exc
