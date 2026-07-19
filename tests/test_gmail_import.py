import unittest
from unittest.mock import MagicMock, patch
from io import BytesIO
import base64
from types import SimpleNamespace

from PIL import Image

from app.models.imports import GmailImportRequest, GmailProductAnalysis
from app.services.gmail_import import (
    _EmailImageParser,
    _ImageCandidate,
    _amazon_order_id,
    _amazon_product_from_title,
    _enrich_amazon_product_with_ai,
    _email_fashion_fallback,
    _delivered_item_count,
    _header,
    _html_images,
    _is_delivered_amazon_message,
    _is_amazon_order_thumbnail_url,
    _is_transactional_amazon_product,
    _is_generated_gmail_description,
    import_gmail_orders,
    _likely_content_image,
    _log_full_test_email,
    _original_amazon_image_url,
    _prepare_candidate,
    _preview,
    _strong_product_hint,
    _store_product,
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

    def test_full_test_email_log_contains_untruncated_body_and_images(self) -> None:
        html = (
            '<html><body><p>Complete order email body</p>'
            '<img src="https://m.media-amazon.com/images/I/cap.jpg" '
            'alt="Fitness Mantra Sports Winters Cap"></body></html>'
        )
        encoded = base64.urlsafe_b64encode(html.encode()).decode().rstrip("=")
        message = {
            "id": "message-1",
            "threadId": "thread-1",
            "payload": {
                "mimeType": "text/html",
                "headers": [
                    {
                        "name": "Subject",
                        "value": "Delivered: Order 408-5421781-6928348",
                    }
                ],
                "body": {"data": encoded, "size": len(html)},
            },
        }
        with self.assertLogs("stylestack.gmail_import", level="INFO") as logs:
            _log_full_test_email(message)
        output = "\n".join(logs.output)
        self.assertIn("Complete order email body", output)
        self.assertIn("Fitness Mantra Sports Winters Cap", output)
        self.assertIn("408-5421781-6928348", output)

    def test_html_parser_extracts_product_image_metadata(self) -> None:
        parser = _EmailImageParser()
        parser.feed(
            '<img src="https://cdn.example.com/shoe.jpg" '
            'alt="Blue running shoe" width="640" height="800">'
        )
        self.assertEqual(len(parser.images), 1)
        self.assertEqual(parser.images[0].alt, "Blue running shoe")
        self.assertEqual(parser.images[0].width, 640)

    def test_html_parser_uses_product_text_after_blank_alt_image(self) -> None:
        parser = _EmailImageParser()
        parser.feed(
            '<img src="https://m.media-amazon.com/images/G/product.jpg">'
            '<div>Fitness Mantra Sports Winters Cap</div>'
        )
        self.assertEqual(
            parser.images[0].alt,
            "Fitness Mantra Sports Winters Cap",
        )

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

    def test_amazon_merchant_image_can_fallback_from_adjacent_product_title(self) -> None:
        candidate = _ImageCandidate(
            contents=b"image",
            content_type="image/jpeg",
            hint="Fitness Mantra Sports Winters Cap",
            digest="abc",
            source_url="https://m.media-amazon.com/images/G/31/order-card.jpg",
            width=160,
            height=160,
        )
        fallback = _email_fashion_fallback(
            candidate,
            "Your package was delivered Order 408-5421781-6928348",
        )
        self.assertIsNotNone(fallback)
        assert fallback is not None
        self.assertEqual(fallback.category, "accessory")
        self.assertEqual(fallback.name, "Fitness Mantra Sports Winters Cap")

    def test_delivered_subject_limits_rate_limit_fallback_imports(self) -> None:
        self.assertEqual(
            _delivered_item_count("Delivered: 1 item | Order # 408-1"),
            1,
        )
        self.assertEqual(_delivered_item_count("Delivered: 2 items"), 2)

    def test_delivered_amazon_product_uses_its_own_title_without_ai(self) -> None:
        candidate = _ImageCandidate(
            contents=b"image",
            content_type="image/jpeg",
            hint=(
                "Fitness Mantra® Sports Winters Cap & Muffler for Men & Women| "
                "Beanie Cap| 1 Set| (Black)"
            ),
            digest="abc",
            source_url="https://m.media-amazon.com/images/I/product.jpg",
            width=2560,
            height=2560,
            email_width=90,
            email_height=90,
            is_order_thumbnail=True,
        )
        fallback = _amazon_product_from_title(candidate)
        self.assertIsNotNone(fallback)
        assert fallback is not None
        self.assertTrue(fallback.is_fashion_item)
        self.assertEqual(fallback.category, "accessory")
        self.assertIn("Fitness Mantra", fallback.name or "")
        self.assertEqual(fallback.brand, "Fitness Mantra")
        self.assertEqual(fallback.color, "black")
        self.assertEqual(fallback.season, "winter")
        self.assertIn("neck-warmer", fallback.tags)

    def test_amazon_title_builds_detailed_grounded_fallback(self) -> None:
        candidate = _ImageCandidate(
            contents=b"image",
            content_type="image/jpeg",
            hint="CHKOKKO Men's Full Sleeve Cotton High Neck T-Shirt (White)",
            digest="shirt",
            source_url="https://m.media-amazon.com/images/I/shirt.jpg",
            email_width=90,
            email_height=90,
            is_order_thumbnail=True,
        )

        analysis = _amazon_product_from_title(candidate)

        self.assertIsNotNone(analysis)
        assert analysis is not None
        self.assertEqual(analysis.brand, "CHKOKKO")
        self.assertEqual(analysis.color, "white")
        self.assertEqual(analysis.season, "winter")
        self.assertIn("cotton", analysis.description or "")
        self.assertIn("full sleeve", analysis.description or "")
        self.assertIn("high neck", analysis.description or "")

    def test_ai_enrichment_preserves_verified_product_identity(self) -> None:
        candidate = _ImageCandidate(
            contents=b"image",
            content_type="image/jpeg",
            hint="CHKOKKO Men's Full Sleeve Cotton High Neck T-Shirt (White)",
            digest="shirt",
            source_url="https://m.media-amazon.com/images/I/shirt.jpg",
            email_width=90,
            email_height=90,
            is_order_thumbnail=True,
        )
        verified = _amazon_product_from_title(candidate)
        assert verified is not None
        ai_result = GmailProductAnalysis(
            is_fashion_item=True,
            name="Incorrect invented sweater",
            brand="Incorrect brand",
            category="jacket",
            color="black",
            season="all",
            formality="casual",
            description=(
                "A clean white high-neck top with long sleeves and a streamlined "
                "silhouette. It works well as a casual winter base layer or as a "
                "minimal standalone top."
            ),
            tags=[
                "high neck",
                "full sleeve",
                "minimal",
                "layering",
                "solid",
            ],
        )

        with patch(
            "app.services.gmail_import._analyze_product_image",
            return_value=ai_result,
        ):
            enriched, used_ai = _enrich_amazon_product_with_ai(
                candidate,
                verified,
            )

        self.assertTrue(used_ai)
        self.assertEqual(enriched.name, verified.name)
        self.assertEqual(enriched.brand, "CHKOKKO")
        self.assertEqual(enriched.category, "shirt")
        self.assertEqual(enriched.color, "white")
        self.assertEqual(enriched.season, "winter")
        self.assertIn("streamlined silhouette", enriched.description or "")
        self.assertEqual(len(enriched.tags), 5)

    def test_old_generated_description_is_safe_to_upgrade(self) -> None:
        self.assertTrue(
            _is_generated_gmail_description(
                "White shirt from a confirmed Amazon delivery."
            )
        )
        self.assertFalse(
            _is_generated_gmail_description(
                "My favorite white cotton top for office layering."
            )
        )

    def test_existing_generic_import_is_updated_without_uploading_again(self) -> None:
        client = MagicMock()
        table = client.table.return_value
        table.select.return_value = table
        table.eq.return_value = table
        table.limit.return_value = table
        table.update.return_value = table
        table.execute.return_value = SimpleNamespace(
            data=[
                {
                    "id": "item-1",
                    "brand": "CHKOKKO",
                    "color": "white",
                    "season": ["winter"],
                    "formality": "casual",
                    "description": "White shirt from a confirmed Amazon delivery.",
                    "tags": ["shirt", "white"],
                    "source_external_id": "amazon:order-1:shirt",
                }
            ]
        )
        candidate = _ImageCandidate(
            contents=b"image",
            content_type="image/jpeg",
            hint="CHKOKKO Men's Full Sleeve Cotton High Neck T-Shirt (White)",
            digest="shirt",
            source_url="https://m.media-amazon.com/images/I/shirt.jpg",
            email_width=90,
            email_height=90,
            is_order_thumbnail=True,
        )
        analysis = GmailProductAnalysis(
            is_fashion_item=True,
            name=candidate.hint,
            brand="CHKOKKO",
            category="shirt",
            color="white",
            season="winter",
            formality="casual",
            description=(
                "A white cotton high-neck shirt with full sleeves and a clean, "
                "minimal silhouette. It is suited to casual winter layering."
            ),
            tags=["cotton", "high neck", "full sleeve", "minimal", "layering"],
        )

        result = _store_product(client, "uid", "order-1", candidate, analysis)

        self.assertEqual(result, "updated")
        client.storage.from_.assert_not_called()
        update_payload = table.update.call_args.args[0]
        self.assertIn("minimal silhouette", update_payload["description"])
        self.assertEqual(update_payload["ai_category"], "shirt")
        self.assertEqual(len(update_payload["tags"]), 5)

    def test_amazon_thumbnail_url_is_rewritten_to_original_image(self) -> None:
        self.assertTrue(
            _is_amazon_order_thumbnail_url(
                "https://m.media-amazon.com/images/I/71ETCqzBUVL.*SS90*.jpg"
            )
        )
        self.assertFalse(
            _is_amazon_order_thumbnail_url(
                "https://m.media-amazon.com/images/I/41CA4IUucWL.*SR276,276*.jpg"
            )
        )
        self.assertEqual(
            _original_amazon_image_url(
                "https://m.media-amazon.com/images/I/71ETCqzBUVL.*SS90*.jpg"
            ),
            "https://m.media-amazon.com/images/I/71ETCqzBUVL.jpg",
        )
        self.assertEqual(
            _original_amazon_image_url(
                "https://m.media-amazon.com/images/I/41CA4IUucWL.*SR276,276*.jpg"
            ),
            "https://m.media-amazon.com/images/I/41CA4IUucWL.jpg",
        )

    def test_recommendation_carousel_image_is_not_a_purchased_product(self) -> None:
        recommendation = _ImageCandidate(
            contents=b"image",
            content_type="image/jpeg",
            hint="Amazon Brand - Symbol Men's Casual Acrylic High Neck Sweater",
            digest="recommendation",
            source_url="https://m.media-amazon.com/images/I/sweater.jpg",
            width=2560,
            height=2560,
            email_width=276,
            email_height=276,
        )
        self.assertFalse(_is_transactional_amazon_product(recommendation))
        self.assertIsNone(_amazon_product_from_title(recommendation))

    def test_delivered_import_rejects_unlabelled_catalog_image(self) -> None:
        unrelated_jacket = _ImageCandidate(
            contents=b"image",
            content_type="image/jpeg",
            hint="",
            digest="jacket",
            source_url="https://m.media-amazon.com/images/I/jacket.jpg",
            width=500,
            height=700,
            email_width=90,
            email_height=90,
        )
        self.assertIsNone(_amazon_product_from_title(unrelated_jacket))

    def test_delivered_import_rejects_nonmerchant_image(self) -> None:
        candidate = _ImageCandidate(
            contents=b"image",
            content_type="image/jpeg",
            hint="Fitness Mantra Sports Winters Cap",
            digest="abc",
            source_url="https://example.com/tracking-image.jpg",
            email_width=90,
            email_height=90,
        )
        self.assertIsNone(_amazon_product_from_title(candidate))

    def test_delivered_import_rejects_amazon_logo(self) -> None:
        logo = _ImageCandidate(
            contents=b"image",
            content_type="image/jpeg",
            hint="Amazon.in",
            digest="logo",
            source_url=(
                "https://m.media-amazon.com/images/G/01/outbound/"
                "OutboundTemplates/Smile_Logo_Dark.png"
            ),
            width=86,
            height=43,
            email_width=86,
            email_height=43,
        )
        self.assertIsNone(_amazon_product_from_title(logo))

    def test_only_delivered_amazon_subjects_are_accepted(self) -> None:
        def payload(subject: str, sender: str = "order-update@amazon.in") -> dict:
            return {
                "headers": [
                    {"name": "Subject", "value": subject},
                    {"name": "From", "value": sender},
                ]
            }

        self.assertTrue(
            _is_delivered_amazon_message(
                payload("Delivered: 1 item | Order # 408-5421781-6928348")
            )
        )
        self.assertTrue(
            _is_delivered_amazon_message(
                payload(
                    "Delivered: Your Amazon package has been delivered.",
                    "shipment-tracking@amazon.in",
                )
            )
        )
        self.assertFalse(
            _is_delivered_amazon_message(payload("Shipped: Your Amazon order"))
        )
        self.assertFalse(
            _is_delivered_amazon_message(payload("Arriving today: Your order"))
        )
        self.assertFalse(
            _is_delivered_amazon_message(
                payload("Delivered: Your package could not be delivered")
            )
        )
        self.assertFalse(
            _is_delivered_amazon_message(
                payload("Delivered: order update", "offers@example.com")
            )
        )

    def test_order_id_is_extracted_from_delivered_subject(self) -> None:
        payload = {
            "headers": [
                {
                    "name": "Subject",
                    "value": "Delivered: 1 item | Order # 408-5421781-6928348",
                }
            ]
        }
        self.assertEqual(
            _amazon_order_id(payload),
            "408-5421781-6928348",
        )

    def test_import_request_no_longer_has_test_order_id(self) -> None:
        request = GmailImportRequest(access_token="x" * 20)
        self.assertFalse(hasattr(request, "order_id"))

    def test_import_orchestration_skips_shipped_mail_and_enriches_product(self) -> None:
        delivered_payload = {
            "headers": [
                {
                    "name": "Subject",
                    "value": "Delivered: 1 item | Order # 408-5421781-6928348",
                },
                {
                    "name": "From",
                    "value": '"Amazon.in" <order-update@amazon.in>',
                },
            ]
        }
        shipped_payload = {
            "headers": [
                {"name": "Subject", "value": "Shipped: Your Amazon order"},
                {
                    "name": "From",
                    "value": '"Amazon.in" <shipment-tracking@amazon.in>',
                },
            ]
        }
        candidate = _ImageCandidate(
            contents=b"image",
            content_type="image/jpeg",
            hint="Fitness Mantra Sports Winters Cap (Black)",
            digest="cap",
            source_url="https://m.media-amazon.com/images/I/cap.jpg",
            width=1200,
            height=1200,
            email_width=90,
            email_height=90,
            is_order_thumbnail=True,
        )
        ai_result = GmailProductAnalysis(
            is_fashion_item=True,
            category="accessory",
            color="black",
            season="winter",
            formality="casual",
            description=(
                "A black knitted beanie and matching neck warmer with a soft, "
                "insulating texture. The coordinated set is suited to casual "
                "cold-weather outfits and everyday outdoor wear."
            ),
            tags=["knitted", "beanie", "neck warmer", "winter", "matching set"],
        )

        def gmail_get(_http, path, _token, **_params):
            if path == "messages":
                return {"messages": [{"id": "delivered"}, {"id": "shipped"}]}
            if path == "messages/delivered":
                return {"id": "delivered", "payload": delivered_payload}
            if path == "messages/shipped":
                return {"id": "shipped", "payload": shipped_payload}
            raise AssertionError(f"Unexpected Gmail path: {path}")

        with (
            patch("app.services.gmail_import._gmail_get", side_effect=gmail_get) as get,
            patch(
                "app.services.gmail_import._amazon_order_content",
                return_value=("delivered", [candidate], 1, 1, ["m.media-amazon.com"]),
            ),
            patch(
                "app.services.gmail_import._store_product",
                return_value="created",
            ) as store,
            patch(
                "app.services.gmail_import._analyze_product_image",
                return_value=ai_result,
            ) as analyze,
        ):
            result = import_gmail_orders(object(), "uid", "x" * 20, 25)

        self.assertEqual(result, (2, 1, 1))
        analyze.assert_called_once()
        stored_analysis = store.call_args.args[4]
        self.assertIn("Fitness Mantra", stored_analysis.name or "")
        self.assertIn("insulating texture", stored_analysis.description or "")
        self.assertEqual(stored_analysis.tags[:2], ["knitted", "beanie"])
        query = get.call_args_list[0].kwargs["q"]
        self.assertIn('subject:"Delivered:"', query)

    def test_complete_import_paginates_every_delivered_search_page(self) -> None:
        ignored_payload = {
            "headers": [
                {"name": "Subject", "value": "Shipped: Your Amazon order"},
                {
                    "name": "From",
                    "value": '"Amazon.in" <shipment-tracking@amazon.in>',
                },
            ]
        }
        listed_pages: list[str | None] = []

        def gmail_get(_http, path, _token, **params):
            if path == "messages":
                page_token = params.get("pageToken")
                listed_pages.append(page_token)
                if page_token is None:
                    return {
                        "messages": [{"id": "first"}],
                        "nextPageToken": "page-2",
                    }
                return {"messages": [{"id": "second"}]}
            if path in {"messages/first", "messages/second"}:
                return {"id": path.rsplit("/", 1)[-1], "payload": ignored_payload}
            raise AssertionError(f"Unexpected Gmail path: {path}")

        progress: list[tuple[int, int, int]] = []
        with patch(
            "app.services.gmail_import._gmail_get",
            side_effect=gmail_get,
        ):
            result = import_gmail_orders(
                object(),
                "uid",
                "x" * 20,
                limit=None,
                on_progress=lambda scanned, imported, skipped: progress.append(
                    (scanned, imported, skipped)
                ),
            )

        self.assertEqual(result, (2, 0, 2))
        self.assertEqual(listed_pages, [None, "page-2"])
        self.assertEqual(progress[-1], (2, 0, 2))


if __name__ == "__main__":
    unittest.main()
