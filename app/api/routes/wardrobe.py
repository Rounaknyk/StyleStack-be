from datetime import date
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status

from app.core.config import get_settings
from app.core.supabase import get_supabase_client
from app.dependencies.auth import CurrentUser
from app.models.wardrobe import (
    WardrobeItemResponse,
    WardrobeItemUpdate,
    WearLogCreate,
    WearLogResponse,
)
from app.services.wardrobe import (
    add_signed_image_url,
    database_error,
    ensure_profile,
    get_owned_item,
)

router = APIRouter()

MAX_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return list(dict.fromkeys(entry.strip() for entry in value.split(",") if entry.strip()))


@router.post(
    "/items",
    response_model=WardrobeItemResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_wardrobe_item(
    current_user: CurrentUser,
    name: Annotated[str, Form(min_length=1, max_length=200)],
    category: Annotated[str, Form(min_length=1, max_length=100)],
    image: Annotated[UploadFile, File(description="JPEG, PNG, or WebP; maximum 10 MB")],
    subcategory: Annotated[str | None, Form(max_length=100)] = None,
    brand: Annotated[str | None, Form(max_length=100)] = None,
    color: Annotated[str | None, Form(max_length=100)] = None,
    size: Annotated[str | None, Form(max_length=50)] = None,
    season: Annotated[str | None, Form(description="Comma-separated seasons")] = None,
    tags: Annotated[str | None, Form(description="Comma-separated tags")] = None,
    notes: Annotated[str | None, Form(max_length=2000)] = None,
    purchase_date: Annotated[date | None, Form()] = None,
    purchase_price: Annotated[Decimal | None, Form(ge=0, max_digits=10, decimal_places=2)] = None,
    currency: Annotated[str | None, Form(min_length=3, max_length=3)] = None,
    is_favorite: Annotated[bool, Form()] = False,
) -> dict[str, Any]:
    content_type = (image.content_type or "").lower()
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Image must be JPEG, PNG, or WebP",
        )

    contents = await image.read(MAX_IMAGE_BYTES + 1)
    await image.close()
    if not contents:
        raise HTTPException(status_code=422, detail="Uploaded image is empty")
    if len(contents) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Image exceeds the 10 MB limit")

    client = get_supabase_client()
    settings = get_settings()
    uid = current_user["uid"]
    ensure_profile(client, current_user)

    # Never trust the original filename for a storage path.
    suffix = ALLOWED_IMAGE_TYPES[content_type]
    image_path = f"{uid}/{uuid4().hex}{suffix}"
    bucket = client.storage.from_(settings.supabase_storage_bucket)

    try:
        bucket.upload(
            path=image_path,
            file=contents,
            file_options={"content-type": content_type, "upsert": "false"},
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Unable to upload the image to storage",
        ) from exc

    item = {
        "owner_firebase_uid": uid,
        "name": name.strip(),
        "category": category.strip(),
        "subcategory": subcategory,
        "brand": brand,
        "color": color,
        "size": size,
        "season": parse_csv(season),
        "tags": parse_csv(tags),
        "notes": notes,
        "purchase_date": purchase_date.isoformat() if purchase_date else None,
        "purchase_price": str(purchase_price) if purchase_price is not None else None,
        "currency": currency.upper() if currency else None,
        "image_path": image_path,
        "is_favorite": is_favorite,
    }
    try:
        response = client.table("wardrobe_items").insert(item).execute()
        if not response.data:
            raise RuntimeError("Supabase returned no inserted wardrobe item")
        return add_signed_image_url(client, response.data[0])
    except Exception as exc:
        # Compensating cleanup prevents an orphaned object if the DB insert fails.
        try:
            bucket.remove([image_path])
        except Exception:
            pass
        raise database_error("create the wardrobe item", exc) from exc


