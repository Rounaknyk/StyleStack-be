from fastapi import APIRouter
from pydantic import BaseModel

from app.dependencies.auth import CurrentUser

router = APIRouter()


class CurrentUserResponse(BaseModel):
    user_id: str


@router.get("/me", response_model=CurrentUserResponse)
def read_current_user(current_user: CurrentUser) -> CurrentUserResponse:
    """Return the Firebase UID represented by the caller's ID token."""
    return CurrentUserResponse(user_id=current_user["uid"])

