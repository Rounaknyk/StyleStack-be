import base64
from dataclasses import dataclass
import hashlib
from html import unescape
from html.parser import HTMLParser
from io import BytesIO
import logging
import re
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from PIL import Image, ImageOps

from app.core.config import get_settings
from app.models.imports import GmailProductAnalysis
from app.services.image_processing import put_item_on_white_background
from app.services.gemini import gemini_json_from_image

logger = logging.getLogger("stylestack.gmail_import")

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
FORCED_GMAIL_ORDER_ID = "408-5421781-6928348"
MAX_REMOTE_IMAGE_BYTES = 10 * 1024 * 1024
MAX_CANDIDATE_IMAGES_PER_MESSAGE = 4
ALLOWED_IMAGE_HOST_SUFFIXES = (
    "amazon.in",
    "amazon.com",
    "media-amazon.com",
    "images-amazon.com",
    "ssl-images-amazon.com",
    "myntra.com",
    "myntassets.com",
    "flipkart.com",
    "flixcart.com",
    "fkcdn.com",
    "ajio.com",
    "googleusercontent.com",
)

MERCHANT_QUERIES: dict[str, str] = {
    "amazon": (
        'newer_than:2y '
        '{from:order-update@amazon.in from:shipment-tracking@amazon.in} '
        '{subject:"Delivered:" "Your package was delivered"} '
        '-subject:(cancelled OR refunded OR returned) '
        '-subject:"could not be delivered"'
    ),
    "myntra": (
        'newer_than:2y from:(myntra.com) subject:order subject:delivered '
        '-subject:(sale OR offer OR coupon)'
    ),
    "flipkart": (
        'newer_than:2y from:(flipkart.com) subject:order subject:delivered '
        '-subject:(sale OR offer OR cashback)'
    ),
    "ajio": (
        'newer_than:2y from:(ajio.com) subject:order subject:delivered '
        '-subject:(sale OR offer OR coupon)'
    ),
}

IMAGE_PROMPT = """Decide whether this ecommerce delivery-email image shows a purchased fashion item suitable for a wardrobe app.
Fashion items include clothing, shoes, bags, jewelry, watches and wearable accessories.
Reject store logos, icons, tracking pixels, banners, models without a clearly featured product, electronics, home goods, beauty products and packaging.
Use the email context only to improve the product name or brand; trust the image for visual attributes.
Return ONLY JSON:
{
  "is_fashion_item": true_or_false,
  "name": "specific concise product name or null",
  "brand": "brand or null",
  "category": "shirt|pants|dress|jacket|shoes|accessory|other or null",
  "color": "black|white|red|blue|green|yellow|purple|pink|brown|grey|orange|beige|multicolor or null",
  "season": "summer|winter|spring|autumn|all or null",
  "formality": "formal|semi-formal|casual|sporty or null",
  "description": "short visual description or null",
  "tags": ["up to 5 useful style/material tags"]
}
"""


@dataclass(frozen=True, slots=True)
class _HTMLImage:
    url: str
    alt: str = ""
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True, slots=True)
class _ImageCandidate:
    contents: bytes
    content_type: str
    hint: str
    digest: str
    source_url: str | None = None
    width: int | None = None
    height: int | None = None


class _EmailImageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.images: list[_HTMLImage] = []

    @staticmethod
    def _dimension(value: str | None) -> int | None:
        if not value:
            return None
        match = re.search(r"\d+", value)
        return int(match.group()) if match else None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.casefold(): value or "" for name, value in attrs}
        for styled_url in re.findall(
            r"url\(\s*['\"]?([^)'\"]+)", values.get("style", ""), re.I
        ):
            self.images.append(_HTMLImage(url=unescape(styled_url.strip())))
        if tag.casefold() != "img":
            return
        src = values.get("src", "").strip()
        if not src and values.get("srcset"):
            src = values["srcset"].split(",")[0].strip().split(" ")[0]
        if src:
            alt = re.sub(r"\s+", " ", values.get("alt", "")).strip()[:300]
            self.images.append(
                _HTMLImage(
                    url=src,
                    alt=alt,
                    width=self._dimension(values.get("width")),
                    height=self._dimension(values.get("height")),
                )
            )

    def handle_data(self, data: str) -> None:
        text = re.sub(r"\s+", " ", unescape(data)).strip()
        if not 8 <= len(text) <= 300:
            return
        lowered = text.casefold()
        if any(
            label in lowered
            for label in (
                "view order",
                "track package",
                "your orders",
                "your account",
                "buy again",
                "privacy",
                "terms",
            )
        ):
            return
        # Amazon commonly puts the product title in the text node immediately
        # after an image whose alt attribute is blank.
        if self.images and not self.images[-1].alt:
            image = self.images[-1]
            self.images[-1] = _HTMLImage(
                url=image.url,
                alt=text,
                width=image.width,
                height=image.height,
            )


