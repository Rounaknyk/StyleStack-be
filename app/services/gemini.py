import base64
import logging

import httpx

from app.core.config import get_settings

logger = logging.getLogger("stylestack.ai")


def gemini_json_from_image(
    prompt: str,
    image: bytes,
    content_type: str,
) -> str:
    """Return Gemini's JSON text for one inline image."""
    settings = get_settings()
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    model = settings.gemini_vision_model.removeprefix("models/")
    encoded = base64.b64encode(image).decode("ascii")
    response = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={
            "x-goog-api-key": settings.gemini_api_key,
            "Content-Type": "application/json",
        },
        json={
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": prompt},
                        {
                            "inline_data": {
                                "mime_type": content_type,
                                "data": encoded,
                            }
                        },
                    ],
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.1,
                "maxOutputTokens": 1800,
            },
        },
        timeout=min(settings.groq_request_timeout_seconds, 20.0),
    )
    response.raise_for_status()
    payload = response.json()
    text = "".join(
        str(part.get("text", ""))
        for candidate in payload.get("candidates", [])
        for part in candidate.get("content", {}).get("parts", [])
    ).strip()
    if not text:
        block_reason = payload.get("promptFeedback", {}).get("blockReason")
        logger.warning("gemini_empty_response block_reason=%s", block_reason)
        raise RuntimeError("Gemini returned no JSON text")
    return text
