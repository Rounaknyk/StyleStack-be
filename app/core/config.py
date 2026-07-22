from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "StyleStack API"
    environment: str = "development"
    # Use an app-specific environment name. Generic DEBUG is commonly set by
    # shell/tooling (for example DEBUG=release) and can otherwise break startup.
    debug: bool = Field(default=False, validation_alias="STYLESTACK_DEBUG")
    api_v1_prefix: str = "/api/v1"
    allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://localhost:5173"]
    )

    firebase_service_account_json: str

    supabase_url: str
    supabase_service_role_key: str
    supabase_storage_bucket: str = "wardrobe-images"
    background_removal_enabled: bool = True
    # BiRefNet Lite is substantially more accurate around sleeves, collars,
    # hems, and soft garment edges than the tiny u2netp model.
    background_removal_model: str = "birefnet-general-lite"
    # Poof is the fast, high-quality primary remover. When its credits are
    # unavailable the image pipeline automatically falls back to the local
    # segmentation/BiRefNet implementation below.
    poof_api_key: str | None = None
    poof_request_timeout_seconds: float = 45.0
    fashion_segmentation_enabled: bool = True
    fashion_segmentation_model_url: str = (
        "https://huggingface.co/Xenova/segformer_b2_clothes/resolve/main/onnx/model_quantized.onnx"
    )
    fashion_segmentation_model_sha256: str = (
        "2b18fbdd196a7ee12702c2fceaa6f812fe70d1154573a43bd62667b9157ee48d"
    )

    groq_api_key: str | None = None
    groq_vision_model: str = "qwen/qwen3.6-27b"
    # Kept separate so the text-only stylist can move to a cheaper/faster
    # model without changing image auto-tagging.
    groq_stylist_model: str = "qwen/qwen3.6-27b"
    groq_request_timeout_seconds: float = 30.0
    groq_requests_per_minute: int = 30
    groq_default_retry_after_seconds: float = 2.0
    ai_requests_per_user_per_minute: int = 3
    ai_request_queue_max_size: int = 500
    ai_request_job_retention_seconds: int = 3600
    gemini_api_key: str | None = None
    gemini_vision_model: str = "gemini-flash-latest"
    pexels_api_key: str | None = None
    # Global operational switch for the optional Pexels moodboard. Keeping
    # this in environment configuration avoids a database read per outfit.
    pexels_inspiration_enabled: bool = True
    pexels_base_url: str = "https://api.pexels.com/v1"
    pexels_request_timeout_seconds: float = 8.0
    pexels_results_per_request: int = 10
    # Disabled by default: local CLIP downloads a roughly 600 MB model.
    inspiration_clip_enabled: bool = False
    inspiration_clip_model: str = "openai/clip-vit-base-patch32"
    inspiration_clip_threshold: float = 0.28
    inspiration_clip_request_timeout_seconds: float = 12.0

    openweather_api_key: str | None = None
    openweather_base_url: str = "https://api.openweathermap.org/data/2.5"
    notification_scheduler_enabled: bool = True
    notification_poll_seconds: int = 60
    # Protects the owner-only broadcast endpoint. Set a long random value in
    # hosted environments and never ship it in the Flutter application.
    admin_notification_key: str | None = None
    broadcast_notification_topic: str = "stylestack_announcements"
    gmail_import_log_email_previews: bool = False
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    google_calendar_auto_sync_enabled: bool = True
    # Refresh connected calendars frequently enough to catch last-minute
    # meetings without polling Google on every API request.
    google_calendar_sync_interval_seconds: int = 300

    # Cost-control defaults for the free pilot. Set false when moving to a
    # durable worker/paid provider; all existing full-fidelity behavior then
    # remains available.
    free_pilot_mode: bool = True
    free_pilot_ai_daily_limit: int = 3
    free_pilot_gmail_max_messages: int = 10
    free_pilot_inspiration_cache_seconds: int = 86400

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def parse_allowed_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