def _decode_part(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _walk_parts(payload: dict[str, Any]):
    yield payload
    for child in payload.get("parts", []) or []:
        yield from _walk_parts(child)


def _body_text(payload: dict[str, Any]) -> str:
    chunks: list[str] = []
    for part in _walk_parts(payload):
        mime = str(part.get("mimeType", "")).casefold()
        data = part.get("body", {}).get("data")
        if data and mime in {"text/plain", "text/html"}:
            try:
                decoded = _decode_part(data).decode("utf-8", errors="ignore")
                if mime == "text/html":
                    decoded = re.sub(r"<[^>]+>", " ", decoded)
                chunks.append(decoded)
            except Exception:
                logger.debug("gmail_part_decode_failed")
    return re.sub(r"\s+", " ", " ".join(chunks)).strip()[:12000]


def _html_images(payload: dict[str, Any]) -> list[_HTMLImage]:
    images: list[_HTMLImage] = []
    for part in _walk_parts(payload):
        if str(part.get("mimeType", "")).casefold() != "text/html":
            continue
        data = part.get("body", {}).get("data")
        if not data:
            continue
        try:
            html = _decode_part(data).decode("utf-8", errors="ignore")
            parser = _EmailImageParser()
            parser.feed(html)
            images.extend(parser.images)
            # Covers schema.org/JSON-LD image fields and CSS that is not exposed
            # as a normal element attribute.
            searchable_html = html.replace("\\/", "/")
            for url in re.findall(r"https?://[^\s\"'<>\\]+", searchable_html):
                decoded_url = unescape(url).rstrip(")]},;")
                lowered = decoded_url.casefold()
                if any(
                    marker in lowered
                    for marker in ("/images/", ".jpg", ".jpeg", ".png", ".webp")
                ):
                    images.append(_HTMLImage(url=decoded_url))
        except Exception:
            logger.debug("gmail_html_image_parse_failed")
    return images


def _header(payload: dict[str, Any], name: str) -> str:
    expected = name.casefold()
    for header in payload.get("headers", []) or []:
        if str(header.get("name", "")).casefold() == expected:
            return re.sub(r"\s+", " ", str(header.get("value", ""))).strip()
    return ""


def _preview(value: str, limit: int = 30) -> str:
    return re.sub(r"\s+", " ", value).strip()[:limit]


def _log_block(title: str, rows: list[tuple[str, str]]) -> None:
    width = 72
    lines = ["", "=" * width, title, "-" * width]
    lines.extend(f"{label:<12}: {value}" for label, value in rows)
    lines.append("=" * width)
    logger.info("\n%s", "\n".join(lines))


def _log_full_test_email(message: dict[str, Any]) -> None:
    """Print complete readable message content only for explicit order testing."""
    payload = message.get("payload", {})
    lines = [
        "",
        "=" * 96,
        "CLOSET SYNC — FULL TEST EMAIL CONTENT",
        "-" * 96,
        f"Message ID : {message.get('id') or ''}",
        f"Thread ID  : {message.get('threadId') or ''}",
        "",
        "HEADERS",
        "-" * 96,
    ]
    for header in payload.get("headers", []) or []:
        lines.append(f"{header.get('name', '')}: {header.get('value', '')}")

    lines.extend(["", "MIME PARTS", "-" * 96])
    for index, part in enumerate(_walk_parts(payload), start=1):
        mime = str(part.get("mimeType", ""))
        filename = str(part.get("filename") or "")
        body = part.get("body", {}) or {}
        lines.append(
            f"[Part {index}] mime={mime or 'unknown'} "
            f"filename={filename or 'none'} size={body.get('size', 0)} "
            f"attachment_id={body.get('attachmentId') or 'none'}"
        )
        data = body.get("data")
        if data and mime.casefold() in {"text/plain", "text/html"}:
            try:
                decoded = _decode_part(data).decode("utf-8", errors="replace")
                lines.extend([decoded, "-" * 96])
            except Exception as exc:
                lines.append(f"[Could not decode body: {type(exc).__name__}]")

    lines.extend(["", "IMAGE REFERENCES", "-" * 96])
    references = _html_images(payload)
    if not references:
        lines.append("none")
    for index, image in enumerate(references, start=1):
        lines.append(
            f"[{index}] url={image.url}\n"
            f"    alt={image.alt or 'none'} width={image.width or 'unknown'} "
            f"height={image.height or 'unknown'}"
        )
    lines.append("=" * 96)
    logger.info("\n%s", "\n".join(lines))


def _gmail_get(client: httpx.Client, path: str, token: str, **params: Any) -> dict[str, Any]:
    response = client.get(
        f"{GMAIL_API}/{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    )
    response.raise_for_status()
    return response.json()


def _prepare_candidate(
    contents: bytes, hint: str, source_url: str | None = None
) -> _ImageCandidate | None:
    """Validate and normalize remote mail images for the Groq base64 limit."""
    try:
        with Image.open(BytesIO(contents)) as image:
            width, height = image.size
            if (
                width < 64
                or height < 64
                or width > 10000
                or height > 10000
                or not 0.18 <= width / height <= 5.5
            ):
                return None
            rgba = ImageOps.exif_transpose(image).convert("RGBA")
            white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            white.alpha_composite(rgba)
            normalized = white.convert("RGB")
            normalized.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
            output = BytesIO()
            normalized.save(output, format="JPEG", quality=88, optimize=True)
            prepared = output.getvalue()
            if len(prepared) > 4 * 1024 * 1024:
                output = BytesIO()
                normalized.save(output, format="JPEG", quality=72, optimize=True)
                prepared = output.getvalue()
            if not prepared or len(prepared) > 4 * 1024 * 1024:
                return None
            return _ImageCandidate(
                contents=prepared,
                content_type="image/jpeg",
                hint=hint,
                digest=hashlib.sha256(prepared).hexdigest(),
                source_url=source_url,
                width=width,
                height=height,
            )
    except Exception:
        return None


def _allowed_image_url(url: str) -> bool:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").casefold()
    return parsed.scheme in {"http", "https"} and any(
        hostname == suffix or hostname.endswith(f".{suffix}")
        for suffix in ALLOWED_IMAGE_HOST_SUFFIXES
    )


def _likely_content_image(image: _HTMLImage) -> bool:
    if not _allowed_image_url(image.url):
        return False
    marker = f"{image.url} {image.alt}".casefold()
    if any(word in marker for word in ("pixel", "spacer", "tracking", "mailtrack", "facebook", "twitter", "instagram")):
        return False
    if image.width is not None and image.height is not None:
        if image.width < 48 or image.height < 48:
            return False
    return True


def _strong_product_hint(value: str) -> bool:
    hint = re.sub(r"\s+", " ", value).strip()
    lowered = hint.casefold()
    if len(hint) < 12:
        return False
    blocked = (
        "amazon logo", "myntra logo", "flipkart logo", "ajio logo",
        "your orders", "your account", "view order", "track package",
        "facebook", "instagram", "twitter", "download app",
    )
    return not any(term in lowered for term in blocked)


def _is_likely_product_asset(candidate: _ImageCandidate) -> bool:
    if _strong_product_hint(candidate.hint):
        return True
    url = (candidate.source_url or "").casefold()
    host = (urlparse(url).hostname or "").casefold()
    if (
        host.endswith("amazon.com")
        or host.endswith("amazon.in")
        or host.endswith("images-amazon.com")
    ):
        return "/images/i/" in url
    return False


def _candidate_product_score(candidate: _ImageCandidate) -> int:
    score = 1000 if _strong_product_hint(candidate.hint) else 0
    url = (candidate.source_url or "").casefold()
    if "/images/i/" in url:
        score += 500
    if candidate.width and candidate.height:
        aspect = candidate.width / candidate.height
        if 0.65 <= aspect <= 1.55:
            score += 300
        elif 0.4 <= aspect <= 2.2:
            score += 120
    pixels = (candidate.width or 0) * (candidate.height or 0)
    return score + min(400, pixels // 5000)


def _delivered_item_count(subject: str) -> int:
    match = re.search(r"delivered:\s*(\d+)\s+items?\b", subject, re.I)
    if not match:
        return 1
    return max(1, min(MAX_CANDIDATE_IMAGES_PER_MESSAGE, int(match.group(1))))


FASHION_KEYWORDS: dict[str, str] = {
    "t-shirt": "shirt", "tshirt": "shirt", "shirt": "shirt",
    "sweater": "jacket", "sweatshirt": "jacket", "hoodie": "jacket",
    "jacket": "jacket", "coat": "jacket", "jeans": "pants",
    "trousers": "pants", "pants": "pants", "dress": "dress",
    "kurta": "shirt", "saree": "dress", "shoes": "shoes",
    "sneakers": "shoes", "sandals": "shoes", "slippers": "shoes",
    "cap": "accessory", "beanie": "accessory", "watch": "accessory",
    "bag": "accessory", "belt": "accessory", "socks": "accessory",
}


def _email_fashion_fallback(
    candidate: _ImageCandidate, email_text: str
) -> GmailProductAnalysis | None:
    combined_text = f"{candidate.hint} {email_text}"
    lowered = combined_text.casefold()
    matched = next(
        (
            (keyword, category)
            for keyword, category in FASHION_KEYWORDS.items()
            if re.search(rf"(?<![a-z]){re.escape(keyword)}(?![a-z])", lowered)
        ),
        None,
    )
    if not matched:
        return None
    if not _is_likely_product_asset(candidate):
        url = candidate.source_url or ""
        host = (urlparse(url).hostname or "").casefold()
        is_merchant_cdn = any(
            host == suffix or host.endswith(f".{suffix}")
            for suffix in (
                "media-amazon.com",
                "images-amazon.com",
                "ssl-images-amazon.com",
                "myntassets.com",
                "fkcdn.com",
                "ajio.com",
            )
        )
        if not is_merchant_cdn:
            return None
    keyword, category = matched
    name = candidate.hint.strip() if _strong_product_hint(candidate.hint) else f"Imported {keyword}"
    return GmailProductAnalysis(
        is_fashion_item=True,
        name=name,
        category=category,  # type: ignore[arg-type]
        description="Imported from a delivered ecommerce order email; AI details are pending.",
        tags=["gmail-import"],
    )


def _forced_order_fallback(
    candidate: _ImageCandidate, order_id: str
) -> GmailProductAnalysis | None:
    """Import the best image in explicit single-order test mode without AI."""
    host = (urlparse(candidate.source_url or "").hostname or "").casefold()
    if not any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in (
            "media-amazon.com",
            "images-amazon.com",
            "ssl-images-amazon.com",
            "amazon.in",
            "amazon.com",
        )
    ):
        return None
    name = (
        candidate.hint.strip()
        if _strong_product_hint(candidate.hint)
        else f"Amazon order {order_id} item"
    )
    return GmailProductAnalysis(
        is_fashion_item=True,
        name=name,
        category="other",
        description=(
            "Imported from an explicitly selected Amazon delivery email. "
            "Review the item details in StyleStack."
        ),
        tags=["gmail-import", "needs-review"],
    )


def _remote_candidate(http: httpx.Client, image: _HTMLImage) -> _ImageCandidate | None:
    try:
        response = http.get(
            image.url,
            headers={"User-Agent": "StyleStack/1.0", "Accept": "image/*"},
            follow_redirects=True,
        )
        response.raise_for_status()
        if not _allowed_image_url(str(response.url)):
            return None
        content_type = response.headers.get("content-type", "").split(";")[0].casefold()
        if content_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
            return None
        contents = response.content
        if not contents or len(contents) > MAX_REMOTE_IMAGE_BYTES:
            return None
        return _prepare_candidate(contents, image.alt, image.url)
    except Exception:
        return None


def _inline_candidates(
    http: httpx.Client, access_token: str, message_id: str, payload: dict[str, Any]
) -> list[_ImageCandidate]:
    result: list[_ImageCandidate] = []
    for part in _walk_parts(payload):
        content_type = str(part.get("mimeType", "")).casefold()
        if content_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
            continue
        body = part.get("body", {})
        try:
            data = body.get("data")
            if data:
                contents = _decode_part(data)
            elif body.get("attachmentId"):
                attachment = _gmail_get(
                    http,
                    f"messages/{message_id}/attachments/{body['attachmentId']}",
                    access_token,
                )
                contents = _decode_part(attachment["data"])
            else:
                continue
            if len(contents) <= MAX_REMOTE_IMAGE_BYTES:
                candidate = _prepare_candidate(
                    contents, str(part.get("filename") or "")
                )
                if candidate:
                    result.append(candidate)
        except Exception:
            logger.debug("gmail_inline_image_failed message_id=%s", message_id)
    return result


def _image_candidates(
    http: httpx.Client, access_token: str, message_id: str, payload: dict[str, Any]
) -> list[_ImageCandidate]:
    candidates = _inline_candidates(http, access_token, message_id, payload)
    html_images = [image for image in _html_images(payload) if _likely_content_image(image)]
    html_images.sort(
        key=lambda image: (
            1 if _strong_product_hint(image.alt) else 0,
            (image.width or 0) * (image.height or 0),
        ),
        reverse=True,
    )
    for image in html_images[:20]:
        candidate = _remote_candidate(http, image)
        if candidate:
            candidates.append(candidate)
    unique: list[_ImageCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.digest not in seen:
            seen.add(candidate.digest)
            unique.append(candidate)
    unique.sort(key=_candidate_product_score, reverse=True)
    return unique[:MAX_CANDIDATE_IMAGES_PER_MESSAGE]


def _thread_content(
    http: httpx.Client,
    access_token: str,
    message: dict[str, Any],
) -> tuple[str, list[_ImageCandidate], int, int, list[str]]:
    """Collect text and images from every message in the Gmail conversation."""
    thread_id = str(message.get("threadId") or "")
    messages = [message]
    if thread_id:
        try:
            thread = _gmail_get(
                http, f"threads/{thread_id}", access_token, format="full"
            )
            messages = thread.get("messages", []) or messages
        except Exception:
            logger.debug("gmail_thread_load_failed thread_id=%s", thread_id)

    texts: list[str] = []
    candidates: list[_ImageCandidate] = []
    seen: set[str] = set()
    raw_reference_count = 0
    referenced_hosts: set[str] = set()
    for thread_message in messages:
        payload = thread_message.get("payload", {})
        body = _body_text(payload) or thread_message.get("snippet", "")
        if body:
            texts.append(body)
        references = _html_images(payload)
        raw_reference_count += len(references)
        for reference in references:
            host = (urlparse(reference.url).hostname or "").casefold()
            if host:
                referenced_hosts.add(host)
        message_id = str(thread_message.get("id") or "")
        if not message_id:
            continue
        for candidate in _image_candidates(
            http, access_token, message_id, payload
        ):
            if candidate.digest not in seen:
                seen.add(candidate.digest)
                candidates.append(candidate)
    candidates.sort(key=_candidate_product_score, reverse=True)
    return (
        " ".join(texts)[:12000],
        candidates[:MAX_CANDIDATE_IMAGES_PER_MESSAGE],
        len(messages),
        raw_reference_count,
        sorted(referenced_hosts)[:8],
    )


def _analyze_product_image(candidate: _ImageCandidate, email_text: str) -> GmailProductAnalysis:
    settings = get_settings()
    encoded = base64.b64encode(candidate.contents).decode("ascii")
    context = _preview(email_text, 3500)
    prompt = f"{IMAGE_PROMPT}\nImage hint: {candidate.hint}\nEmail context: {context}"
    groq_error: Exception | None = None
    if settings.groq_api_key:
        try:
            response = httpx.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.groq_api_key}"},
                json={
                    "model": settings.groq_vision_model,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:{candidate.content_type};base64,{encoded}"}},
                        ],
                    }],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.1,
                    "max_completion_tokens": 700,
                },
                timeout=settings.groq_request_timeout_seconds,
            )
            response.raise_for_status()
            return GmailProductAnalysis.model_validate_json(
                response.json()["choices"][0]["message"]["content"]
            )
        except Exception as exc:
            groq_error = exc
    if settings.gemini_api_key:
        return GmailProductAnalysis.model_validate_json(
            gemini_json_from_image(
                prompt, candidate.contents, candidate.content_type
            )
        )
    if groq_error:
        raise groq_error
    raise RuntimeError("No AI vision provider is configured")


