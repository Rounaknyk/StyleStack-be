from fastapi import APIRouter

from app.api.routes import users, wardrobe

api_router = APIRouter()
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(wardrobe.router, prefix="/wardrobe", tags=["wardrobe"])
