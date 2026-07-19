import io
import math

from PIL import Image, ImageOps


def perceptual_hash(image_bytes: bytes, hash_size: int = 8) -> str:
    """Return a rotation-sensitive pHash without adding a large ML dependency."""
    sample_size = hash_size * 4
    with Image.open(io.BytesIO(image_bytes)) as source:
        image = ImageOps.exif_transpose(source).convert("L").resize(
            (sample_size, sample_size),
            Image.Resampling.LANCZOS,
        )
        pixels = list(image.get_flattened_data())

    coefficients: list[float] = []
    factor = math.pi / sample_size
    for vertical in range(hash_size):
        for horizontal in range(hash_size):
            total = 0.0
            for y in range(sample_size):
                cos_y = math.cos((2 * y + 1) * vertical * factor / 2)
                row = y * sample_size
                for x in range(sample_size):
                    total += (
                        pixels[row + x]
                        * math.cos((2 * x + 1) * horizontal * factor / 2)
                        * cos_y
                    )
            coefficients.append(total)

    detail = coefficients[1:]
    median = sorted(detail)[len(detail) // 2]
    bits = 0
    for coefficient in coefficients:
        bits = (bits << 1) | int(coefficient > median)
    return f"{bits:0{hash_size * hash_size // 4}x}"
