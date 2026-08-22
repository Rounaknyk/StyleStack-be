import logging
import os
from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel

from app.core.supabase import get_supabase_client

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
    phone_auth_enabled: bool


@router.get("/features", response_model=AppFeatures)
def get_features():
    client = get_supabase_client()
    try:
        data = client.table("app_config").select("config_value").eq("config_key", "phone_auth_enabled").limit(1).execute()
        if data.data and len(data.data) > 0:
            is_enabled = str(data.data[0].get("config_value", "true")).lower() == "true"
            return AppFeatures(phone_auth_enabled=is_enabled)
    except Exception as e:
        logger.error("Failed to read app_config: %s", e)
    
    # Default fallback
    return AppFeatures(phone_auth_enabled=True)


@router.post("/features", response_model=AppFeatures)
def update_features(features: AppFeatures, _: str = Depends(verify_admin_api_key)):
    client = get_supabase_client()
    try:
        # Since config_value is jsonb, we pass the boolean (Supabase client will serialize it)
        client.table("app_config").upsert({
            "config_key": "phone_auth_enabled",
            "config_value": features.phone_auth_enabled
        }).execute()
        logger.info("Features updated: phone_auth_enabled=%s", features.phone_auth_enabled)
        return features
    except Exception as e:
        logger.error("Failed to update app_config: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update configuration",
        )
