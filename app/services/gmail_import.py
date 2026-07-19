import base64
from dataclasses import dataclass
import hashlib
from html import unescape
from html.parser import HTMLParser
from io import BytesIO
import logging
import re
from typing import Any, Callable, Literal
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from PIL import Image, ImageOps

from app.core.config import get_settings
from app.models.imports import GmailProductAnalysis
from app.services.gemini import gemini_json_from_image
from app.services.groq_rate_limit import groq_rate_gate

logger = logging.getLogger("stylestack.gmail_import")

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
AMAZON_RELATED_ORDER_MAX_MESSAGES = 10
MAX_REMOTE_IMAGE_BYTES = 10 * 1024 * 1024
MAX_CANDIDATE_IMAGES_PER_MESSAGE = 10
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

AMAZON_ORDER_SENDERS_QUERY = (
    "{from:order-update@amazon.in "
    "from:shipment-tracking@amazon.in "
    "from:auto-confirm@amazon.in}"
)
AMAZON_DELIVERED_QUERY = (
    f'{AMAZON_ORDER_SENDERS_QUERY} subject:"Delivered:" '
    '-subject:(cancelled OR refunded OR returned OR replacement) '
    '-subject:"could not be delivered" '
    '-subject:"delivery attempted"'
)

IMAGE_PROMPT = """Analyze this verified ecommerce fashion product for a wardrobe app.
Fashion items include clothing, shoes, bags, jewelry, watches and wearable accessories.
Reject store logos, icons, tracking pixels, banners, models without a clearly featured product, electronics, home goods, beauty products and packaging.
The supplied product title is verified. Keep the identity grounded in that title and use the image for visible color, silhouette, texture, pattern, fit, neckline, sleeve length and styling details.
Never invent an unseen material, brand, pattern or construction detail. Write a useful 2-3 sentence wardrobe description covering what the item is, its visible design, and practical styling/occasion notes.
Return ONLY JSON:
{
  "is_fashion_item": true_or_false,
  "name": "specific concise product name or null",
  "brand": "brand or null",
  "category": "shirt|pants|dress|jacket|shoes|accessory|kurta|saree|lehenga|sherwani|salwar|dhoti|dupatta|blouse|anarkali|ethnic_set|other or null",
  "color": "black|white|red|blue|green|yellow|purple|pink|brown|grey|orange|beige|multicolor or null",
  "season": "summer|winter|spring|autumn|all or null",
  "formality": "formal|semi-formal|casual|sporty or null",
  "description": "accurate detailed 2-3 sentence description, maximum 450 characters",
  "tags": ["exactly 5 concise searchable tags for material/style/fit/pattern/detail"]
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
    email_width: int | None = None
    email_height: int | None = None
    is_order_thumbnail: bool = False


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
    contents: bytes,
    hint: str,
    source_url: str | None = None,
    *,
    email_width: int | None = None,
    email_height: int | None = None,
    is_order_thumbnail: bool = False,
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
                email_width=email_width,
                email_height=email_height,
                is_order_thumbnail=is_order_thumbnail,
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


def _original_amazon_image_url(url: str) -> str:
    """Remove Amazon thumbnail transforms while retaining the catalog image ID."""
    host = (urlparse(url).hostname or "").casefold()
    if not host.endswith("amazon.com"):
        return url
    return re.sub(
        r"\.(?:\*[a-z0-9_,]+\*|_[a-z0-9_,]+_)\.(?=jpe?g(?:\?|$))",
        ".",
        url,
        flags=re.I,
    )


def _is_amazon_order_thumbnail_url(url: str) -> bool:
    """Amazon transactional rows use SS thumbnails; SR assets are carousels."""
    return bool(
        re.search(
            r"\.(?:\*ss\d+\*|_ss\d+_)\.(?=jpe?g(?:\?|$))",
            url,
            flags=re.I,
        )
    )


def _likely_content_image(image: _HTMLImage) -> bool:
    if not _allowed_image_url(image.url):
        return False
    marker = f"{image.url} {image.alt}".casefold()
    if any(
        word in marker
        for word in (
            "pixel",
            "spacer",
            "tracking",
            "mailtrack",
            "facebook",
            "twitter",
            "instagram",
            "amazon logo",
            "smile_logo",
            "outboundtemplates",
            "infoicon",
            "/nav/",
            ".woff",
        )
    ):
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
    if _is_transactional_amazon_product(candidate):
        score += 5_000
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


def _best_unique_candidates(
    candidates: list[_ImageCandidate],
) -> list[_ImageCandidate]:
    by_digest: dict[str, _ImageCandidate] = {}
    for candidate in candidates:
        current = by_digest.get(candidate.digest)
        if current is None or _candidate_product_score(
            candidate
        ) > _candidate_product_score(current):
            by_digest[candidate.digest] = candidate
    result = list(by_digest.values())
    result.sort(key=_candidate_product_score, reverse=True)
    return result[:MAX_CANDIDATE_IMAGES_PER_MESSAGE]


def _is_transactional_amazon_product(candidate: _ImageCandidate) -> bool:
    """Distinguish purchased-item thumbnails from Amazon recommendations."""
    url = (candidate.source_url or "").casefold()
    host = (urlparse(url).hostname or "").casefold()
    if not (
        _strong_product_hint(candidate.hint)
        and "/images/i/" in url
        and host.endswith("amazon.com")
        and candidate.is_order_thumbnail
    ):
        return False
    if candidate.email_width is None or candidate.email_height is None:
        return True
    return 48 <= candidate.email_width <= 200 and 48 <= candidate.email_height <= 200


def _delivered_item_count(subject: str) -> int:
    match = re.search(r"delivered:\s*(\d+)\s+items?\b", subject, re.I)
    if not match:
        return 1
    return max(1, min(MAX_CANDIDATE_IMAGES_PER_MESSAGE, int(match.group(1))))


def _is_delivered_amazon_message(payload: dict[str, Any]) -> bool:
    sender = _header(payload, "From").casefold()
    subject = _header(payload, "Subject").strip()
    if not any(
        address in sender
        for address in (
            "order-update@amazon.in",
            "shipment-tracking@amazon.in",
        )
    ):
        return False
    if not re.match(r"^delivered\s*:", subject, re.I):
        return False
    rejected = (
        "could not be delivered",
        "delivery attempted",
        "not delivered",
        "cancelled",
        "refunded",
        "returned",
        "replacement",
    )
    return not any(term in subject.casefold() for term in rejected)


def _amazon_order_id(payload: dict[str, Any], text: str = "") -> str | None:
    searchable = f"{_header(payload, 'Subject')} {text}"
    match = re.search(r"(?<!\d)(\d{3}-\d{7}-\d{7})(?!\d)", searchable)
    return match.group(1) if match else None


def _amazon_thread_order_id(
    http: httpx.Client,
    access_token: str,
    message: dict[str, Any],
) -> str | None:
    thread_id = str(message.get("threadId") or "")
    if not thread_id:
        return None
    try:
        thread = _gmail_get(
            http,
            f"threads/{thread_id}",
            access_token,
            format="full",
        )
        for thread_message in thread.get("messages", []) or []:
            payload = thread_message.get("payload", {})
            order_id = _amazon_order_id(
                payload,
                _body_text(payload) or str(thread_message.get("snippet") or ""),
            )
            if order_id:
                return order_id
    except Exception:
        logger.debug("gmail_thread_order_id_failed thread_id=%s", thread_id)
    return None


FASHION_KEYWORDS: dict[str, str] = {
    "t-shirt": "shirt", "tshirt": "shirt", "shirt": "shirt",
    "sweater": "jacket", "sweatshirt": "jacket", "hoodie": "jacket",
    "jacket": "jacket", "coat": "jacket", "jeans": "pants",
    "trousers": "pants", "pants": "pants", "dress": "dress",
    "kurta": "kurta", "saree": "saree", "lehenga": "lehenga",
    "sherwani": "sherwani", "salwar": "salwar", "dhoti": "dhoti",
    "dupatta": "dupatta", "anarkali": "anarkali", "blouse": "blouse",
    "shoes": "shoes",
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


def _amazon_product_from_title(
    candidate: _ImageCandidate,
) -> GmailProductAnalysis | None:
    """Build safe metadata from the purchased item's own Amazon title."""
    if not _is_transactional_amazon_product(candidate):
        return None
    name = re.sub(r"\s+", " ", candidate.hint).strip()
    lowered = name.casefold()
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
    keyword, category = matched

    color = next(
        (
            value
            for value in (
                "black",
                "white",
                "red",
                "blue",
                "green",
                "yellow",
                "purple",
                "pink",
                "brown",
                "grey",
                "orange",
                "beige",
                "multicolor",
            )
            if re.search(rf"(?<![a-z]){value}(?![a-z])", lowered)
        ),
        None,
    )
    brand_match = re.match(r"\s*([^|,(]{2,60}?)(?:®|™)", name)
    if not brand_match:
        brand_match = re.match(
            r"\s*([a-z0-9& .'-]{2,40}?)(?=\s+(?:men|women|boys|girls|unisex)(?:'s|’s|\s))",
            name,
            re.I,
        )
    brand = brand_match.group(1).strip(" -") if brand_match else None
    material = next(
        (
            term
            for term in (
                "cotton",
                "linen",
                "denim",
                "wool",
                "woolen",
                "acrylic",
                "fleece",
                "leather",
                "polyester",
                "silk",
                "knit",
            )
            if term in lowered
        ),
        None,
    )
    sleeve = next(
        (term for term in ("full sleeve", "long sleeve", "short sleeve", "sleeveless") if term in lowered),
        None,
    )
    neckline = next(
        (term for term in ("high neck", "crew neck", "round neck", "v-neck", "polo neck") if term in lowered),
        None,
    )
    gender = next(
        (label for term, label in (("men's", "men's"), ("women's", "women's"), ("unisex", "unisex")) if term in lowered),
        None,
    )
    season = (
        "winter"
        if any(
            term in lowered
            for term in ("winter", "woolen", "thermal", "high neck")
        )
        else "all"
    )
    if category == "accessory" and any(term in lowered for term in ("muffler", "neck warmer")):
        description = " ".join(
            part
            for part in (
                color.title() if color else None,
                "winter beanie cap with a matching neck-warmer/muffler set",
                f"by {brand}." if brand else ".",
            )
            if part
        ).replace(" .", ".")
    else:
        label = {
            "shirt": "shirt",
            "pants": "pair of pants",
            "dress": "dress",
            "jacket": "jacket",
            "shoes": "pair of shoes",
            "accessory": keyword,
            "kurta": "kurta",
            "saree": "saree",
            "lehenga": "lehenga outfit",
            "sherwani": "sherwani",
            "salwar": "salwar suit",
            "dhoti": "dhoti",
            "dupatta": "dupatta",
            "blouse": "blouse",
            "anarkali": "anarkali dress",
            "ethnic_set": "ethnic outfit",
            "other": "fashion item",
        }[category]
        attributes = [
            gender,
            color,
            material,
            sleeve,
            neckline,
            label,
        ]
        article = "An" if attributes and attributes[0] and attributes[0][0] in "aeiou" else "A"
        description = (
            f"{article} {' '.join(part for part in attributes if part)}"
            f"{' by ' + brand if brand else ''}. "
            f"Designed for casual {('cool-weather layering' if season == 'winter' else 'everyday styling')}."
        )
    tags = [keyword]
    tag_evidence = {
        "winter": ("winter",),
        "beanie": ("beanie",),
        "neck-warmer": ("neck warmer", "muffler"),
        "unisex": ("unisex", "men & women", "men and women"),
        "knitwear": ("knit", "knitted"),
    }
    for tag, evidence in tag_evidence.items():
        if any(term in lowered for term in evidence) and tag not in tags:
            tags.append(tag)
    for tag in (material, sleeve, neckline, gender):
        if tag and tag not in tags:
            tags.append(tag)
    if color and color not in tags:
        tags.append(color)
    return GmailProductAnalysis(
        is_fashion_item=True,
        name=name,
        brand=brand,
        category=category,  # type: ignore[arg-type]
        color=color,  # type: ignore[arg-type]
        season=season,
        formality="casual",
        description=description,
        tags=tags[:5],
    )


