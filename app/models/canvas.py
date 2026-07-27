from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class CanvasStyleItem(BaseModel):
    item_id: UUID
    x: float = Field(ge=-10000, le=10000)
    y: float = Field(ge=-10000, le=10000)
    scale: float = Field(gt=0.05, le=20)
    rotation: float = Field(ge=-1000, le=1000)


class CanvasStyleResponse(BaseModel):
    id: UUID
    owner_firebase_uid: str
    name: str
    preview_url: str | None = None
    items: list[CanvasStyleItem] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ScheduleCanvasStyleRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    description: str | None = None
    location: str | None = None
    start_at: datetime
    end_at: datetime | None = None
    all_day: bool = False
