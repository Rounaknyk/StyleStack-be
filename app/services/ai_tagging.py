import base64
import io
import json
import logging

import httpx
from PIL import Image, ImageOps

from app.core.config import get_settings
from app.models.ai_tags import (
    ClothingDetection,
    ClothingTags,
    OutfitSelfieVisionResult,
)
from app.services.gemini import gemini_json_from_image

logger = logging.getLogger("stylestack.ai")


def _prepare_vision_image(
    image: bytes,
    content_type: str,
    *,
    max_dimension: int = 1280,
) -> tuple[bytes, str]:
    """Create a small RGB copy for AI calls without changing the stored original."""
    try:
        with Image.open(io.BytesIO(image)) as source:
            prepared = ImageOps.exif_transpose(source).convert("RGB")
            prepared.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            prepared.save(output, format="JPEG", quality=82, optimize=True)
            compressed = output.getvalue()
            if compressed:
                logger.info(
                    "vision_image_prepared original_bytes=%s ai_bytes=%s dimensions=%sx%s",
                    len(image),
                    len(compressed),
                    prepared.width,
                    prepared.height,
                )
                return compressed, "image/jpeg"
    except Exception as exc:
        logger.warning("vision_image_prepare_failed error_type=%s", type(exc).__name__)
    return image, content_type


def _log_provider_failure(provider: str, exc: Exception) -> None:
    status_code = None
    detail = None
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        try:
            payload = exc.response.json()
            error = payload.get("error", payload) if isinstance(payload, dict) else payload
            detail = str(error)[:300]
        except Exception:
            detail = exc.response.text[:300]
    logger.warning(
        "vision_provider_failed provider=%s error_type=%s status_code=%s detail=%s",
        provider,
        type(exc).__name__,
        status_code,
        detail,
    )

TAGGING_PROMPT = """Analyze the primary clothing item in this image and return ONLY valid JSON with these fields:
{
  "category": "shirt|pants|dress|jacket|shoes|accessory|other",
  "color": "black|white|red|blue|green|yellow|purple|pink|brown|grey|orange|beige|multicolor",
  "season": "summer|winter|spring|autumn|all",
  "formality": "formal|semi-formal|casual|sporty",
  "description": "brief description of the item",
  "tags": ["up to 5 concise useful style or material tags"],
  "visual_tags": ["up to 10 stable visual traits such as pattern, material, silhouette, neckline, sleeve and length"]
}"""

MULTI_ITEM_PROMPT = """Detect every clearly visible wardrobe-relevant item in this image.
Treat tops, pants, dresses, jackets, shoes, bags, jewelry and other wearable accessories as separate items.
Ignore people, furniture, packaging and background objects. Do not invent hidden items.
Return ONLY valid JSON in this exact shape:
{
  "items": [
    {
      "category": "shirt|pants|dress|jacket|shoes|accessory|other",
      "color": "black|white|red|blue|green|yellow|purple|pink|brown|grey|orange|beige|multicolor",
      "season": "summer|winter|spring|autumn|all",
      "formality": "formal|semi-formal|casual|sporty",
      "description": "brief specific description",
      "tags": ["up to 5 concise useful style or material tags"],
      "visual_tags": ["up to 10 stable visual traits"]
    }
  ]
}
Return at most 12 items, ordered by prominence. If the image contains one garment, return one item."""


