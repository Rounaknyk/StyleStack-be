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
    """Remove the dominant background and return an optimized white JPEG."""
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
    isolated = remove(source, session=_background_session())
    if not isinstance(isolated, Image.Image):
        isolated = Image.open(BytesIO(isolated)).convert("RGBA")
    else:
        isolated = isolated.convert("RGBA")

    white = Image.new("RGBA", isolated.size, (255, 255, 255, 255))
    white.alpha_composite(isolated)
    output = BytesIO()
    white.convert("RGB").save(
        output,
        format="JPEG",
        quality=92,
        optimize=True,
        progressive=True,
    )
    result = output.getvalue()
    if not result:
        raise RuntimeError("Background removal returned an empty image")
    return result
