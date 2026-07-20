import asyncio
from datetime import date, datetime, timezone
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
    WearHistoryEntry,
    WearLogCreate,
    WearLogResponse,
)
from app.models.ai_tags import ClothingDetection, ClothingTags
from app.services.ai_request_queue import AiRequestJob, ai_request_queue
from app.services.background_jobs import ImageTaggingJob, background_jobs
from app.services.image_fingerprint import perceptual_hash
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


def _cached_analysis(
    image_hash: str, kind: str
) -> dict[str, Any] | None:
    client = get_supabase_client()
    try:
        response = (
            client.table("ai_image_analysis_cache")
            .select("analysis,hit_count")
            .eq("image_hash", image_hash)
            .eq("analysis_kind", kind)
            .limit(1)
            .execute()
        )
        if not response.data:
            return None
        row = response.data[0]
        try:
            client.table("ai_image_analysis_cache").update(
                {
                    "hit_count": int(row.get("hit_count") or 0) + 1,
                    "last_used_at": datetime.now(timezone.utc).isoformat(),
                }
            ).eq("image_hash", image_hash).eq("analysis_kind", kind).execute()
        except Exception:
            logger.warning("ai_analysis_cache_hit_count_failed hash=%s", image_hash)
        analysis = row.get("analysis")
        return analysis if isinstance(analysis, dict) else None
    except Exception as exc:
        logger.warning(
            "ai_analysis_cache_lookup_failed hash=%s error_type=%s",
            image_hash,
            type(exc).__name__,
        )
        return None


def _job_response(job: AiRequestJob) -> dict[str, Any]:
    return ai_request_queue.snapshot(job)


def _group_wear_history(
    logs: list[dict[str, Any]],
    items_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for log in logs:
        worn_at = str(log.get("worn_at") or "")
        notes = str(log.get("notes") or "")
        key = (worn_at, notes)
        group = groups.setdefault(
            key,
            {
                "id": str(log.get("id") or f"{worn_at}:{notes}"),
                "worn_at": worn_at,
                "notes": log.get("notes"),
                "items": [],
                "_item_ids": set(),
            },
        )
        item_id = str(log.get("wardrobe_item_id") or "")
        item = items_by_id.get(item_id)
        if item is not None and item_id not in group["_item_ids"]:
            group["_item_ids"].add(item_id)
            group["items"].append(item)

    entries: list[dict[str, Any]] = []
    for group in groups.values():
        group.pop("_item_ids", None)
        if group["items"]:
            entries.append(group)
    return entries


async def _read_ai_image(image: UploadFile) -> tuple[bytes, str]:
    content_type = (image.content_type or "").lower()
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=415, detail="Image must be JPEG, PNG, or WebP")
    contents = await image.read(4 * 1024 * 1024 + 1)
    await image.close()
    if not contents:
        raise HTTPException(status_code=422, detail="Uploaded image is empty")
    if len(contents) > 4 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Preview image exceeds the 4 MB AI limit")
    return contents, content_type


