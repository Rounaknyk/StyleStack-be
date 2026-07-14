from io import BytesIO
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from app.services.fashion_segmentation import isolate_garment_on_white


class FashionSegmentationTests(unittest.TestCase):
    def test_only_requested_garment_pixels_are_kept(self) -> None:
        source = Image.new("RGB", (100, 100), "red")
        encoded = BytesIO()
        source.save(encoded, "JPEG")
        labels = np.full((128, 128), 11, dtype=np.uint8)  # face/person
        labels[:, :64] = 4  # upper-clothes
        with patch(
            "app.services.fashion_segmentation._segmentation_map",
            return_value=labels,
        ):
            result = isolate_garment_on_white(encoded.getvalue(), "shirt")
        self.assertIsNotNone(result)
        assert result is not None
        rendered = Image.open(BytesIO(result)).convert("RGB")
        self.assertGreater(rendered.getpixel((20, 50))[0], 150)
        self.assertTrue(all(value > 235 for value in rendered.getpixel((80, 50))))

    def test_unknown_category_uses_caller_fallback(self) -> None:
        self.assertIsNone(isolate_garment_on_white(b"not-read", "other"))

    def test_product_photo_without_person_uses_high_detail_fallback(self) -> None:
        source = Image.new("RGB", (160, 90), "blue")
        encoded = BytesIO()
        source.save(encoded, "JPEG")
        labels = np.full((72, 128), 4, dtype=np.uint8)
        with patch(
            "app.services.fashion_segmentation._segmentation_map",
            return_value=labels,
        ):
            result = isolate_garment_on_white(encoded.getvalue(), "shirt")
        self.assertIsNone(result)

    def test_person_photo_keeps_original_canvas_dimensions(self) -> None:
        source = Image.new("RGB", (180, 320), "red")
        encoded = BytesIO()
        source.save(encoded, "JPEG")
        labels = np.full((128, 72), 11, dtype=np.uint8)
        labels[28:98, 15:58] = 4
        with patch(
            "app.services.fashion_segmentation._segmentation_map",
            return_value=labels,
        ):
            result = isolate_garment_on_white(encoded.getvalue(), "shirt")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(Image.open(BytesIO(result)).size, source.size)


if __name__ == "__main__":
    unittest.main()