def _enrich_amazon_product_with_ai(
    candidate: _ImageCandidate,
    verified: GmailProductAnalysis,
) -> tuple[GmailProductAnalysis, bool]:
    """Enrich one verified purchase; never let AI change its identity."""
    context = (
        f"Verified purchased product title: {verified.name}. "
        f"Verified brand: {verified.brand or 'unknown'}. "
        f"Verified category: {verified.category or 'unknown'}. "
        "Describe only this product; ignore any recommendation items."
    )
    try:
        ai = _analyze_product_image(candidate, context)
        description = (ai.description or "").strip()
        if len(description) < 50:
            description = verified.description or description
        tags: list[str] = []
        for raw_tag in [*ai.tags, *verified.tags]:
            tag = re.sub(r"\s+", " ", raw_tag).strip().casefold()
            if tag and tag not in tags:
                tags.append(tag)
            if len(tags) == 5:
                break
        enriched = verified.model_copy(
            update={
                # Explicit attributes in the purchased title are stronger
                # evidence than vision guesses. AI fills only missing values.
                "color": verified.color or ai.color,
                "season": (
                    ai.season
                    if verified.season in (None, "all") and ai.season
                    else verified.season
                ),
                "formality": ai.formality or verified.formality,
                "description": description,
                "tags": tags,
            }
        )
        logger.info(
            "gmail_product_ai_enriched name=%r tags=%s",
            enriched.name,
            ",".join(tags),
        )
        return enriched, True
    except Exception as exc:
        status_code = (
            exc.response.status_code
            if isinstance(exc, httpx.HTTPStatusError)
            else None
        )
        logger.warning(
            "gmail_product_ai_fallback name=%r error_type=%s status=%s",
            verified.name,
            type(exc).__name__,
            status_code or "none",
        )
        return verified, False