def _store_product(
    client: Any,
    uid: str,
    message_id: str,
    candidate: _ImageCandidate,
    analysis: GmailProductAnalysis,
) -> str | None:
    external_id = f"{message_id}:{candidate.digest[:20]}"
    existing = (
        client.table("wardrobe_items")
        .select("id")
        .eq("owner_firebase_uid", uid)
        .eq("import_source", "gmail")
        .eq("source_external_id", external_id)
        .limit(1)
        .execute()
    )
    if existing.data:
        return None

    processed = put_item_on_white_background(candidate.contents, analysis.category)
    image_path = f"{uid}/gmail/{uuid4().hex}.jpg"
    bucket = client.storage.from_(get_settings().supabase_storage_bucket)
    bucket.upload(
        path=image_path,
        file=processed,
        file_options={"content-type": "image/jpeg", "upsert": "false"},
    )
    category = analysis.category or "other"
    color = analysis.color
    name = (analysis.name or " ".join(value for value in (color, category) if value) or "Imported item").strip()
    row = {
        "owner_firebase_uid": uid,
        "name": name[:200],
        "category": category,
        "brand": analysis.brand,
        "color": color,
        "season": [analysis.season] if analysis.season else [],
        "formality": analysis.formality,
        "description": analysis.description,
        "tags": list(dict.fromkeys(tag.strip().lower() for tag in analysis.tags if tag.strip()))[:5],
        "image_path": image_path,
        "tagged": True,
        "ai_tag_status": "completed",
        "ai_category": analysis.category,
        "ai_color": analysis.color,
        "ai_season": analysis.season,
        "ai_formality": analysis.formality,
        "ai_description": analysis.description,
        "import_source": "gmail",
        "source_external_id": external_id,
    }
    try:
        client.table("wardrobe_items").insert(row).execute()
        return image_path
    except Exception:
        try:
            bucket.remove([image_path])
        except Exception:
            logger.warning("gmail_orphan_image_cleanup_failed path=%s", image_path)
        raise