def _enqueue_or_reuse(
    *, uid: str, kind: str, contents: bytes, content_type: str
) -> AiRequestJob:
    image_hash = perceptual_hash(contents)
    cached = _cached_analysis(image_hash, kind)
    if cached is not None:
        return ai_request_queue.completed_from_cache(
            owner_uid=uid,
            kind=kind,  # type: ignore[arg-type]
            image_hash=image_hash,
            result=cached,
        )
    try:
        return ai_request_queue.enqueue(
            owner_uid=uid,
            kind=kind,  # type: ignore[arg-type]
            image=contents,
            content_type=content_type,
            image_hash=image_hash,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


async def _await_analysis_job(job: AiRequestJob) -> dict[str, Any]:
    while job.state in ("queued", "processing"):
        await asyncio.sleep(0.25)
    if job.state == "completed" and job.result is not None:
        return job.result
    raise HTTPException(
        status_code=502,
        detail=job.error or "AI could not analyze this image.",
    )


@router.post("/analysis-jobs", status_code=status.HTTP_202_ACCEPTED)
async def create_analysis_job(
    current_user: CurrentUser,
    image: Annotated[UploadFile, File(description="JPEG, PNG, or WebP; maximum 4 MB")],
    kind: Annotated[str, Form(pattern="^(single|multiple)$")] = "single",
) -> dict[str, Any]:
    contents, content_type = await _read_ai_image(image)
    job = _enqueue_or_reuse(
        uid=current_user["uid"],
        kind=kind,
        contents=contents,
        content_type=content_type,
    )
    return _job_response(job)


@router.get("/analysis-jobs/{job_id}")
def read_analysis_job(
    job_id: str,
    current_user: CurrentUser,
) -> dict[str, Any]:
    job = ai_request_queue.get(job_id, current_user["uid"])
    if not job:
        raise HTTPException(status_code=404, detail="Analysis job not found")
    return _job_response(job)


@router.delete("/analysis-jobs/{job_id}")
def cancel_analysis_job(
    job_id: str,
    current_user: CurrentUser,
) -> dict[str, Any]:
    job = ai_request_queue.cancel(job_id, current_user["uid"])
    if not job:
        raise HTTPException(status_code=404, detail="Analysis job not found")
    if job.state == "processing":
        raise HTTPException(status_code=409, detail="Analysis is already processing")
    return _job_response(job)


@router.post("/analysis-jobs/{job_id}/retry")
def retry_analysis_job(
    job_id: str,
    current_user: CurrentUser,
) -> dict[str, Any]:
    job = ai_request_queue.retry(job_id, current_user["uid"])
    if not job:
        raise HTTPException(status_code=404, detail="Analysis job not found")
    if job.state == "failed":
        raise HTTPException(status_code=409, detail="This job can no longer be retried")
    return _job_response(job)


def parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return list(dict.fromkeys(entry.strip() for entry in value.split(",") if entry.strip()))


@router.post("/analyze-image", response_model=ClothingTags)
async def analyze_image_preview(
    current_user: CurrentUser,
    image: Annotated[UploadFile, File(description="JPEG, PNG, or WebP; maximum 4 MB")],
) -> ClothingTags:
    contents, content_type = await _read_ai_image(image)
    try:
        job = _enqueue_or_reuse(
            uid=current_user["uid"],
            kind="single",
            contents=contents,
            content_type=content_type,
        )
        tags = ClothingTags.model_validate(await _await_analysis_job(job))
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
    contents, content_type = await _read_ai_image(image)
    try:
        job = _enqueue_or_reuse(
            uid=current_user["uid"],
            kind="multiple",
            contents=contents,
            content_type=content_type,
        )
        result = ClothingDetection.model_validate(await _await_analysis_job(job))
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


@router.get("/wear-history", response_model=list[WearHistoryEntry])
def list_wear_history(
    current_user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> list[dict[str, Any]]:
    client = get_supabase_client()
    uid = current_user["uid"]
    try:
        wear_response = (
            client.table("wear_logs")
            .select("*")
            .eq("owner_firebase_uid", uid)
            .order("worn_at", desc=True)
            .limit(min(limit * 8, 300))
            .execute()
        )
        logs = wear_response.data or []
        if not logs:
            return []

        item_ids = list(
            dict.fromkeys(
                str(log["wardrobe_item_id"])
                for log in logs
                if log.get("wardrobe_item_id")
            )
        )
        item_response = (
            client.table("wardrobe_items")
            .select("*")
            .eq("owner_firebase_uid", uid)
            .in_("id", item_ids)
            .execute()
        )
        items_by_id = {
            str(item["id"]): add_signed_image_url(client, item)
            for item in (item_response.data or [])
        }
        entries = _group_wear_history(logs, items_by_id)[:limit]
        logger.debug(
            "wardrobe_wear_history_listed uid=%s count=%s", uid, len(entries)
        )
        return entries
    except Exception as exc:
        raise database_error("list wear history", exc) from exc


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


@router.post("/items/{item_id}/retry-processing", response_model=WardrobeItemResponse)
def retry_wardrobe_item_processing(
    item_id: UUID, current_user: CurrentUser
) -> dict[str, Any]:
    client = get_supabase_client()
    uid = current_user["uid"]
    item = get_owned_item(client, str(item_id), uid)
    if item["ai_tag_status"] in ("pending", "processing"):
        raise HTTPException(status_code=409, detail="This item is already processing")
    image_path = item.get("image_path")
    if not image_path:
        raise HTTPException(
            status_code=422,
            detail="This item has no saved image to analyze. Enter details manually.",
        )
    try:
        background_jobs.retry_ai_tagging(
            ImageTaggingJob(
                item_id=str(item_id),
                image_path=str(image_path),
                owner_uid=uid,
                category=item.get("category"),
                generate_name=item.get("name") == "New wardrobe item",
            )
        )
        refreshed = get_owned_item(client, str(item_id), uid)
        return refreshed
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "wardrobe_item_retry_failed uid=%s item_id=%s error_type=%s",
            uid,
            item_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Could not queue this item right now. Please try again shortly.",
        ) from exc


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
