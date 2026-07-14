import io
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from app.services.ai_tagging import _prepare_vision_image
from app.services.gemini import gemini_json_from_image


class VisionProviderTests(unittest.TestCase):
    def test_ai_image_copy_is_resized_and_converted_to_jpeg(self) -> None:
        original = io.BytesIO()
        Image.new("RGBA", (2400, 1800), (10, 20, 30, 180)).save(original, "PNG")

        prepared, content_type = _prepare_vision_image(
            original.getvalue(), "image/png"
        )

        self.assertEqual(content_type, "image/jpeg")
        with Image.open(io.BytesIO(prepared)) as image:
            self.assertLessEqual(max(image.size), 1280)
            self.assertEqual(image.mode, "RGB")

    @patch("app.services.gemini.httpx.post")
    def test_gemini_uses_generate_content_and_reads_json_text(
        self, post: Mock
    ) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "candidates": [
                {"content": {"parts": [{"text": '{"ok":true}'}]}}
            ]
        }
        post.return_value = response

        text = gemini_json_from_image("Return JSON", b"image", "image/jpeg")

        self.assertEqual(text, '{"ok":true}')
        url = post.call_args.args[0]
        payload = post.call_args.kwargs["json"]
        self.assertIn(":generateContent", url)
        self.assertEqual(
            payload["generationConfig"]["responseMimeType"],
            "application/json",
        )
        self.assertIn("inline_data", payload["contents"][0]["parts"][1])

if __name__ == "__main__":
    unittest.main()
