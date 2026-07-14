from functools import lru_cache
import hashlib
from io import BytesIO
import logging
from pathlib import Path

import httpx
import numpy as np
import onnxruntime as ort
from PIL import Image, ImageFilter, ImageOps

from app.core.config import get_settings

logger = logging.getLogger("stylestack.fashion_segmentation")
MODEL_PATH = Path.home() / ".stylestack" / "models" / "segformer_b2_clothes_quantized.onnx"

CATEGORY_LABELS: dict[str, set[int]] = {
    "shirt": {4}, "shirts": {4}, "t-shirt": {4}, "top": {4}, "tops": {4},
    "jacket": {4}, "jackets": {4}, "dress": {5, 7}, "dresses": {5, 7},
    "skirt": {5}, "skirts": {5}, "pants": {6}, "trousers": {6},
    "jeans": {6}, "shoe": {9, 10}, "shoes": {9, 10},
    "accessory": {1, 3, 8, 16, 17}, "accessories": {1, 3, 8, 16, 17},
    "bag": {16}, "bags": {16}, "scarf": {17}, "belt": {8}, "hat": {1},
}
PERSON_LABELS = {2, 11, 12, 13, 14, 15}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as model:
        for chunk in iter(lambda: model.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ensure_model() -> Path:
    settings = get_settings()
    expected = settings.fashion_segmentation_model_sha256
    if MODEL_PATH.exists() and _sha256(MODEL_PATH) == expected:
        return MODEL_PATH
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = MODEL_PATH.with_suffix(".download")
    with httpx.stream(
        "GET", settings.fashion_segmentation_model_url,
        follow_redirects=True, timeout=120,
    ) as response:
        response.raise_for_status()
        with temporary.open("wb") as output:
            for chunk in response.iter_bytes(1024 * 1024):
                output.write(chunk)
    if _sha256(temporary) != expected:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("Fashion segmentation model checksum did not match")
    temporary.replace(MODEL_PATH)
    logger.info(
        "fashion_segmentation_model_ready size_mb=%.1f",
        MODEL_PATH.stat().st_size / 1048576,
    )
    return MODEL_PATH


@lru_cache(maxsize=1)
def _session() -> ort.InferenceSession:
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    return ort.InferenceSession(
        str(_ensure_model()), sess_options=options,
        providers=["CPUExecutionProvider"],
    )


def _segmentation_map(image: Image.Image) -> np.ndarray:
    # Preserve the source aspect ratio. Stretching a portrait or landscape
    # photo into a square changes garment proportions and is a common cause of
    # missing sleeves and hems in the final mask.
    source = image.convert("RGB")
    width, height = source.size
    scale = min(512 / width, 512 / height)
    resized_width = max(1, round(width * scale))
    resized_height = max(1, round(height * scale))
    resized = source.resize(
        (resized_width, resized_height), Image.Resampling.BILINEAR
    )
    left = (512 - resized_width) // 2
    top = (512 - resized_height) // 2
    canvas = Image.new("RGB", (512, 512), "white")
    canvas.paste(resized, (left, top))
    pixels = np.asarray(canvas, dtype=np.float32) / 255.0
    mean = np.asarray([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.asarray([0.229, 0.224, 0.225], dtype=np.float32)
    values = ((pixels - mean) / std).transpose(2, 0, 1)[None, ...]
    logits = _session().run(None, {"pixel_values": values})[0][0]
    labels = np.argmax(logits, axis=0).astype(np.uint8)
    output_height, output_width = labels.shape
    crop_left = round(left * output_width / 512)
    crop_top = round(top * output_height / 512)
    crop_right = round((left + resized_width) * output_width / 512)
    crop_bottom = round((top + resized_height) * output_height / 512)
    return labels[
        crop_top:max(crop_top + 1, crop_bottom),
        crop_left:max(crop_left + 1, crop_right),
    ]


def _adaptive_odd_filter_size(image_size: tuple[int, int]) -> int:
    """Return a safe mask expansion that scales with the source resolution."""
    radius = max(2, min(10, round(min(image_size) * 0.008)))
    return radius * 2 + 1


def isolate_garment_on_white(contents: bytes, category: str) -> bytes | None:
    """Return only the chosen garment class, or None when no reliable mask exists."""
    target_labels = CATEGORY_LABELS.get(category.strip().casefold())
    if not target_labels:
        return None
    source = ImageOps.exif_transpose(Image.open(BytesIO(contents))).convert("RGBA")
    labels = _segmentation_map(source)
    target = np.isin(labels, list(target_labels))
    person = np.isin(labels, list(PERSON_LABELS))
    target_ratio = float(target.mean())
    person_ratio = float(person.mean())
    if target_ratio < 0.004:
        return None
    # Product/flat-lay photos do not need semantic person removal. The general
    # high-detail model retains fine garment structure much more reliably.
    if person_ratio < 0.02:
        logger.info(
            "fashion_segmentation_skipped_no_person category=%s person_ratio=%.3f",
            category,
            person_ratio,
        )
        return None
    if target_ratio > 0.80:
        logger.warning(
            "fashion_segmentation_mask_rejected category=%s garment_ratio=%.3f",
            category,
            target_ratio,
        )
        return None
    mask = Image.fromarray((target * 255).astype(np.uint8), mode="L").resize(
        source.size, Image.Resampling.NEAREST
    )
    # Favor recall around the garment boundary so valid fabric is never shaved
    # away. A soft, resolution-aware expansion restores collars, cuffs, hems,
    # and sleeve edges while the semantic mask still excludes the person.
    filter_size = _adaptive_odd_filter_size(source.size)
    blur_radius = max(1.0, min(2.5, min(source.size) * 0.0015))
    mask = mask.filter(ImageFilter.MaxFilter(filter_size)).filter(
        ImageFilter.GaussianBlur(blur_radius)
    )
    garment = Image.new("RGBA", source.size, (0, 0, 0, 0))
    garment.paste(source, (0, 0), mask)
    white = Image.new("RGBA", source.size, (255, 255, 255, 255))
    white.alpha_composite(garment)
    output = BytesIO()
    white.convert("RGB").save(
        output, "JPEG", quality=95, optimize=True, progressive=True
    )
    logger.info(
        "fashion_garment_isolated category=%s garment_ratio=%.3f person_ratio=%.3f",
        category, target_ratio, person_ratio,
    )
    return output.getvalue()
