from functools import lru_cache
from io import BytesIO
import logging

from PIL import Image, ImageOps

from app.core.config import get_settings
from app.services.fashion_segmentation import isolate_garment_on_white

logger = logging.getLogger("stylestack.images")


@lru_cache(maxsize=1)
def _background_session():
    # Imported lazily so application startup and non-image commands stay fast.
    from rembg import new_session

    return new_session(get_settings().background_removal_model)


def put_item_on_white_background(contents: bytes, category: str | None = None) -> bytes:
    """Remove the background without cropping or resizing the source canvas."""
    settings = get_settings()
    if settings.fashion_segmentation_enabled and category:
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


def put_item_on_transparent_background(contents: bytes) -> bytes:
    """Remove the background while preserving the complete source canvas as PNG."""
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