def _remote_candidate(http: httpx.Client, image: _HTMLImage) -> _ImageCandidate | None:
    original_url = _original_amazon_image_url(image.url)
    for download_url in dict.fromkeys((original_url, image.url)):
        try:
            response = http.get(
                download_url,
                headers={"User-Agent": "StyleStack/1.0", "Accept": "image/*"},
                follow_redirects=True,
            )
            response.raise_for_status()
            if not _allowed_image_url(str(response.url)):
                continue
            content_type = (
                response.headers.get("content-type", "").split(";")[0].casefold()
            )
            if content_type not in {
                "image/jpeg",
                "image/png",
                "image/webp",
                "image/gif",
            }:
                continue
            contents = response.content
            if not contents or len(contents) > MAX_REMOTE_IMAGE_BYTES:
                continue
            candidate = _prepare_candidate(
                contents,
                image.alt,
                download_url,
                email_width=image.width,
                email_height=image.height,
                is_order_thumbnail=_is_amazon_order_thumbnail_url(image.url),
            )
            if candidate:
                return candidate
        except Exception:
            continue
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
    html_images = [
        image
        for image in _html_images(payload)
        if _likely_content_image(image)
        and _is_amazon_order_thumbnail_url(image.url)
    ]
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
    return _best_unique_candidates(candidates)


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
        candidates.extend(
            _image_candidates(http, access_token, message_id, payload)
        )
    candidates = _best_unique_candidates(candidates)
    return (
        " ".join(texts)[:12000],
        candidates[:MAX_CANDIDATE_IMAGES_PER_MESSAGE],
        len(messages),
        raw_reference_count,
        sorted(referenced_hosts)[:8],
    )


