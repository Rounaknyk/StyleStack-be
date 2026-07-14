from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class CalendarEventCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    location: str | None = Field(default=None, max_length=300)
    start_at: datetime
    end_at: datetime | None = None
    all_day: bool = False
    occasion: str = Field(default="event", min_length=2, max_length=80)


class GoogleCalendarConnectRequest(BaseModel):
    server_auth_code: str = Field(min_length=10)
    email: str | None = Field(default=None, max_length=320)


class GoogleCalendarConnectionResponse(BaseModel):
    connected: bool
    email: str | None = None
    last_synced_at: datetime | None = None
    imported: int = 0


class CalendarEventResponse(BaseModel):
    id: UUID
    source: str
    title: str
    description: str | None = None
    location: str | None = None
    start_at: datetime
    end_at: datetime | None = None
    all_day: bool
    occasion: str
    outfit_id: UUID | None = None
    created_at: datetime


class CalendarSyncResponse(BaseModel):
    imported: int


class AppNotificationResponse(BaseModel):
    id: UUID
    type: str
    title: str
    body: str
    data: dict[str, Any] = Field(default_factory=dict)
    read_at: datetime | None = None
    created_at: datetime
