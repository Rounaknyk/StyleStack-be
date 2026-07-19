from datetime import date
from decimal import Decimal
import logging
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status

from app.core.config import get_settings
from app.core.supabase import get_supabase_client
from app.dependencies.auth import CurrentUser
from app.models.wardrobe import (
    TagStatusResponse,
    WardrobeItemResponse,
    WardrobeItemUpdate,
    WearLogCreate,
    WearLogResponse,
)
from app.models.ai_tags import ClothingDetection, ClothingTags
from app.services.ai_tagging import analyze_clothing_bytes, analyze_multiple_clothing_bytes
from app.services.background_jobs import ImageTaggingJob, background_jobs
from app.services.wardrobe import (
    add_signed_image_url,
    database_error,
    ensure_profile,
    get_owned_item,
)

router = APIRouter()
logger = logging.getLogger("stylestack.wardrobe")

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


@router.post("/analyze-image", response_model=ClothingTags)
async def analyze_image_preview(
    current_user: CurrentUser,
    image: Annotated[UploadFile, File(description="JPEG, PNG, or WebP; maximum 4 MB")],
) -> ClothingTags:
    content_type = (image.content_type or "").lower()
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Image must be JPEG, PNG, or WebP",
        )
    # Groq's base64 image request limit is 4 MB.
    contents = await image.read(4 * 1024 * 1024 + 1)
    await image.close()
    if not contents:
        raise HTTPException(status_code=422, detail="Uploaded image is empty")
    if len(contents) > 4 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Preview image exceeds the 4 MB AI limit")
    try:
        tags = analyze_clothing_bytes(contents, content_type)
        logger.info(
            "image_preview_analyzed uid=%s brand=%s category=%s",
            current_user["uid"],
            tags.brand,
            tags.category,
        )
        return tags
    except Exception as exc:
        logger.error(
            "image_preview_analysis_failed uid=%s error_type=%s",
            current_user["uid"],
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI could not analyze this image. You can still enter details manually.",
        ) from exc


