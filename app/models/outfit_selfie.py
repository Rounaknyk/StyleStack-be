from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.wardrobe import WardrobeItemResponse


class OutfitSelfieDetectionResponse(BaseModel):
    id: UUID
    detected_name: str
    detected_category: str | None = None
    detected_color: str | None = None
    detected_description: str | None = None
    visual_tags: list[str] = Field(default_factory=list)
    confidence: float
    selected: bool = True
    wardrobe_item_id: UUID | None = None
    wardrobe_item: WardrobeItemResponse | None = None


class OutfitSelfieAnalysisResponse(BaseModel):
    quality_acceptable: bool
    quality_score: float
    quality_feedback: str
    selfie_id: UUID | None = None
    image_url: str | None = None
    detections: list[OutfitSelfieDetectionResponse] = Field(default_factory=list)


class OutfitSelfieDetectionConfirmation(BaseModel):
    detection_id: UUID
    selected: bool = True
    wardrobe_item_id: UUID | None = None


class OutfitSelfieConfirmation(BaseModel):
    detections: list[OutfitSelfieDetectionConfirmation] = Field(
        min_length=1, max_length=12
    )


class OutfitSelfieConfirmationResponse(BaseModel):
    selfie_id: UUID
    status: Literal["confirmed"]
    logged_items: int
    unmatched_items: list[str] = Field(default_factory=list)


class OutfitSelfieHistoryEntry(BaseModel):
    id: UUID
    image_url: str | None = None
    captured_at: datetime
    confirmed_at: datetime | None = None
    items: list[WardrobeItemResponse] = Field(default_factory=list)
