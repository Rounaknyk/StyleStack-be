from fastapi import APIRouter

from app.api.routes import calendar, canvas, imports, outfit_selfies, outfits, users, wardrobe

api_router = APIRouter()
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(wardrobe.router, prefix="/wardrobe", tags=["wardrobe"])
api_router.include_router(
    outfit_selfies.router,
    prefix="/wardrobe/outfit-selfies",
    tags=["outfit selfies"],
)
api_router.include_router(outfits.router, prefix="/outfits", tags=["outfits"])
api_router.include_router(imports.router, prefix="/imports", tags=["imports"])
api_router.include_router(calendar.router, prefix="/calendar", tags=["calendar"])
api_router.include_router(canvas.router, prefix="/canvas", tags=["canvas styles"])