def import_gmail_orders(
    client: Any,
    uid: str,
    access_token: str,
    limit: int,
    *,
    order_id: str | None = None,
) -> tuple[int, int, int]:
    # Temporary production safety gate: never let any client version trigger a
    # full mailbox scan while we diagnose this specific Amazon order.
    if order_id != FORCED_GMAIL_ORDER_ID or limit != 1:
        logger.info(
            "gmail_import_scope_forced requested_order=%s requested_limit=%s "
            "enforced_order=%s",
            order_id or "none",
            limit,
            FORCED_GMAIL_ORDER_ID,
        )
    order_id = FORCED_GMAIL_ORDER_ID
    limit = 1
    scanned = imported = skipped = 0
    groq_rate_limited = False
    with httpx.Client(timeout=30) as http:
        message_refs: list[dict[str, Any]] = []
        seen_message_ids: set[str] = set()
        merchant_matches: dict[str, int] = {}
        merchant_refs: dict[str, list[dict[str, Any]]] = {}
        for merchant, query in MERCHANT_QUERIES.items():
            if order_id and merchant != "amazon":
                merchant_matches[merchant] = 0
                merchant_refs[merchant] = []
                continue
            effective_query = f'{query} "{order_id}"' if order_id else query
            listing = _gmail_get(
                http,
                "messages",
                access_token,
                q=effective_query,
                maxResults=1 if order_id else limit,
            )
            matches = listing.get("messages", []) or []
            merchant_matches[merchant] = len(matches)
            merchant_refs[merchant] = matches

        # Round-robin preserves merchant diversity but automatically gives empty
        # merchant capacity to stores that have more delivery confirmations.
        position = 0
        while len(message_refs) < limit:
            added = False
            for merchant in MERCHANT_QUERIES:
                refs = merchant_refs.get(merchant, [])
                if position >= len(refs):
                    continue
                message_ref = refs[position]
                message_id = str(message_ref.get("id") or "")
                if message_id and message_id not in seen_message_ids:
                    seen_message_ids.add(message_id)
                    message_refs.append(message_ref)
                    added = True
                    if len(message_refs) >= limit:
                        break
            if not added and all(
                position >= len(refs) - 1 for refs in merchant_refs.values()
            ):
                break
            position += 1

        _log_block(
            "CLOSET SYNC — DELIVERY EMAIL SEARCH",
            [(merchant.title(), str(count)) for merchant, count in merchant_matches.items()]
            + [("Unique emails", str(len(message_refs[:limit])))],
        )

        for email_index, message_ref in enumerate(message_refs[:limit], start=1):
            message_id = str(message_ref.get("id") or "")
            if not message_id:
                continue
            scanned += 1
            message = _gmail_get(http, f"messages/{message_id}", access_token, format="full")
            if order_id:
                _log_full_test_email(message)
            payload = message.get("payload", {})
            (
                text,
                candidates,
                thread_message_count,
                raw_image_reference_count,
                image_hosts,
            ) = _thread_content(http, access_token, message)
            message_imported = 0
            rejected = 0
            failed = 0
            rate_limited = groq_rate_limited
            imported_names: list[str] = []
            rejection_reasons: list[str] = []
            fallback_accepted = 0
            fallback_budget = _delivered_item_count(_header(payload, "Subject"))
            candidates_to_process = candidates[:fallback_budget] if order_id else candidates
            for candidate in candidates_to_process:
                try:
                    if order_id:
                        analysis = _email_fashion_fallback(candidate, text)
                        if analysis is None:
                            analysis = _forced_order_fallback(candidate, order_id)
                        if analysis is None:
                            rejected += 1
                            rejection_reasons.append(
                                "best image was not hosted by the selected merchant"
                            )
                            continue
                        fallback_accepted += 1
                    elif rate_limited:
                        if fallback_accepted >= fallback_budget:
                            rejected += 1
                            rejection_reasons.append("extra image beyond delivered item count")
                            continue
                        analysis = _email_fashion_fallback(candidate, text)
                        if analysis is None:
                            rejected += 1
                            rejection_reasons.append(
                                "image lacked matching fashion product evidence"
                            )
                            continue
                        fallback_accepted += 1
                    else:
                        analysis = _analyze_product_image(candidate, text)
                    if not analysis.is_fashion_item:
                        rejected += 1
                        rejection_reasons.append("vision classified image as non-fashion")
                        continue
                    stored_path = _store_product(
                        client, uid, message_id, candidate, analysis
                    )
                    if stored_path:
                        imported += 1
                        message_imported += 1
                        imported_names.append(analysis.name or candidate.hint or "Imported item")
                except Exception as exc:
                    status_code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                    if status_code == 429:
                        rate_limited = True
                        groq_rate_limited = True
                        fallback = _email_fashion_fallback(candidate, text)
                        if fallback is not None:
                            if fallback_accepted >= fallback_budget:
                                rejected += 1
                                rejection_reasons.append(
                                    "extra image beyond delivered item count"
                                )
                                continue
                            fallback_accepted += 1
                            try:
                                stored_path = _store_product(
                                    client, uid, message_id, candidate, fallback
                                )
                                if stored_path:
                                    imported += 1
                                    message_imported += 1
                                    imported_names.append(
                                        fallback.name or "Imported fashion item"
                                    )
                                    continue
                            except Exception:
                                pass
                    failed += 1
            if message_imported == 0:
                skipped += 1
            if get_settings().gmail_import_log_email_previews:
                result = (
                    f"Imported {message_imported}: {', '.join(imported_names)}"
                    if message_imported
                    else "No product imported"
                )
                if rate_limited and candidates:
                    result += " (Groq rate limit encountered; descriptive image names used as fallback)"
                _log_block(
                    f"CLOSET SYNC — EMAIL {email_index} OF {len(message_refs[:limit])}",
                    [
                        ("Subject", _header(payload, "Subject")),
                        ("Test order", order_id or "all eligible orders"),
                        ("From", _header(payload, "From")),
                        ("Preview", _preview(text)),
                        ("Images", f"{len(candidates)} candidate(s)"),
                        ("Thread", f"{thread_message_count} message(s) checked"),
                        ("Raw refs", str(raw_image_reference_count)),
                        ("Image hosts", ", ".join(image_hosts) or "none"),
                        ("Rejected", str(rejected)),
                        (
                            "Why rejected",
                            "; ".join(dict.fromkeys(rejection_reasons)) or "none",
                        ),
                        ("Failed", str(failed)),
                        ("Result", result),
                    ],
                )
        _log_block(
            "CLOSET SYNC — COMPLETE",
            [
                ("Emails", str(scanned)),
                ("Items added", str(imported)),
                ("No item", str(skipped)),
            ],
        )
    return scanned, imported, skipped
