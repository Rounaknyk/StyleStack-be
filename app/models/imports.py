from typing import Literal

from pydantic import BaseModel, Field


class GmailImportRequest(BaseModel):
    access_token: str = Field(min_length=20, max_length=4096)
    max_messages: int = Field(default=10, ge=1, le=50)


class GmailImportResponse(BaseModel):
    scanned_messages: int
    imported_items: int
    skipped_items: int


class GmailImportJobResponse(BaseModel):
    job_id: str
    status: Literal["queued", "processing", "completed", "failed"]
    scanned_messages: int = 0
    imported_items: int = 0
    skipped_items: int = 0
    error: str | None = None


class GmailProductAnalysis(BaseModel):
    is_fashion_item: bool
    name: str | None = Field(default=None, max_length=200)
    brand: str | None = Field(default=None, max_length=100)
    category: Literal[
        "shirt", "pants", "dress", "jacket", "shoes", "accessory",
        "kurta", "saree", "lehenga", "sherwani", "salwar", "dhoti",
        "dupatta", "blouse", "anarkali", "ethnic_set", "other"
    ] | None = None
    color: Literal[
        "black", "white", "red", "blue", "green", "yellow", "purple",
        "pink", "brown", "grey", "orange", "beige", "multicolor",
    ] | None = None
    season: Literal["summer", "winter", "spring", "autumn", "all"] | None = None
    formality: Literal["formal", "semi-formal", "casual", "sporty"] | None = None
    description: str | None = Field(default=None, max_length=500)
    tags: list[str] = Field(default_factory=list, max_length=5)
