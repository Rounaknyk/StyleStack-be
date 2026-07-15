import json
import logging
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from app.core.config import get_settings
from app.core.supabase import get_supabase_client
from app.dependencies.auth import CurrentUser
from app.models.canvas import CanvasStyleItem, CanvasStyleResponse

router = APIRouter()
logger = logging.getLogger("stylestack.canvas")
MAX_PREVIEW_BYTES = 8 * 1024 * 1024


def _signed(style: dict[str, Any], client: Any) -> dict[str, Any]:
    result = dict(style)
    result["preview_url"] = None
    path = result.get("preview_path")
    if path:
        try:
            signed = client.storage.from_(get_settings().supabase_storage_bucket).create_signed_url(path, 3600)
            if isinstance(signed, dict):
                result["preview_url"] = signed.get("signedURL") or signed.get("signedUrl")
        except Exception:
            logger.warning("canvas_preview_sign_failed style_id=%s", style.get("id"))
    return result


def _parse_items(raw: str) -> list[CanvasStyleItem]:
    try:
        value = json.loads(raw)
        if not isinstance(value, list):
            raise ValueError("items must be an array")
        return [CanvasStyleItem.model_validate(item) for item in value]
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="items must be valid canvas JSON") from exc


@router.post("/styles", response_model=CanvasStyleResponse, status_code=status.HTTP_201_CREATED)
async def create_canvas_style(
    current_user: CurrentUser,
    name: Annotated[str, Form(min_length=1, max_length=120)],
    items: Annotated[str, Form(description="JSON array of positioned wardrobe items")],
    preview_image: Annotated[UploadFile, File(description="PNG canvas preview")],
) -> dict[str, Any]:
    parsed_items = _parse_items(items)
    if not parsed_items:
        raise HTTPException(status_code=422, detail="Add at least one item to the canvas")
    content_type = (preview_image.content_type or "").lower()
    if content_type not in {"image/png", "image/jpeg", "image/webp"}:
        raise HTTPException(status_code=415, detail="Preview must be PNG, JPEG, or WebP")
    preview_bytes = await preview_image.read(MAX_PREVIEW_BYTES + 1)
    await preview_image.close()
    if not preview_bytes:
        raise HTTPException(status_code=422, detail="Preview image is empty")
    if len(preview_bytes) > MAX_PREVIEW_BYTES:
        raise HTTPException(status_code=413, detail="Preview image exceeds 8 MB")

    client = get_supabase_client()
    uid = current_user["uid"]
    ids = list(dict.fromkeys(str(item.item_id) for item in parsed_items))
    owned = client.table("wardrobe_items").select("id").eq("owner_firebase_uid", uid).in_("id", ids).execute().data or []
    if {str(row["id"]) for row in owned} != set(ids):
        raise HTTPException(status_code=422, detail="Every canvas item must belong to your wardrobe")

    style_id = uuid4()
    extension = ".png" if content_type == "image/png" else ".jpg"
    preview_path = f"{uid}/canvas/{style_id}{extension}"
    bucket = client.storage.from_(get_settings().supabase_storage_bucket)
    try:
        bucket.upload(
            path=preview_path,
            file=preview_bytes,
            file_options={"content-type": content_type, "upsert": "false"},
        )
        response = client.table("canvas_styles").insert(
            {
                "id": str(style_id),
                "owner_firebase_uid": uid,
                "name": name.strip(),
                "preview_path": preview_path,
                "items": [item.model_dump(mode="json") for item in parsed_items],
            }
        ).execute()
        if not response.data:
            raise RuntimeError("Supabase returned no canvas style")
        logger.info("canvas_style_created uid=%s style_id=%s items=%s", uid, style_id, len(parsed_items))
        return _signed(response.data[0], client)
    except HTTPException:
        raise
    except Exception as exc:
        try:
            bucket.remove([preview_path])
        except Exception:
            logger.warning("canvas_preview_cleanup_failed path=%s", preview_path)
        logger.exception("canvas_style_create_failed uid=%s error_type=%s", uid, type(exc).__name__)
        raise HTTPException(status_code=502, detail="Could not save this style") from exc


@router.get("/styles", response_model=list[CanvasStyleResponse])
def list_canvas_styles(current_user: CurrentUser) -> list[dict[str, Any]]:
    client = get_supabase_client()
    rows = client.table("canvas_styles").select("*").eq("owner_firebase_uid", current_user["uid"]).order("created_at", desc=True).execute().data or []
    return [_signed(row, client) for row in rows]


@router.get("/styles/{style_id}", response_model=CanvasStyleResponse)
def read_canvas_style(style_id: UUID, current_user: CurrentUser) -> dict[str, Any]:
    client = get_supabase_client()
    rows = client.table("canvas_styles").select("*").eq("id", str(style_id)).eq("owner_firebase_uid", current_user["uid"]).limit(1).execute().data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Canvas style not found")
    return _signed(rows[0], client)


@router.delete("/styles/{style_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_canvas_style(style_id: UUID, current_user: CurrentUser) -> None:
    client = get_supabase_client()
    rows = client.table("canvas_styles").select("preview_path").eq("id", str(style_id)).eq("owner_firebase_uid", current_user["uid"]).limit(1).execute().data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Canvas style not found")
    client.table("canvas_styles").delete().eq("id", str(style_id)).eq("owner_firebase_uid", current_user["uid"]).execute()
    path = rows[0].get("preview_path")
    if path:
        try:
            client.storage.from_(get_settings().supabase_storage_bucket).remove([path])
        except Exception:
            logger.warning("canvas_preview_delete_failed style_id=%s", style_id)
