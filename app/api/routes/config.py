import logging
import os
from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel

from app.core.supabase import get_supabase_client
from app.dependencies.auth import OwnerUser

router = APIRouter()
logger = logging.getLogger("stylestack.config")

# Simple API Key protection for admin endpoints
API_KEY_NAME = "x-admin-api-key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

def verify_admin_api_key(api_key: str | None = Depends(api_key_header)):
    expected_api_key = os.environ.get("ADMIN_API_KEY")
    if not expected_api_key:
        logger.warning("ADMIN_API_KEY environment variable is not set")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Admin configuration is missing on the server",
        )
    if api_key != expected_api_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid Admin API Key",
        )
    return api_key


class AppFeatures(BaseModel):
    phone_auth_enabled: bool | None = None
    google_calendar_enabled: bool | None = None


@router.get("/features", response_model=AppFeatures)
def get_features():
    client = get_supabase_client()
    phone_enabled = True
    calendar_enabled = False
    try:
        # Fetch all config values to avoid multiple queries
        data = client.table("app_config").select("*").in_("config_key", ["phone_auth_enabled", "google_calendar_enabled"]).execute()
        if data.data:
            for item in data.data:
                val = str(item.get("config_value", "false")).lower() == "true"
                if item["config_key"] == "phone_auth_enabled":
                    phone_enabled = val
                elif item["config_key"] == "google_calendar_enabled":
                    calendar_enabled = val
    except Exception as e:
        logger.error("Failed to read app_config: %s", e)
    
    return AppFeatures(
        phone_auth_enabled=phone_enabled,
        google_calendar_enabled=calendar_enabled,
    )


@router.post("/features", response_model=AppFeatures)
def update_features(features: AppFeatures, _: str = Depends(verify_admin_api_key)):
    return _do_update_features(features)


@router.post("/owner/features", response_model=AppFeatures)
def update_owner_features(features: AppFeatures, owner: OwnerUser):
    logger.info("Owner %s is updating features via in-app toggle", owner.get("email") or owner.get("uid"))
    return _do_update_features(features)


def _do_update_features(features: AppFeatures):
    client = get_supabase_client()
    try:
        upserts = []
        if features.phone_auth_enabled is not None:
            upserts.append({
                "config_key": "phone_auth_enabled",
                "config_value": features.phone_auth_enabled
            })
        if features.google_calendar_enabled is not None:
            upserts.append({
                "config_key": "google_calendar_enabled",
                "config_value": features.google_calendar_enabled
            })
            
        if upserts:
            client.table("app_config").upsert(upserts).execute()
            
        logger.info("Features updated: %s", features.dict(exclude_unset=True))
        
        # Return current full state
        return get_features()
    except Exception as e:
        logger.error("Failed to update app_config: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update configuration",
        )

