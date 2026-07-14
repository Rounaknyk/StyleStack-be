import logging
from datetime import datetime, timezone
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.supabase import get_supabase_client
from app.dependencies.auth import CurrentUser
from app.models.outfit_selfie import (
    OutfitSelfieAnalysisResponse,
    OutfitSelfieConfirmation,
    OutfitSelfieConfirmationResponse,
    OutfitSelfieHistoryEntry,
)
from app.services.ai_tagging import analyze_outfit_selfie_bytes
from app.services.wardrobe import add_signed_image_url, database_error, ensure_profile

router = APIRouter()
logger = logging.getLogger("stylestack.outfit_selfie")

MAX_AI_IMAGE_BYTES = 4 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def _signed_selfie_url(client: Any, image_path: str) -> str | None:
    try:
        signed = client.storage.from_(
            get_settings().supabase_storage_bucket
        ).create_signed_url(image_path, 3600)
        if isinstance(signed, dict):
            return signed.get("signedURL") or signed.get("signedUrl")
    except Exception:
        logger.warning("outfit_selfie_url_signing_failed path=%s", image_path)
    return None


def _candidate(item: dict[str, Any]) -> dict[str, object]:
    return {
        "id": str(item["id"]),
        "name": item.get("name"),
        "category": item.get("category") or item.get("ai_category"),
        "color": item.get("color") or item.get("ai_color"),
        "description": item.get("description") or item.get("ai_description"),
        "tags": item.get("tags") or [],
        "visual_tags": item.get("ai_visual_tags") or [],
    }


def _detection_response(
    client: Any,
    detection: dict[str, Any],
    owned_items: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    item_id = detection.get("wardrobe_item_id")
    item = owned_items.get(str(item_id)) if item_id else None
    return {
        **detection,
        "wardrobe_item": add_signed_image_url(client, item) if item else None,
    }


@router.post("/analyze", response_model=OutfitSelfieAnalysisResponse)
async def analyze_outfit_selfie(
    current_user: CurrentUser,
    image: Annotated[UploadFile, File(description="Full-body JPEG, PNG, or WebP")],
) -> dict[str, Any]:
    content_type = (image.content_type or "").lower()
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=415, detail="Selfie must be JPEG, PNG, or WebP")
    contents = await image.read(MAX_AI_IMAGE_BYTES + 1)
    await image.close()
    if not contents:
        raise HTTPException(status_code=422, detail="The selfie is empty")
    if len(contents) > MAX_AI_IMAGE_BYTES:
        raise HTTPException(
            status_code=413,
            detail="The selfie is too large. Retake it at a lower resolution.",
        )

    client = get_supabase_client()
    uid = current_user["uid"]
    ensure_profile(client, current_user)
    try:
        wardrobe_response = (
            client.table("wardrobe_items")
            .select("*")
            .eq("owner_firebase_uid", uid)
            .order("updated_at", desc=True)
            .limit(150)
            .execute()
        )
        wardrobe = wardrobe_response.data or []
    except Exception as exc:
        raise database_error("load wardrobe candidates", exc) from exc

    try:
        result = await run_in_threadpool(
            analyze_outfit_selfie_bytes,
            contents,
            content_type,
            [_candidate(item) for item in wardrobe],
        )
    except Exception as exc:
        logger.exception(
            "outfit_selfie_analysis_failed uid=%s error_type=%s message=%s",
            uid,
            type(exc).__name__,
            str(exc)[:200],
        )
        raise HTTPException(
            status_code=502,
            detail="StyleStack could not analyze this selfie. Please try again.",
        ) from exc

    if not result.quality_acceptable or not result.items:
        logger.info(
            "outfit_selfie_retake_requested uid=%s quality=%.2f",
            uid,
            result.quality_score,
        )
        return {
            "quality_acceptable": False,
            "quality_score": result.quality_score,
            "quality_feedback": result.quality_feedback,
            "detections": [],
        }

    owned_items = {str(item["id"]): item for item in wardrobe}
    selfie_id = str(uuid4())
    suffix = ALLOWED_IMAGE_TYPES[content_type]
    image_path = f"{uid}/outfit-selfies/{selfie_id}{suffix}"
    bucket = client.storage.from_(get_settings().supabase_storage_bucket)
    try:
        bucket.upload(
            path=image_path,
            file=contents,
            file_options={"content-type": content_type, "upsert": "false"},
        )
        selfie_response = client.table("outfit_selfies").insert(
            {
                "id": selfie_id,
                "owner_firebase_uid": uid,
                "image_path": image_path,
                "quality_score": result.quality_score,
                "quality_feedback": result.quality_feedback,
            }
        ).execute()
        if not selfie_response.data:
            raise RuntimeError("Supabase returned no outfit selfie")

        rows: list[dict[str, Any]] = []
        for detected in result.items:
            matched_id = detected.matched_item_id
            if matched_id not in owned_items:
                matched_id = None
            rows.append(
                {
                    "outfit_selfie_id": selfie_id,
                    "wardrobe_item_id": matched_id,
                    "detected_name": detected.detected_name,
                    "detected_category": detected.category,
                    "detected_color": detected.color,
                    "detected_description": detected.description,
                    "visual_tags": detected.visual_tags,
                    "confidence": detected.confidence if matched_id else min(detected.confidence, 0.45),
                    "selected": True,
                }
            )
        detections_response = client.table("outfit_selfie_detections").insert(rows).execute()
        detections = detections_response.data or []
    except Exception as exc:
        try:
            client.table("outfit_selfies").delete().eq("id", selfie_id).execute()
            bucket.remove([image_path])
        except Exception:
            logger.warning("outfit_selfie_cleanup_failed selfie_id=%s", selfie_id)
        raise database_error("save the outfit selfie", exc) from exc

    logger.info(
        "outfit_selfie_ready_for_review uid=%s selfie_id=%s detections=%s",
        uid,
        selfie_id,
        len(detections),
    )
    return {
        "quality_acceptable": True,
        "quality_score": result.quality_score,
        "quality_feedback": result.quality_feedback,
        "selfie_id": selfie_id,
        "image_url": _signed_selfie_url(client, image_path),
        "detections": [
            _detection_response(client, detection, owned_items)
            for detection in detections
        ],
    }


