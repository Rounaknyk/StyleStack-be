from functools import lru_cache
from io import BytesIO
import logging

from PIL import Image, ImageOps

from app.core.config import get_settings
from app.services.fashion_segmentation import (
    GENERIC_ACCESSORY_CATEGORIES,
    isolate_garment_on_transparent,
    isolate_garment_on_white,
)

logger = logging.getLogger("stylestack.images")

_poof_disabled_for_process = False


@lru_cache(maxsize=1)
def _background_session():
    # Imported lazily so application startup and non-image commands stay fast.
    from rembg import new_session

    return new_session(get_settings().background_removal_model)


@lru_cache(maxsize=1)
def _poof_client():
    # Imported lazily so the SDK adds no startup cost when Poof is disabled.
    from poof import Poof

    settings = get_settings()
    if not settings.poof_api_key:
        raise RuntimeError("Poof is not configured")
    return Poof(
        api_key=settings.poof_api_key,
        timeout=settings.poof_request_timeout_seconds,
    )


def _remove_background_with_poof(contents: bytes) -> bytes:
    """Return a full-canvas transparent PNG produced by Poof."""
    source = ImageOps.exif_transpose(Image.open(BytesIO(contents))).convert("RGBA")
    result = _poof_client().remove_background(
        contents,
        format="png",
        channels="rgba",
        size="full",
        crop=False,
    )
    data = getattr(result, "data", None)
    if not isinstance(data, bytes) or not data:
        raise RuntimeError("Poof returned an empty image")

    isolated = ImageOps.exif_transpose(Image.open(BytesIO(data))).convert("RGBA")
    if isolated.size != source.size:
        raise RuntimeError(
            "Poof changed the source canvas from "
            f"{source.size} to {isolated.size}"
        )
    if isolated.getchannel("A").getbbox() is None:
        raise RuntimeError("Poof erased the entire item")

    output = BytesIO()
    isolated.save(output, format="PNG", optimize=True)
    processed = output.getvalue()
    if not processed:
        raise RuntimeError("Poof returned an empty transparent PNG")
    logger.info(
        "poof_background_removed processing_time_ms=%s",
        getattr(result, "processing_time_ms", None),
    )
    return processed


def _try_poof_background_removal(contents: bytes) -> bytes | None:
    """Use Poof while configured/credited, then gracefully fall back locally."""
    global _poof_disabled_for_process

    settings = get_settings()
    if not settings.poof_api_key or _poof_disabled_for_process:
        return None
    try:
        return _remove_background_with_poof(contents)
    except Exception as exc:
        error_name = type(exc).__name__
        # Invalid credentials and exhausted credits will not recover during the
        # same server process. Disabling further calls avoids delaying every
        # queued upload; a deploy/restart (or new billing period) re-enables it.
        if error_name in {
            "AuthError",
            "AuthenticationError",
            "AuthorizationError",
            "PaymentRequiredError",
            "PermissionDeniedError",
        }:
            _poof_disabled_for_process = True
        logger.warning(
            "poof_background_removal_failed error_type=%s fallback=local_model "
            "disabled_for_process=%s",
            error_name,
            _poof_disabled_for_process,
        )
        return None


