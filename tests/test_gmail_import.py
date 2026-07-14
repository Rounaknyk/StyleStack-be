import unittest
from io import BytesIO
import base64

from PIL import Image

from app.services.gmail_import import (
    _EmailImageParser,
    _ImageCandidate,
    _email_fashion_fallback,
    _header,
    _html_images,
    _likely_content_image,
    _prepare_candidate,
    _preview,
    _strong_product_hint,
)


class GmailImportLoggingTests(unittest.TestCase):
    def test_header_is_case_insensitive_and_single_line(self) -> None:
        payload = {
            "headers": [
                {"name": "Subject", "value": "Your\n  Myntra order"},
                {"name": "From", "value": "Orders <orders@example.com>"},
            ]
        }
        self.assertEqual(_header(payload, "subject"), "Your Myntra order")
        self.assertEqual(
            _header(payload, "FROM"), "Orders <orders@example.com>"
        )

    def test_content_preview_is_normalized_and_limited(self) -> None:
        preview = _preview("  First line\nsecond     line and more content")
        self.assertEqual(preview, "First line second line and more"[:30])
        self.assertLessEqual(len(preview), 30)

    def test_html_parser_extracts_product_image_metadata(self) -> None:
        parser = _EmailImageParser()
        parser.feed(
            '<img src="https://cdn.example.com/shoe.jpg" '
            'alt="Blue running shoe" width="640" height="800">'
        )
        self.assertEqual(len(parser.images), 1)
        self.assertEqual(parser.images[0].alt, "Blue running shoe")
        self.assertEqual(parser.images[0].width, 640)

    def test_json_ld_image_url_is_extracted(self) -> None:
        html = (
            '<script type="application/ld+json">'
            '{"image":"https:\\/\\/m.media-amazon.com\\/images\\/I\\/shirt.jpg"}'
            '</script>'
        )
        encoded = base64.urlsafe_b64encode(html.encode()).decode().rstrip("=")
        images = _html_images(
            {"mimeType": "text/html", "body": {"data": encoded}}
        )
        self.assertTrue(
            any(image.url.endswith("/shirt.jpg") for image in images)
        )

    def test_remote_images_are_limited_to_merchant_cdn_hosts(self) -> None:
        allowed = _EmailImageParser()
        allowed.feed('<img src="https://m.media-amazon.com/product.jpg">')
        legacy_amazon = _EmailImageParser()
        legacy_amazon.feed(
            '<img src="https://g-ecx.images-amazon.com/images/G/31/product.jpg">'
        )
        blocked = _EmailImageParser()
        blocked.feed('<img src="https://127.0.0.1/internal.png">')
        self.assertTrue(_likely_content_image(allowed.images[0]))
        self.assertTrue(_likely_content_image(legacy_amazon.images[0]))
        self.assertFalse(_likely_content_image(blocked.images[0]))

    def test_candidate_is_normalized_for_vision(self) -> None:
        source = BytesIO()
        Image.new("RGBA", (400, 600), (10, 20, 30, 150)).save(source, "PNG")
        candidate = _prepare_candidate(source.getvalue(), "product")
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.content_type, "image/jpeg")
        self.assertLess(len(candidate.contents), 4 * 1024 * 1024)

    def test_product_name_is_strong_fallback_but_navigation_is_not(self) -> None:
        self.assertTrue(
            _strong_product_hint("Amazon Brand - Symbol Men's Full Sleeve T-Shirt")
        )
        self.assertFalse(_strong_product_hint("Your Orders"))

    def test_amazon_catalog_image_can_fallback_when_email_names_clothing(self) -> None:
        candidate = _ImageCandidate(
            contents=b"image",
            content_type="image/jpeg",
            hint="",
            digest="abc",
            source_url="https://m.media-amazon.com/images/I/product.jpg",
        )
        fallback = _email_fashion_fallback(
            candidate, "Delivered today Amazon Brand Symbol men's full sleeve t-shirt"
        )
        self.assertIsNotNone(fallback)
        assert fallback is not None
        self.assertEqual(fallback.category, "shirt")


if __name__ == "__main__":
    unittest.main()