@router.post(
    "/{selfie_id}/confirm",
    response_model=OutfitSelfieConfirmationResponse,
)
def confirm_outfit_selfie(
    selfie_id: UUID,
    payload: OutfitSelfieConfirmation,
    current_user: CurrentUser,
) -> dict[str, Any]:
    client = get_supabase_client()
    uid = current_user["uid"]
    try:
        selfie_response = (
            client.table("outfit_selfies")
            .select("*")
            .eq("id", str(selfie_id))
            .eq("owner_firebase_uid", uid)
            .limit(1)
            .execute()
        )
        if not selfie_response.data:
            raise HTTPException(status_code=404, detail="Outfit selfie not found")
        selfie = selfie_response.data[0]
        if selfie["status"] == "confirmed":
            raise HTTPException(
                status_code=409, detail="This outfit selfie is already confirmed"
            )

        detection_response = (
            client.table("outfit_selfie_detections")
            .select("*")
            .eq("outfit_selfie_id", str(selfie_id))
            .execute()
        )
        stored = {str(row["id"]): row for row in detection_response.data or []}
        supplied_ids = {str(choice.detection_id) for choice in payload.detections}
        if not supplied_ids.issubset(stored):
            raise HTTPException(
                status_code=422, detail="One or more detections are invalid"
            )

        chosen_item_ids = {
            str(choice.wardrobe_item_id)
            for choice in payload.detections
            if choice.selected and choice.wardrobe_item_id is not None
        }
        owned_ids: set[str] = set()
        if chosen_item_ids:
            owned_response = (
                client.table("wardrobe_items")
                .select("id")
                .eq("owner_firebase_uid", uid)
                .in_("id", list(chosen_item_ids))
                .execute()
            )
            owned_ids = {str(row["id"]) for row in owned_response.data or []}
        if chosen_item_ids != owned_ids:
            raise HTTPException(
                status_code=422, detail="A selected wardrobe item is invalid"
            )

        unmatched: list[str] = []
        for choice in payload.detections:
            row = stored[str(choice.detection_id)]
            item_id = str(choice.wardrobe_item_id) if choice.wardrobe_item_id else None
            client.table("outfit_selfie_detections").update(
                {"selected": choice.selected, "wardrobe_item_id": item_id}
            ).eq("id", str(choice.detection_id)).execute()
            if choice.selected and item_id is None:
                unmatched.append(row["detected_name"])

        wear_rows = [
            {
                "wardrobe_item_id": item_id,
                "owner_firebase_uid": uid,
                "notes": f"Logged from outfit selfie {selfie_id}",
            }
            for item_id in sorted(chosen_item_ids)
        ]
        if wear_rows:
            client.table("wear_logs").insert(wear_rows).execute()
        client.table("outfit_selfies").update(
            {
                "status": "confirmed",
                "confirmed_at": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("id", str(selfie_id)).eq("owner_firebase_uid", uid).execute()
    except HTTPException:
        raise
    except Exception as exc:
        raise database_error("confirm the outfit selfie", exc) from exc

    logger.info(
        "outfit_selfie_confirmed uid=%s selfie_id=%s logged_items=%s unmatched=%s",
        uid,
        selfie_id,
        len(chosen_item_ids),
        len(unmatched),
    )
    return {
        "selfie_id": selfie_id,
        "status": "confirmed",
        "logged_items": len(chosen_item_ids),
        "unmatched_items": unmatched,
    }


@router.delete("/{selfie_id}", status_code=status.HTTP_204_NO_CONTENT)
def discard_outfit_selfie(
    selfie_id: UUID,
    current_user: CurrentUser,
) -> None:
    """Remove an unconfirmed selfie when the user cancels or retakes."""
    client = get_supabase_client()
    uid = current_user["uid"]
    try:
        response = (
            client.table("outfit_selfies")
            .select("id,image_path,status")
            .eq("id", str(selfie_id))
            .eq("owner_firebase_uid", uid)
            .limit(1)
            .execute()
        )
        if not response.data:
            return
        selfie = response.data[0]
        if selfie["status"] == "confirmed":
            raise HTTPException(
                status_code=409,
                detail="Confirmed outfit history cannot be discarded as a draft",
            )
        client.table("outfit_selfies").delete().eq("id", str(selfie_id)).eq(
            "owner_firebase_uid", uid
        ).execute()
        image_path = selfie.get("image_path")
        if image_path:
            try:
                client.storage.from_(
                    get_settings().supabase_storage_bucket
                ).remove([image_path])
            except Exception:
                logger.warning(
                    "outfit_selfie_storage_cleanup_failed selfie_id=%s", selfie_id
                )
    except HTTPException:
        raise
    except Exception as exc:
        raise database_error("discard the outfit selfie", exc) from exc

    logger.info("outfit_selfie_discarded uid=%s selfie_id=%s", uid, selfie_id)


@router.get("/history", response_model=list[OutfitSelfieHistoryEntry])
def outfit_selfie_history(
    current_user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> list[dict[str, Any]]:
    client = get_supabase_client()
    uid = current_user["uid"]
    try:
        selfie_response = (
            client.table("outfit_selfies")
            .select("*")
            .eq("owner_firebase_uid", uid)
            .eq("status", "confirmed")
            .order("captured_at", desc=True)
            .limit(limit)
            .execute()
        )
        selfies = selfie_response.data or []
        if not selfies:
            return []
        selfie_ids = [str(row["id"]) for row in selfies]
        detection_response = (
            client.table("outfit_selfie_detections")
            .select("outfit_selfie_id,wardrobe_item_id,selected")
            .in_("outfit_selfie_id", selfie_ids)
            .eq("selected", True)
            .execute()
        )
        detections = detection_response.data or []
        item_ids = list(
            {
                str(row["wardrobe_item_id"])
                for row in detections
                if row.get("wardrobe_item_id")
            }
        )
        items: dict[str, dict[str, Any]] = {}
        if item_ids:
            item_response = (
                client.table("wardrobe_items")
                .select("*")
                .eq("owner_firebase_uid", uid)
                .in_("id", item_ids)
                .execute()
            )
            items = {
                str(row["id"]): add_signed_image_url(client, row)
                for row in item_response.data or []
            }
        items_by_selfie: dict[str, list[dict[str, Any]]] = {}
        for row in detections:
            item = items.get(str(row.get("wardrobe_item_id")))
            if item:
                items_by_selfie.setdefault(str(row["outfit_selfie_id"]), []).append(
                    item
                )
        return [
            {
                "id": selfie["id"],
                "image_url": _signed_selfie_url(client, selfie["image_path"]),
                "captured_at": selfie["captured_at"],
                "confirmed_at": selfie.get("confirmed_at"),
                "items": items_by_selfie.get(str(selfie["id"]), []),
            }
            for selfie in selfies
        ]
    except Exception as exc:
        raise database_error("load outfit selfie history", exc) from exc