def put_item_on_white_background(contents: bytes, category: str | None = None) -> bytes:
    """Remove the background without cropping or resizing the source canvas."""
    settings = get_settings()
    category_key = (category or "").strip().casefold()
    use_semantic_mask = category_key not in GENERIC_ACCESSORY_CATEGORIES
    if settings.fashion_segmentation_enabled and category and use_semantic_mask:
        try:
            fashion_result = isolate_garment_on_white(contents, category)
            if fashion_result:
                return fashion_result
        except Exception as exc:
            logger.warning(
                "fashion_segmentation_failed category=%s error_type=%s",
                category,
                type(exc).__name__,
            )
    from rembg import remove

    source = Image.open(BytesIO(contents))
    source = ImageOps.exif_transpose(source).convert("RGBA")
    isolated = remove(
        source,
        session=_background_session(),
        alpha_matting=True,
        alpha_matting_foreground_threshold=240,
        alpha_matting_background_threshold=10,
        alpha_matting_erode_size=5,
        post_process_mask=True,
    )
    if not isinstance(isolated, Image.Image):
        isolated = Image.open(BytesIO(isolated)).convert("RGBA")
    else:
        isolated = isolated.convert("RGBA")

    if isolated.size != source.size:
        raise RuntimeError(
            "Background removal changed the source canvas from "
            f"{source.size} to {isolated.size}"
        )

    alpha = isolated.getchannel("A")
    alpha_bounds = alpha.getbbox()
    if alpha_bounds is None:
        raise RuntimeError("Background removal erased the entire item")
    kept_area = sum(
        level * count for level, count in enumerate(alpha.histogram())
    ) / (255 * source.width * source.height)
    if kept_area < 0.002:
        raise RuntimeError("Background removal retained too little of the item")

    white = Image.new("RGBA", isolated.size, (255, 255, 255, 255))
    white.alpha_composite(isolated)
    output = BytesIO()
    white.convert("RGB").save(
        output,
        format="JPEG",
        quality=95,
        optimize=True,
        progressive=True,
    )
    result = output.getvalue()
    if not result:
        raise RuntimeError("Background removal returned an empty image")
    return result


def put_item_on_transparent_background(
    contents: bytes, category: str | None = None
) -> bytes:
    """Remove the background while preserving the complete source canvas as PNG."""
    poof_result = _try_poof_background_removal(contents)
    if poof_result:
        return poof_result

    settings = get_settings()
    category_key = (category or "").strip().casefold()
    use_semantic_mask = category_key not in GENERIC_ACCESSORY_CATEGORIES
    if settings.fashion_segmentation_enabled and category and use_semantic_mask:
        try:
            fashion_result = isolate_garment_on_transparent(contents, category)
            if fashion_result:
                return fashion_result
        except Exception as exc:
            logger.warning(
                "fashion_transparent_segmentation_failed category=%s error_type=%s",
                category, type(exc).__name__,
            )
    from rembg import remove

    source = ImageOps.exif_transpose(Image.open(BytesIO(contents))).convert("RGBA")
    isolated = remove(
        source,
        session=_background_session(),
        alpha_matting=True,
        alpha_matting_foreground_threshold=240,
        alpha_matting_background_threshold=10,
        alpha_matting_erode_size=5,
        post_process_mask=True,
    )
    if not isinstance(isolated, Image.Image):
        isolated = Image.open(BytesIO(isolated)).convert("RGBA")
    else:
        isolated = isolated.convert("RGBA")
    if isolated.size != source.size:
        raise RuntimeError("Transparent background removal changed the source canvas")
    alpha = isolated.getchannel("A")
    if alpha.getbbox() is None:
        raise RuntimeError("Transparent background removal erased the entire item")
    output = BytesIO()
    isolated.save(output, format="PNG", optimize=True)
    result = output.getvalue()
    if not result:
        raise RuntimeError("Transparent cutout is empty")
    return result


def optimize_item_image(
    contents: bytes,
    *,
    max_dimension: int = 2048,
    quality: int = 90,
) -> bytes:
    """Normalize orientation and size without cropping or changing garment color."""
    source = ImageOps.exif_transpose(Image.open(BytesIO(contents)))
    if source.mode in {"RGBA", "LA"} or (
        source.mode == "P" and "transparency" in source.info
    ):
        rgba = source.convert("RGBA")
        white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        white.alpha_composite(rgba)
        source = white.convert("RGB")
    else:
        source = source.convert("RGB")

    source.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
    output = BytesIO()
    source.save(
        output,
        format="JPEG",
        quality=quality,
        optimize=True,
        progressive=True,
    )
    result = output.getvalue()
    if not result:
        raise RuntimeError("Image optimization returned an empty image")
    return result


def create_item_thumbnail(
    contents: bytes,
    *,
    max_dimension: int = 480,
    quality: int = 84,
) -> bytes:
    """Create an aspect-preserving grid image; never center-crop clothing."""
    return optimize_item_image(
        contents,
        max_dimension=max_dimension,
        quality=quality,
    )
