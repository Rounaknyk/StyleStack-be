from typing import Literal

from pydantic import BaseModel, Field


class ClothingTags(BaseModel):
    brand: str | None = Field(default=None, max_length=100)
    category: Literal[
        "shirt", "pants", "dress", "jacket", "shoes", "accessory",
        "kurta", "saree", "lehenga", "sherwani", "salwar", "dhoti",
        "dupatta", "blouse", "anarkali", "ethnic_set", "other"
    ]
    color: Literal[
        "black",
        "white",
        "red",
        "blue",
        "green",
        "yellow",
        "purple",
        "pink",
        "brown",
        "grey",
        "orange",
        "beige",
        "multicolor",
    ]
    season: Literal["summer", "winter", "spring", "autumn", "all"]
    formality: Literal["formal", "semi-formal", "casual", "sporty"]
    description: str = Field(min_length=1, max_length=500)
    tags: list[str] = Field(default_factory=list, max_length=5)
    visual_tags: list[str] = Field(default_factory=list, max_length=10)


class ClothingDetection(BaseModel):
    """All wardrobe-relevant garments visible in one source image."""

    items: list[ClothingTags] = Field(min_length=1, max_length=12)


class OutfitSelfieVisionItem(BaseModel):
    detected_name: str = Field(min_length=1, max_length=160)
    category: str = Field(min_length=1, max_length=80)
    color: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=500)
    visual_tags: list[str] = Field(default_factory=list, max_length=10)
    matched_item_id: str | None = None
    confidence: float = Field(ge=0, le=1)


class OutfitSelfieVisionResult(BaseModel):
    quality_acceptable: bool
    quality_score: float = Field(ge=0, le=1)
    quality_feedback: str = Field(min_length=1, max_length=300)
    items: list[OutfitSelfieVisionItem] = Field(default_factory=list, max_length=12)
