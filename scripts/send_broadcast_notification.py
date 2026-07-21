#!/usr/bin/env python3
"""Send an owner-approved StyleStack announcement through the backend."""

from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", required=True, help="Notification title")
    parser.add_argument("--body", required=True, help="Notification body")
    parser.add_argument(
        "--destination",
        default="today",
        choices=(
            "today",
            "wardrobe",
            "planner",
            "profile",
            "notifications",
            "saved_styles",
            "outfit",
        ),
        help="Screen opened when the notification is tapped",
    )
    parser.add_argument("--outfit-id", help="Required for destination=outfit")
    parser.add_argument("--image-url", help="Optional public HTTPS image URL")
    parser.add_argument(
        "--api-base-url",
        default=os.environ.get(
            "STYLESTACK_API_BASE_URL", "https://stylestack-be.onrender.com/api/v1"
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    key = os.environ.get("STYLESTACK_ADMIN_NOTIFICATION_KEY")
    if not key:
        print(
            "Set STYLESTACK_ADMIN_NOTIFICATION_KEY before running this command.",
            file=sys.stderr,
        )
        return 2

    payload = {
        "title": args.title,
        "body": args.body,
        "destination": args.destination,
        "dry_run": args.dry_run,
    }
    if args.outfit_id:
        payload["outfit_id"] = args.outfit_id
    if args.image_url:
        payload["image_url"] = args.image_url

    request = Request(
        f"{args.api_base_url.rstrip('/')}/admin/notifications/broadcast",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "X-StyleStack-Admin-Key": key,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310
            print(json.dumps(json.load(response), indent=2))
    except HTTPError as exc:
        print(exc.read().decode() or str(exc), file=sys.stderr)
        return 1
    except URLError as exc:
        print(f"Could not reach StyleStack: {exc.reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
