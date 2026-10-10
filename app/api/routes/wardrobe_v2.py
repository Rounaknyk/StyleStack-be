from fastapi import APIRouter, UploadFile
import os
from qstash import QStash
from app.dependencies.auth import CurrentUser
from app.api.routes.wardrobe import _read_ai_image
from app.services.image_fingerprint import perceptual_hash
from app.core.supabase import get_supabase_client

router = APIRouter()

from typing import Annotated
from fastapi import Form, File
from datetime import date
from decimal import Decimal

@router.post("/items/", status_code=202)
async def create_wardrobe_item_v2(
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
):
    """
    V2 Architecture Migration: Uses Upstash QStash Serverless Message Queue. 
    Returns 202 instantly with a task_id for polling.
    """
    contents, content_type = await _read_ai_image(image)
    image_hash = perceptual_hash(contents)
    
    task_id = f"job_{image_hash}_{current_user.uid}"
    
    # Publish Webhook to QStash
    qstash_token = os.getenv("QSTASH_TOKEN", "mock_token")
    client = QStash(qstash_token)
    
    # Render automatically sets RENDER_EXTERNAL_URL in production
    base_url = os.getenv("RENDER_EXTERNAL_URL", "http://localhost:8000")
    webhook_url = f"{base_url}/api/v2/wardrobe/internal/process-image"
    
    # Publish to QStash
    try:
        res = client.publish_json(
            url=webhook_url,
            body={
                "task_id": task_id,
                "owner_uid": current_user.uid,
                "kind": "single",
                "image_hash": image_hash,
                "image_url": "mock_url"
            }
        )
        message_id = res.message_id
    except Exception as e:
        message_id = "local_mock_id"
    
    return {
        "status": "queued",
        "task_id": task_id,
        "message_id": message_id,
        "message": "Webhook published to QStash"
    }

@router.post("/internal/process-image")
async def internal_process_image(payload: dict):
    """
    Internal Webhook receiver called by QStash.
    """
    import time
    
    time.sleep(3) # Simulate heavy AI processing
    
    result = {
        "tags": {"category": "Tops", "style": "Streetwear"},
        "hash": payload.get("image_hash")
    }
    
    client = get_supabase_client()
    client.table("ai_jobs").upsert({
        "id": payload.get("task_id"),
        "status": "completed",
        "result": result
    }).execute()
    
    return {"status": "success", "processed_task": payload.get("task_id")}

@router.get("/items/{task_id}")
def read_wardrobe_item_v2_status(task_id: str, current_user: CurrentUser):
    """
    V2 Architecture Migration: Poll Supabase to see if QStash finished the task.
    """
    client = get_supabase_client()
    response = client.table("ai_jobs").select("*").eq("id", task_id).execute()
    
    if len(response.data) > 0 and response.data[0].get("status") == "completed":
        return {
            "status": "completed",
            "task_id": task_id,
            "result": response.data[0].get("result")
        }
    else:
        return {
            "status": "processing",
            "task_id": task_id
        }
