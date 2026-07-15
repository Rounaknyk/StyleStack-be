from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


GenderIdentity = Literal["woman", "man", "non_binary", "prefer_not_to_say"]
BodyType = Literal["slim", "average", "athletic", "curvy", "plus", "not_sure"]
StylePreference = Literal[
    "formal",
    "office",
    "casual",
    "sporty",
    "trendy",
    "ethnic",
    "minimal",
    "bohemian",
    "glam",
    "not_sure",
    "explore",
]
ShoppingFrequency = Literal[
    "every_week",
    "every_month",
    "every_2_3_months",
    "every_season",
    "rarely",
]
OnboardingGoal = Literal[
    "daily_outfit_ideas",
    "organize_wardrobe",
    "discover_personal_style",
    "reduce_decision_fatigue",
    "shop_less_style_better",
    "outfit_inspiration",
    "track_what_i_wear",
]


def _unique(values: list[str]) -> list[str]:
    """Preserve the user's order while preventing duplicate preferences."""
    return list(dict.fromkeys(values))


class OnboardingCompleteRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    gender_identity: GenderIdentity
    date_of_birth: date
    body_type: BodyType | None = None
    height_cm: int | None = Field(default=None, ge=90, le=230)
    style_preferences: list[StylePreference] = Field(default_factory=list, max_length=11)
    shopping_frequency: ShoppingFrequency | None = None
    onboarding_goals: list[OnboardingGoal] = Field(default_factory=list, max_length=7)

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Display name cannot be blank")
        return value

    @field_validator("date_of_birth")
    @classmethod
    def validate_date_of_birth(cls, value: date) -> date:
        today = date.today()
        if value > today:
            raise ValueError("Date of birth cannot be in the future")
        try:
            oldest_allowed = today.replace(year=today.year - 120)
        except ValueError:
            # February 29 is not available in every target year.
            oldest_allowed = today.replace(year=today.year - 120, day=28)
        if value < oldest_allowed:
            raise ValueError("Date of birth must be within the last 120 years")
        return value

    @field_validator("style_preferences", "onboarding_goals")
    @classmethod
    def remove_duplicates(cls, values: list[str]) -> list[str]:
        values = _unique(values)
        if "not_sure" in values and len(values) > 1:
            raise ValueError("not_sure cannot be combined with other selections")
        if "explore" in values and len(values) > 1:
            raise ValueError("explore cannot be combined with other selections")
        return values


class OnboardingProfileResponse(BaseModel):
    display_name: str | None = None
    gender_identity: GenderIdentity | None = None
    date_of_birth: date | None = None
    body_type: BodyType | None = None
    height_cm: int | None = None
    style_preferences: list[StylePreference] = Field(default_factory=list)
    shopping_frequency: ShoppingFrequency | None = None
    onboarding_goals: list[OnboardingGoal] = Field(default_factory=list)
    onboarding_completed: bool = False
    onboarding_completed_at: datetime | None = None
    onboarding_version: int = 1
