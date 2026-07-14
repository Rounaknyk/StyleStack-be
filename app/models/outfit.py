from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class OutfitSuggestionRequest(BaseModel):
    city: str | None = Field(default=None, min_length=2, max_length=120)
    occasion: str = Field(default="daily", min_length=2, max_length=80)
    calendar_event_id: UUID | None = None


class WeatherResponse(BaseModel):
    city: str
    temperature_c: float
    feels_like_c: float
    condition: str
    description: str


class OutfitResponse(BaseModel):
    id: UUID
    owner_firebase_uid: str
    occasion: str
    reasoning: str
    weather: dict[str, Any]
    item_ids: list[UUID]
    items: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime


class OutfitWearResponse(BaseModel):
    outfit_id: UUID
    logged_items: int
