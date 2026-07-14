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
    background_removal_model: str = "u2netp"
    fashion_segmentation_enabled: bool = True
    fashion_segmentation_model_url: str = (
        "https://huggingface.co/Xenova/segformer_b2_clothes/resolve/main/onnx/model_quantized.onnx"
    )
    fashion_segmentation_model_sha256: str = (
        "2b18fbdd196a7ee12702c2fceaa6f812fe70d1154573a43bd62667b9157ee48d"
    )

    groq_api_key: str | None = None
    groq_vision_model: str = "qwen/qwen3.6-27b"
    groq_request_timeout_seconds: float = 30.0
    gemini_api_key: str | None = None
    gemini_vision_model: str = "gemini-flash-latest"

    openweather_api_key: str | None = None
    openweather_base_url: str = "https://api.openweathermap.org/data/2.5"
    notification_scheduler_enabled: bool = True
    notification_poll_seconds: int = 60
    gmail_import_log_email_previews: bool = False
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    google_calendar_auto_sync_enabled: bool = True

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
