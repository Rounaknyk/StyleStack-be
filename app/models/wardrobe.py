from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

TagStatus = Literal["pending", "processing", "completed", "failed"]


class WardrobeItemResponse(BaseModel):
    id: UUID
    owner_firebase_uid: str
    name: str
    category: str
    subcategory: str | None = None
    brand: str | None = None
    color: str | None = None
    size: str | None = None
    season: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    description: str | None = None
    formality: str | None = None
    notes: str | None = None
    purchase_date: date | None = None
    purchase_price: Decimal | None = None
    currency: str | None = None
    image_path: str | None = None
    image_url: str | None = None
    is_favorite: bool
    tagged: bool = False
    ai_tag_status: TagStatus = "pending"
    ai_category: str | None = None
    ai_color: str | None = None
    ai_season: str | None = None
    ai_formality: str | None = None
    ai_description: str | None = None
    wear_count: int = 0
    created_at: datetime
    updated_at: datetime


class WardrobeItemUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    category: str | None = Field(default=None, min_length=1, max_length=100)
    subcategory: str | None = Field(default=None, max_length=100)
    brand: str | None = Field(default=None, max_length=100)
    color: str | None = Field(default=None, max_length=100)
    size: str | None = Field(default=None, max_length=50)
    season: list[str] | None = None
    tags: list[str] | None = None
    description: str | None = Field(default=None, max_length=500)
    formality: str | None = Field(default=None, max_length=50)
    notes: str | None = Field(default=None, max_length=2000)
    purchase_date: date | None = None
    purchase_price: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    is_favorite: bool | None = None

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str | None) -> str | None:
        return value.upper() if value else value

    @field_validator("season", "tags")
    @classmethod
    def clean_string_lists(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return list(dict.fromkeys(entry.strip() for entry in value if entry.strip()))


class WearLogCreate(BaseModel):
    worn_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    notes: str | None = Field(default=None, max_length=1000)


class WearLogResponse(BaseModel):
    id: UUID
    wardrobe_item_id: UUID
    owner_firebase_uid: str
    worn_at: datetime
    notes: str | None = None
    created_at: datetime


class TagStatusResponse(BaseModel):
    status: TagStatus