@router.get("/items", response_model=list[WardrobeItemResponse])
def list_wardrobe_items(
    current_user: CurrentUser,
    category: Annotated[str | None, Query(max_length=100)] = None,
    brand: Annotated[str | None, Query(max_length=100)] = None,
    color: Annotated[str | None, Query(max_length=100)] = None,
    tag: Annotated[str | None, Query(max_length=100)] = None,
    is_favorite: bool | None = None,
    search: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    try:
        query = (
            get_supabase_client()
            .table("wardrobe_items")
            .select("*")
            .eq("owner_firebase_uid", current_user["uid"])
        )
        if category:
            query = query.eq("category", category)
        if brand:
            query = query.eq("brand", brand)
        if color:
            query = query.eq("color", color)
        if tag:
            query = query.contains("tags", [tag])
        if is_favorite is not None:
            query = query.eq("is_favorite", is_favorite)
        if search:
            # PostgREST escaping: wildcard characters are treated as wildcards.
            safe_search = search.replace(",", "").replace("(", "").replace(")", "")
            query = query.or_(f"name.ilike.%{safe_search}%,brand.ilike.%{safe_search}%")
        response = query.order("created_at", desc=True).range(
            offset, offset + limit - 1
        ).execute()
        return [
            add_signed_image_url(get_supabase_client(), item)
            for item in (response.data or [])
        ]
    except HTTPException:
        raise
    except Exception as exc:
        raise database_error("list wardrobe items", exc) from exc


@router.get("/items/{item_id}", response_model=WardrobeItemResponse)
def read_wardrobe_item(item_id: UUID, current_user: CurrentUser) -> dict[str, Any]:
    return get_owned_item(get_supabase_client(), str(item_id), current_user["uid"])


@router.put("/items/{item_id}", response_model=WardrobeItemResponse)
def update_wardrobe_item(
    item_id: UUID,
    payload: WardrobeItemUpdate,
    current_user: CurrentUser,
) -> dict[str, Any]:
    client = get_supabase_client()
    uid = current_user["uid"]
    get_owned_item(client, str(item_id), uid)
    updates = payload.model_dump(exclude_unset=True, mode="json")
    if not updates:
        raise HTTPException(status_code=422, detail="At least one field must be provided")

    try:
        response = (
            client.table("wardrobe_items")
            .update(updates)
            .eq("id", str(item_id))
            .eq("owner_firebase_uid", uid)
            .execute()
        )
        if not response.data:
            raise HTTPException(status_code=404, detail="Wardrobe item not found")
        return add_signed_image_url(client, response.data[0])
    except HTTPException:
        raise
    except Exception as exc:
        raise database_error("update the wardrobe item", exc) from exc


@router.delete("/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_wardrobe_item(item_id: UUID, current_user: CurrentUser) -> None:
    client = get_supabase_client()
    uid = current_user["uid"]
    item = get_owned_item(client, str(item_id), uid)

    try:
        response = (
            client.table("wardrobe_items")
            .delete()
            .eq("id", str(item_id))
            .eq("owner_firebase_uid", uid)
            .execute()
        )
        if not response.data:
            raise HTTPException(status_code=404, detail="Wardrobe item not found")
    except HTTPException:
        raise
    except Exception as exc:
        raise database_error("delete the wardrobe item", exc) from exc

    # The database deletion is authoritative; storage cleanup is best-effort.
    if item.get("image_path"):
        try:
            settings = get_settings()
            client.storage.from_(settings.supabase_storage_bucket).remove(
                [item["image_path"]]
            )
        except Exception:
            pass


@router.post(
    "/items/{item_id}/wear",
    response_model=WearLogResponse,
    status_code=status.HTTP_201_CREATED,
)
def log_item_wear(
    item_id: UUID,
    payload: WearLogCreate,
    current_user: CurrentUser,
) -> dict[str, Any]:
    client = get_supabase_client()
    uid = current_user["uid"]
    get_owned_item(client, str(item_id), uid)
    wear_log = {
        "wardrobe_item_id": str(item_id),
        "owner_firebase_uid": uid,
        **payload.model_dump(mode="json"),
    }
    try:
        response = client.table("wear_logs").insert(wear_log).execute()
        if not response.data:
            raise RuntimeError("Supabase returned no inserted wear log")
        return response.data[0]
    except Exception as exc:
        raise database_error("log the item wear", exc) from exc
