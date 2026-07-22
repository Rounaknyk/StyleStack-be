from io import BytesIO
import unittest
from unittest.mock import patch

from PIL import Image

from app.services.image_processing import (
    create_item_thumbnail,
    optimize_item_image,
    put_item_on_transparent_background,
    put_item_on_white_background,
)
from app.services.fashion_segmentation import CATEGORY_LABELS


def _jpeg(size: tuple[int, int] = (180, 120)) -> bytes:
    source = Image.new("RGB", size, "navy")
    output = BytesIO()
    source.save(output, "JPEG")
    return output.getvalue()


class ImageProcessingTests(unittest.TestCase):
    def test_poof_is_used_before_local_background_models(self) -> None:
        isolated = Image.new("RGBA", (180, 120), (0, 0, 0, 0))
        isolated.putpixel((90, 60), (20, 40, 130, 255))
        output = BytesIO()
        isolated.save(output, "PNG")

        with (
            patch(
                "app.services.image_processing._try_poof_background_removal",
                return_value=output.getvalue(),
            ),
            patch(
                "app.services.image_processing.isolate_garment_on_transparent",
                side_effect=AssertionError("local segmentation should not run"),
            ),
        ):
            result = put_item_on_transparent_background(_jpeg(), "shirt")

        self.assertEqual(result, output.getvalue())

    def test_local_model_remains_fallback_when_poof_is_unavailable(self) -> None:
        isolated = Image.new("RGBA", (180, 120), (0, 0, 0, 0))
        isolated.putpixel((90, 60), (20, 40, 130, 255))
        output = BytesIO()
        isolated.save(output, "PNG")

        with (
            patch(
                "app.services.image_processing._try_poof_background_removal",
                return_value=None,
            ),
            patch(
                "app.services.image_processing.isolate_garment_on_transparent",
                return_value=output.getvalue(),
            ),
        ):
            result = put_item_on_transparent_background(_jpeg(), "shirt")

        self.assertEqual(result, output.getvalue())

    def test_footwear_aliases_use_shoe_segmentation_labels(self) -> None:
        self.assertEqual(CATEGORY_LABELS["sneakers"], {9, 10})
        self.assertEqual(CATEGORY_LABELS["footwear"], {9, 10})

    def test_small_accessories_bypass_clothes_segmentation(self) -> None:
        isolated = Image.new("RGBA", (180, 120), (0, 0, 0, 0))
        for x in range(60, 120):
            for y in range(35, 85):
                isolated.putpixel((x, y), (20, 40, 130, 255))
        with (
            patch(
                "app.services.image_processing.isolate_garment_on_transparent",
                side_effect=AssertionError("semantic segmentation should be skipped"),
            ),
            patch("rembg.remove", return_value=isolated),
            patch("app.services.image_processing._background_session"),
        ):
            result = put_item_on_white_background(_jpeg(), "watch")
        self.assertTrue(result)

    def test_optimization_preserves_aspect_ratio_and_limits_largest_side(self) -> None:
        result = optimize_item_image(_jpeg((3000, 1500)), max_dimension=1000)
        rendered = Image.open(BytesIO(result))
        self.assertEqual(rendered.size, (1000, 500))

    def test_thumbnail_never_crops_the_image(self) -> None:
        result = create_item_thumbnail(_jpeg((1200, 400)), max_dimension=480)
        rendered = Image.open(BytesIO(result))
        self.assertEqual(rendered.size, (480, 160))

    def test_general_removal_preserves_canvas_and_whitens_background(
        self,
    ) -> None:
        isolated = Image.new("RGBA", (180, 120), (0, 0, 0, 0))
        for x in range(35, 145):
            for y in range(20, 105):
                isolated.putpixel((x, y), (20, 40, 130, 255))

        with (
            patch(
                "app.services.image_processing.isolate_garment_on_white",
                return_value=None,
            ),
            patch("rembg.remove", return_value=isolated),
            patch("app.services.image_processing._background_session"),
        ):
            result = put_item_on_white_background(_jpeg(), "shirt")

        rendered = Image.open(BytesIO(result)).convert("RGB")
        self.assertEqual(rendered.size, (180, 120))
        self.assertTrue(all(value > 245 for value in rendered.getpixel((5, 5))))
        self.assertLess(rendered.getpixel((90, 60))[2], 180)

    def test_changed_canvas_is_rejected_instead_of_silently_cropping(self) -> None:
        isolated = Image.new("RGBA", (90, 60), (20, 40, 130, 255))
        with (
            patch(
                "app.services.image_processing.isolate_garment_on_white",
                return_value=None,
            ),
            patch("rembg.remove", return_value=isolated),
            patch("app.services.image_processing._background_session"),
        ):
            with self.assertRaisesRegex(RuntimeError, "changed the source canvas"):
                put_item_on_white_background(_jpeg(), "shirt")


if __name__ == "__main__":
    unittest.main()