def _amazon_order_content(
    http: httpx.Client,
    access_token: str,
    delivered_message: dict[str, Any],
    order_id: str | None,
) -> tuple[str, list[_ImageCandidate], int, int, list[str]]:
    """Enrich one delivered confirmation using only that order's Amazon mail."""
    messages = [delivered_message]
    seen_message_ids = {str(delivered_message.get("id") or "")}
    if order_id:
        try:
            listing = _gmail_get(
                http,
                "messages",
                access_token,
                q=f'{AMAZON_ORDER_SENDERS_QUERY} "{order_id}"',
                maxResults=AMAZON_RELATED_ORDER_MAX_MESSAGES,
            )
            for reference in listing.get("messages", []) or []:
                message_id = str(reference.get("id") or "")
                if not message_id or message_id in seen_message_ids:
                    continue
                seen_message_ids.add(message_id)
                messages.append(
                    _gmail_get(
                        http,
                        f"messages/{message_id}",
                        access_token,
                        format="full",
                    )
                )
        except Exception:
            logger.warning("gmail_order_enrichment_failed order_id=%s", order_id)

    texts: list[str] = []
    candidates: list[_ImageCandidate] = []
    raw_reference_count = 0
    image_hosts: set[str] = set()
    checked_messages = 0
    processed_threads: set[str] = set()
    for message in messages:
        thread_key = str(message.get("threadId") or message.get("id") or "")
        if thread_key and thread_key in processed_threads:
            continue
        if thread_key:
            processed_threads.add(thread_key)
        text, found, thread_count, raw_count, hosts = _thread_content(
            http,
            access_token,
            message,
        )
        if text:
            texts.append(text)
        checked_messages += thread_count
        raw_reference_count += raw_count
        image_hosts.update(hosts)
        candidates.extend(found)
    candidates = _best_unique_candidates(candidates)
    return (
        " ".join(texts)[:12000],
        candidates[:MAX_CANDIDATE_IMAGES_PER_MESSAGE],
        checked_messages,
        raw_reference_count,
        sorted(image_hosts)[:8],
    )


