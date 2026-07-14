from datetime import time

from pydantic import BaseModel, Field


class UserPreferences(BaseModel):
    city: str | None = None
    timezone: str = "Asia/Kolkata"
    notification_enabled: bool = False
    notification_time: time = time(hour=8)


class UserPreferencesUpdate(BaseModel):
    city: str | None = Field(default=None, min_length=2, max_length=120)
    timezone: str | None = Field(default=None, max_length=80)
    notification_enabled: bool | None = None
    notification_time: time | None = None


class DeviceTokenRequest(BaseModel):
    token: str = Field(min_length=20, max_length=4096)
    platform: str = Field(default="unknown", max_length=20)


class TestNotificationResponse(BaseModel):
    success_count: int
    failure_count: int
