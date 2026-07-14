from io import BytesIO
import unittest
from unittest.mock import patch

from PIL import Image

from app.services.image_processing import put_item_on_white_background


def _jpeg(size: tuple[int, int] = (180, 120)) -> bytes:
    source = Image.new("RGB", size, "navy")
    output = BytesIO()
    source.save(output, "JPEG")
    return output.getvalue()


class ImageProcessingTests(unittest.TestCase):
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