def analyze_clothing_image(image_url: str) -> ClothingTags:
    """Analyze one image with Groq, falling back to Gemini."""
    settings = get_settings()
    groq_error: Exception | None = None
    if settings.groq_api_key:
        try:
            response = httpx.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.groq_api_key}",
                    "Content-Type": "application/json",
                },
                json={
            "model": settings.groq_vision_model,
            "reasoning_effort": "none",
            "messages": [
                {
                    "role": "system",
                    "content": "You classify clothing images. Return only the requested JSON object with no markdown or commentary.",
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": TAGGING_PROMPT},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.7,
            "top_p": 0.8,
            "presence_penalty": 1.5,
            "max_completion_tokens": 512,
                },
                timeout=settings.groq_request_timeout_seconds,
            )
            response.raise_for_status()
            return ClothingTags.model_validate_json(
                response.json()["choices"][0]["message"]["content"]
            )
        except Exception as exc:
            groq_error = exc
    if settings.gemini_api_key:
        if image_url.startswith("data:"):
            header, encoded = image_url.split(",", 1)
            content_type = header.split(";", 1)[0].removeprefix("data:")
            image = base64.b64decode(encoded)
        else:
            downloaded = httpx.get(image_url, timeout=30, follow_redirects=True)
            downloaded.raise_for_status()
            image = downloaded.content
            content_type = downloaded.headers.get("content-type", "image/jpeg").split(";")[0]
        return ClothingTags.model_validate_json(
            gemini_json_from_image(TAGGING_PROMPT, image, content_type)
        )
    if groq_error:
        raise groq_error
    raise RuntimeError("No AI vision provider is configured")


def analyze_clothing_bytes(image: bytes, content_type: str) -> ClothingTags:
    """Analyze an upload preview before the wardrobe item is created."""
    encoded = base64.b64encode(image).decode("ascii")
    return analyze_clothing_image(f"data:{content_type};base64,{encoded}")


def analyze_multiple_clothing_bytes(
    image: bytes, content_type: str
) -> ClothingDetection:
    """Detect multiple garments in one upload preview."""
    settings = get_settings()
    encoded = base64.b64encode(image).decode("ascii")
    groq_error: Exception | None = None
    if settings.groq_api_key:
        try:
            response = httpx.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {settings.groq_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": settings.groq_vision_model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a precise fashion catalog assistant. Return JSON only.",
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": MULTI_ITEM_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{content_type};base64,{encoded}"
                            },
                        },
                    ],
                },
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_completion_tokens": 1600,
        },
        timeout=settings.groq_request_timeout_seconds,
            )
            response.raise_for_status()
            return ClothingDetection.model_validate_json(
                response.json()["choices"][0]["message"]["content"]
            )
        except Exception as exc:
            groq_error = exc
    if settings.gemini_api_key:
        return ClothingDetection.model_validate_json(
            gemini_json_from_image(MULTI_ITEM_PROMPT, image, content_type)
        )
    if groq_error:
        raise groq_error
    raise RuntimeError("No AI vision provider is configured")


def analyze_outfit_selfie_bytes(
    image: bytes,
    content_type: str,
    wardrobe_candidates: list[dict[str, object]],
) -> OutfitSelfieVisionResult:
    """Assess a full-body selfie and match visible garments to owned items."""
    settings = get_settings()
    ai_image, ai_content_type = _prepare_vision_image(image, content_type)
    encoded = base64.b64encode(ai_image).decode("ascii")
    candidates_json = json.dumps(wardrobe_candidates, ensure_ascii=True)
    prompt = f"""Analyze this outfit selfie quickly and conservatively.

First assess whether the photo clearly shows enough of the person's outfit to identify clothing. A usable photo should be reasonably lit, not severely blurred, and show most of the outfit. If it is unusable, set quality_acceptable=false, explain how to retake it, and return an empty items array.

For every clearly visible wardrobe-relevant garment, shoe, bag, or accessory:
- describe stable visible traits using visual_tags (pattern, material, silhouette, neckline, sleeve, length, distinctive details)
- match it to at most one candidate below only when category, color and visual traits are compatible
- matched_item_id must be copied exactly from the candidate list or null
- confidence is confidence in that specific wardrobe match; use at most 0.45 when unmatched and avoid forced matches

The user's wardrobe candidates are:
{candidates_json}

Return ONLY valid JSON in this exact shape:
{{
  "quality_acceptable": true,
  "quality_score": 0.0,
  "quality_feedback": "short helpful message",
  "items": [
    {{
      "detected_name": "concise garment name",
      "category": "shirt|pants|dress|jacket|shoes|accessory|other",
      "color": "dominant color",
      "description": "specific visible description",
      "visual_tags": ["up to 10 stable visual traits"],
      "matched_item_id": "exact candidate id or null",
      "confidence": 0.0
    }}
  ]
}}
Return at most 12 items. Do not identify the person or infer sensitive traits."""

    groq_error: Exception | None = None
    if settings.groq_api_key:
        try:
            response = httpx.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.groq_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.groq_vision_model,
                    "reasoning_effort": "none",
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are a conservative fashion visual matcher. Return JSON only.",
                        },
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:{content_type};base64,{encoded}"
                                    },
                                },
                            ],
                        },
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.1,
                    "max_completion_tokens": 1800,
                },
                timeout=settings.groq_request_timeout_seconds,
            )
            response.raise_for_status()
            return OutfitSelfieVisionResult.model_validate_json(
                response.json()["choices"][0]["message"]["content"]
            )
        except Exception as exc:
            groq_error = exc
            _log_provider_failure("groq", exc)
    if settings.gemini_api_key:
        try:
            return OutfitSelfieVisionResult.model_validate_json(
                gemini_json_from_image(prompt, ai_image, ai_content_type)
            )
        except Exception as exc:
            _log_provider_failure("gemini", exc)
            if groq_error:
                raise RuntimeError("All configured vision providers failed") from exc
            raise
    if groq_error:
        raise groq_error
    raise RuntimeError("No AI vision provider is configured")