def _analyze_product_image(candidate: _ImageCandidate, email_text: str) -> GmailProductAnalysis:
    settings = get_settings()
    encoded = base64.b64encode(candidate.contents).decode("ascii")
    context = _preview(email_text, 3500)
    prompt = f"{IMAGE_PROMPT}\nImage hint: {candidate.hint}\nEmail context: {context}"
    groq_error: Exception | None = None
    if settings.groq_api_key:
        try:
            response = groq_rate_gate.post(
                headers={"Authorization": f"Bearer {settings.groq_api_key}"},
                payload={
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


def _normalized_tags(*tag_groups: list[str]) -> list[str]:
    tags: list[str] = []
    for raw_tag in (tag for group in tag_groups for tag in group):
        tag = re.sub(r"\s+", " ", raw_tag).strip().casefold()
        if tag and tag not in tags:
            tags.append(tag)
        if len(tags) == 5:
            break
    return tags


def _is_generated_gmail_description(description: Any) -> bool:
    if not isinstance(description, str) or not description.strip():
        return True
    normalized = description.strip().casefold()
    generated_markers = (
        "from a confirmed amazon delivery",
        "imported from a delivered ecommerce order email",
        "imported from an explicitly selected amazon delivery email",
        "ai details are pending",
    )
    return any(marker in normalized for marker in generated_markers)


def _store_product(
    client: Any,
    uid: str,
    source_id: str,
    candidate: _ImageCandidate,
    analysis: GmailProductAnalysis,
) -> Literal["created", "updated"]:
    category = analysis.category or "other"
    color = analysis.color
    name = (
        analysis.name
        or " ".join(value for value in (color, category) if value)
        or "Imported item"
    ).strip()
    external_id = f"amazon:{source_id}:{candidate.digest[:20]}"
    existing = (
        client.table("wardrobe_items")
        .select(
            "id,brand,color,season,formality,description,tags,source_external_id"
        )
        .eq("owner_firebase_uid", uid)
        .eq("import_source", "gmail")
        .eq("source_external_id", external_id)
        .limit(1)
        .execute()
    )
    existing_row = existing.data[0] if existing.data else None
    if existing_row is None:
        legacy_existing = (
            client.table("wardrobe_items")
            .select(
                "id,brand,color,season,formality,description,tags,source_external_id"
            )
            .eq("owner_firebase_uid", uid)
            .eq("import_source", "gmail")
            .eq("name", name[:200])
            .limit(1)
            .execute()
        )
        existing_row = legacy_existing.data[0] if legacy_existing.data else None

    tags = _normalized_tags(
        analysis.tags,
        list(existing_row.get("tags") or []) if existing_row else [],
    )
    ai_payload = {
        "tagged": True,
        "ai_tag_status": "completed",
        "ai_category": analysis.category,
        "ai_color": analysis.color,
        "ai_season": analysis.season,
        "ai_formality": analysis.formality,
        "ai_description": analysis.description,
        "tags": tags,
    }
    if existing_row is not None:
        update_payload = dict(ai_payload)
        if _is_generated_gmail_description(existing_row.get("description")):
            update_payload["description"] = analysis.description
        for field, value in (
            ("brand", analysis.brand),
            ("color", analysis.color),
            ("formality", analysis.formality),
        ):
            if not existing_row.get(field) and value:
                update_payload[field] = value
        if not existing_row.get("season") and analysis.season:
            update_payload["season"] = [analysis.season]
        if not existing_row.get("source_external_id"):
            update_payload["source_external_id"] = external_id
        (
            client.table("wardrobe_items")
            .update(update_payload)
            .eq("id", existing_row["id"])
            .eq("owner_firebase_uid", uid)
            .execute()
        )
        logger.info(
            "gmail_existing_product_enriched item_id=%s name=%r",
            existing_row["id"],
            name,
        )
        return "updated"

    # Merchant catalog images already have a clean product background. Running
    # garment segmentation on small thumbnails destroys most of the product.
    processed = candidate.contents
    image_path = f"{uid}/gmail/{uuid4().hex}.jpg"
    bucket = client.storage.from_(get_settings().supabase_storage_bucket)
    bucket.upload(
        path=image_path,
        file=processed,
        file_options={"content-type": "image/jpeg", "upsert": "false"},
    )
    row = {
        "owner_firebase_uid": uid,
        "name": name[:200],
        "category": category,
        "brand": analysis.brand,
        "color": color,
        "season": [analysis.season] if analysis.season else [],
        "formality": analysis.formality,
        "description": analysis.description,
        "tags": tags,
        "image_path": image_path,
        **ai_payload,
        "import_source": "gmail",
        "source_external_id": external_id,
    }
    try:
        client.table("wardrobe_items").insert(row).execute()
        return "created"
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
    limit: int | None,
    on_progress: Callable[[int, int, int], None] | None = None,
) -> tuple[int, int, int]:
    scanned = imported = skipped = 0
    processed_new = 0
    existing_order_ids: set[str] = set()
    try:
        existing_rows = (
            client.table("wardrobe_items")
            .select("source_external_id")
            .eq("owner_firebase_uid", uid)
            .eq("import_source", "gmail")
            .execute()
        )
        for row in existing_rows.data or []:
            external_id = str(row.get("source_external_id") or "")
            match = re.match(r"amazon:([^:]+):", external_id)
            if match:
                existing_order_ids.add(match.group(1))
    except Exception:
        logger.warning("gmail_existing_orders_unavailable uid=%s", uid)

    def report_progress() -> None:
        if on_progress:
            on_progress(scanned, imported, skipped)

    with httpx.Client(timeout=30) as http:
        page_token: str | None = None
        page_number = 0
        message_refs: list[dict[str, Any]] = []
        while True:
            if not message_refs:
                if page_number > 0 and page_token is None:
                    break
                params: dict[str, Any] = {
                    "q": AMAZON_DELIVERED_QUERY,
                    "maxResults": 100,
                }
                if page_token:
                    params["pageToken"] = page_token
                listing = _gmail_get(
                    http,
                    "messages",
                    access_token,
                    **params,
                )
                message_refs = list(listing.get("messages", []) or [])
                next_token = listing.get("nextPageToken")
                page_token = str(next_token) if next_token else None
                page_number += 1
                _log_block(
                    "CLOSET SYNC — AMAZON DELIVERED EMAIL SEARCH",
                    [
                        ("Page", str(page_number)),
                        ("Delivered matches on page", str(len(message_refs))),
                        (
                            "Import scope",
                            "all new delivered orders"
                            if limit is None
                            else f"up to {limit} new delivered orders",
                        ),
                    ],
                )
                if not message_refs:
                    continue

            if limit is not None and processed_new >= limit:
                break
            message_ref = message_refs.pop(0)
            message_id = str(message_ref.get("id") or "")
            if not message_id:
                continue
            scanned += 1
            message = _gmail_get(http, f"messages/{message_id}", access_token, format="full")
            payload = message.get("payload", {})
            subject = _header(payload, "Subject")
            sender = _header(payload, "From")
            if not _is_delivered_amazon_message(payload):
                skipped += 1
                logger.info(
                    "gmail_message_ignored reason=not_confirmed_amazon_delivery "
                    "subject=%r from=%r",
                    subject,
                    sender,
                )
                report_progress()
                continue
            root_text = _body_text(payload) or str(message.get("snippet") or "")
            order_id = _amazon_order_id(payload, root_text)
            if not order_id:
                order_id = _amazon_thread_order_id(http, access_token, message)
            if order_id and order_id in existing_order_ids:
                skipped += 1
                logger.debug(
                    "gmail_delivered_order_already_imported order_id=%s",
                    order_id,
                )
                report_progress()
                continue
            processed_new += 1
            email_index = processed_new
            (
                text,
                candidates,
                thread_message_count,
                raw_image_reference_count,
                image_hosts,
            ) = _amazon_order_content(
                http,
                access_token,
                message,
                order_id,
            )
            message_imported = 0
            message_created = 0
            message_updated = 0
            rejected = 0
            failed = 0
            imported_names: list[str] = []
            rejection_reasons: list[str] = []
            item_budget = _delivered_item_count(subject)
            candidates_to_process = [
                candidate
                for candidate in candidates
                if _is_transactional_amazon_product(candidate)
            ]
            for candidate in candidates_to_process:
                if message_imported >= item_budget:
                    break
                try:
                    verified = _amazon_product_from_title(candidate)
                    if verified is None or not verified.is_fashion_item:
                        rejected += 1
                        rejection_reasons.append(
                            "delivered product was not a supported fashion item"
                        )
                        continue
                    analysis, _ = _enrich_amazon_product_with_ai(
                        candidate,
                        verified,
                    )
                    storage_result = _store_product(
                        client,
                        uid,
                        order_id or message_id,
                        candidate,
                        analysis,
                    )
                    imported += 1
                    message_imported += 1
                    if storage_result == "created":
                        message_created += 1
                    else:
                        message_updated += 1
                    imported_names.append(
                        analysis.name or candidate.hint or "Imported item"
                    )
                    if order_id:
                        existing_order_ids.add(order_id)
                except Exception as exc:
                    failed += 1
                    logger.warning(
                        "gmail_product_import_failed order_id=%s hint=%r "
                        "error_type=%s",
                        order_id or "unknown",
                        candidate.hint,
                        type(exc).__name__,
                    )
            if message_imported == 0:
                skipped += 1
            if get_settings().gmail_import_log_email_previews:
                result = (
                    (
                        f"Created {message_created}, enriched {message_updated}: "
                        f"{', '.join(imported_names)}"
                    )
                    if message_imported
                    else "No product imported"
                )
                _log_block(
                    f"CLOSET SYNC — NEW DELIVERED EMAIL {email_index}",
                    [
                        ("Subject", subject),
                        ("Order", order_id or "not found"),
                        ("From", sender),
                        ("Delivery", "confirmed"),
                        ("Preview", _preview(text)),
                        (
                            "Products",
                            f"{len(candidates_to_process)} purchased candidate(s)",
                        ),
                        (
                            "Order mail",
                            f"{thread_message_count} related message(s) checked",
                        ),
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
            report_progress()
        _log_block(
            "CLOSET SYNC — COMPLETE",
            [
                ("Emails", str(scanned)),
                ("Items added/enriched", str(imported)),
                ("No item", str(skipped)),
            ],
        )
    return scanned, imported, skipped
