from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class OutfitSuggestionRequest(BaseModel):
    city: str | None = Field(default=None, min_length=2, max_length=120)
    occasion: str = Field(default="daily", min_length=2, max_length=80)
    calendar_event_id: UUID | None = None
    refresh: bool = False
    previous_outfit_id: UUID | None = None


class WeatherResponse(BaseModel):
    city: str
    temperature_c: float | None = None
    feels_like_c: float | None = None
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
    inspiration_enabled: bool = True
    inspiration_images: list[dict[str, Any]] = Field(default_factory=list)


class OutfitWearResponse(BaseModel):
    outfit_id: UUID
    logged_items: int


OutfitFeedbackSignal = Literal[
    "worn", "liked", "refreshed", "wore_something_else", "disliked"
]


class OutfitFeedbackRequest(BaseModel):
    signal: OutfitFeedbackSignal
    reason: str | None = Field(default=None, max_length=240)


class OutfitFeedbackResponse(BaseModel):
    outfit_id: UUID
    signal: OutfitFeedbackSignal
    saved: bool = True
