"""Optional local CLIP relevance scoring for inspiration images.

CLIP is deliberately loaded lazily. The core API can run without the large
PyTorch/Transformers dependency; enabling this check requires installing the
packages in requirements-clip.txt and setting INSPIRATION_CLIP_ENABLED=true.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from io import BytesIO
from typing import Any

import httpx
from PIL import Image

from app.core.config import get_settings

logger = logging.getLogger("stylestack.clip")


@lru_cache(maxsize=1)
def _load_model() -> tuple[Any, Any, Any]:
    try:
        import torch
        from transformers import CLIPModel, CLIPProcessor
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "CLIP is enabled but optional dependencies are missing; install requirements-clip.txt"
        ) from exc

    settings = get_settings()
    logger.info("clip_model_loading model=%s", settings.inspiration_clip_model)
    processor = CLIPProcessor.from_pretrained(settings.inspiration_clip_model)
    model = CLIPModel.from_pretrained(settings.inspiration_clip_model)
    model.eval()
    logger.info("clip_model_ready model=%s", settings.inspiration_clip_model)
    return model, processor, torch


def _outfit_text(items: list[dict[str, Any]], occasion: str) -> str:
    pieces = []
    for item in items:
        category = item.get("category") or item.get("ai_category") or "clothing"
        color = item.get("color") or item.get("ai_color")
        pieces.append(f"{color + ' ' if color else ''}{category}")
    return (
        "A stylish person wearing a complete outfit with "
        + ", ".join(pieces)
        + f", suitable for a {occasion} occasion"
    )


def score_image(image_url: str, items: list[dict[str, Any]], occasion: str) -> float:
    """Return raw CLIP cosine score in [0, 1], or raise on unavailable scoring."""
    settings = get_settings()
    model, processor, torch = _load_model()
    response = httpx.get(image_url, timeout=settings.inspiration_clip_request_timeout_seconds)
    response.raise_for_status()
    image = Image.open(BytesIO(response.content)).convert("RGB")
    text = _outfit_text(items, occasion)
    inputs = processor(text=[text], images=image, return_tensors="pt", padding=True)
    with torch.no_grad():
        image_features = model.get_image_features(pixel_values=inputs["pixel_values"])
        text_features = model.get_text_features(
            input_ids=inputs["input_ids"], attention_mask=inputs.get("attention_mask")
        )
        image_features = image_features / image_features.norm(dim=-1, keepdim=True)
        text_features = text_features / text_features.norm(dim=-1, keepdim=True)
        cosine = (image_features @ text_features.T).item()
    return max(0.0, min(1.0, float(cosine)))


def score_if_enabled(image_url: str, items: list[dict[str, Any]], occasion: str) -> float | None:
    settings = get_settings()
    if not settings.inspiration_clip_enabled:
        return None
    return score_image(image_url, items, occasion)