@router.post("/detect-items", response_model=ClothingDetection)
async def detect_items_preview(
    current_user: CurrentUser,
    image: Annotated[UploadFile, File(description="JPEG, PNG, or WebP; maximum 4 MB")],
) -> ClothingDetection:
    """Return every wardrobe-relevant item visible in one photo."""
    content_type = (image.content_type or "").lower()
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=415, detail="Image must be JPEG, PNG, or WebP")
    contents = await image.read(4 * 1024 * 1024 + 1)
    await image.close()
    if not contents:
        raise HTTPException(status_code=422, detail="Uploaded image is empty")
    if len(contents) > 4 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Preview image exceeds the 4 MB AI limit")
    try:
        result = analyze_multiple_clothing_bytes(contents, content_type)
        logger.info(
            "image_items_detected uid=%s item_count=%s",
            current_user["uid"],
            len(result.items),
        )
        return result
    except Exception as exc:
        logger.error(
            "image_item_detection_failed uid=%s error_type=%s",
            current_user["uid"],
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=502,
            detail="AI could not detect items in this image. You can still add it manually.",
        ) from exc


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
    description: Annotated[str | None, Form(max_length=500)] = None,
    formality: Annotated[str | None, Form(max_length=50)] = None,
    ai_category: Annotated[str | None, Form(max_length=100)] = None,
    ai_color: Annotated[str | None, Form(max_length=100)] = None,
    ai_season: Annotated[str | None, Form(max_length=50)] = None,
    ai_formality: Annotated[str | None, Form(max_length=50)] = None,
    ai_description: Annotated[str | None, Form(max_length=500)] = None,
    ai_tags: Annotated[str | None, Form(description="Comma-separated AI tags")] = None,
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

    settings = get_settings()
    client = get_supabase_client()
    uid = current_user["uid"]
    ensure_profile(client, current_user)

    # Never trust the original filename for a storage path.
    suffix = ALLOWED_IMAGE_TYPES[content_type]
    image_path = f"{uid}/incoming/{uuid4().hex}{suffix}"
    bucket = client.storage.from_(settings.supabase_storage_bucket)

    try:
        bucket.upload(
            path=image_path,
            file=contents,
            file_options={"content-type": content_type, "upsert": "false"},
        )
    except Exception as exc:
        logger.error(
            "wardrobe_image_upload_failed uid=%s error_type=%s",
            uid,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Unable to upload the image to storage",
        ) from exc

    has_ai_preview = all(
        value and value.strip()
        for value in (ai_category, ai_color, ai_season, ai_formality, ai_description)
    )
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
        "description": description,
        "formality": formality,
        "notes": notes,
        "purchase_date": purchase_date.isoformat() if purchase_date else None,
        "purchase_price": str(purchase_price) if purchase_price is not None else None,
        "currency": currency.upper() if currency else None,
        "image_path": image_path,
        "is_favorite": is_favorite,
        "tagged": False,
        "ai_tag_status": "pending",
        "ai_category": ai_category.strip() if has_ai_preview else None,
        "ai_color": ai_color.strip() if has_ai_preview else None,
        "ai_season": ai_season.strip() if has_ai_preview else None,
        "ai_formality": ai_formality.strip() if has_ai_preview else None,
        "ai_description": ai_description.strip() if has_ai_preview else None,
        "ai_visual_tags": parse_csv(ai_tags) if has_ai_preview else [],
    }
    try:
        response = client.table("wardrobe_items").insert(item).execute()
        if not response.data:
            raise RuntimeError("Supabase returned no inserted wardrobe item")
        created_item = add_signed_image_url(client, response.data[0])
        logger.info(
            "wardrobe_item_created uid=%s item_id=%s category=%s",
            uid,
            created_item["id"],
            created_item["category"],
        )
        queued = background_jobs.enqueue(
            ImageTaggingJob(
                item_id=str(created_item["id"]),
                image_path=image_path,
                owner_uid=uid,
                category=category.strip(),
                skip_ai=has_ai_preview,
                generate_name=name.strip().lower().startswith("new wardrobe item"),
            )
        )
        if not queued:
            try:
                client.table("wardrobe_items").update(
                    {"ai_tag_status": "failed"}
                ).eq("id", str(created_item["id"])).execute()
            except Exception:
                logger.exception(
                    "background_queue_failure_status_update_failed item_id=%s",
                    created_item["id"],
                )
            created_item["ai_tag_status"] = "failed"
        return created_item
    except Exception as exc:
        # Compensating cleanup prevents an orphaned object if the DB insert fails.
        try:
            bucket.remove([image_path])
        except Exception:
            logger.warning("orphaned_image_cleanup_failed path=%s", image_path)
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
        items = [
            add_signed_image_url(get_supabase_client(), item)
            for item in (response.data or [])
        ]
        wear_counts: dict[str, int] = {}
        if items:
            try:
                item_ids = [str(item["id"]) for item in items]
                wear_response = (
                    get_supabase_client()
                    .table("wear_logs")
                    .select("wardrobe_item_id")
                    .eq("owner_firebase_uid", current_user["uid"])
                    .in_("wardrobe_item_id", item_ids)
                    .execute()
                )
                for wear_log in wear_response.data or []:
                    worn_item_id = str(wear_log["wardrobe_item_id"])
                    wear_counts[worn_item_id] = wear_counts.get(worn_item_id, 0) + 1
            except Exception:
                logger.warning(
                    "wardrobe_wear_counts_unavailable uid=%s", current_user["uid"]
                )
        for item in items:
            item["wear_count"] = wear_counts.get(str(item["id"]), 0)
        logger.debug(
            "wardrobe_items_listed uid=%s count=%s", current_user["uid"], len(items)
        )
        return items
    except HTTPException:
        raise
    except Exception as exc:
        raise database_error("list wardrobe items", exc) from exc


@router.get("/items/{item_id}", response_model=WardrobeItemResponse)
def read_wardrobe_item(item_id: UUID, current_user: CurrentUser) -> dict[str, Any]:
    item = get_owned_item(get_supabase_client(), str(item_id), current_user["uid"])
    logger.info("wardrobe_item_read uid=%s item_id=%s", current_user["uid"], item_id)
    return item


@router.get("/items/{item_id}/tag-status", response_model=TagStatusResponse)
def read_item_tag_status(
    item_id: UUID, current_user: CurrentUser
) -> TagStatusResponse:
    item = get_owned_item(
        get_supabase_client(), str(item_id), current_user["uid"]
    )
    return TagStatusResponse(status=item["ai_tag_status"])


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
        updated_item = add_signed_image_url(client, response.data[0])
        logger.info(
            "wardrobe_item_updated uid=%s item_id=%s fields=%s",
            uid,
            item_id,
            ",".join(sorted(updates)),
        )
        return updated_item
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
    storage_paths = list(
        dict.fromkeys(
            path
            for path in (item.get("image_path"), item.get("thumbnail_path"))
            if path
        )
    )
    if storage_paths:
        try:
            settings = get_settings()
            client.storage.from_(settings.supabase_storage_bucket).remove(
                storage_paths
            )
        except Exception:
            logger.warning(
                "wardrobe_image_delete_failed uid=%s item_id=%s",
                uid,
                item_id,
            )
    logger.info("wardrobe_item_deleted uid=%s item_id=%s", uid, item_id)


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
        created_log = response.data[0]
        logger.info(
            "wardrobe_wear_logged uid=%s item_id=%s wear_log_id=%s",
            uid,
            item_id,
            created_log["id"],
        )
        return created_log
    except Exception as exc:
        raise database_error("log the item wear", exc) from exc
