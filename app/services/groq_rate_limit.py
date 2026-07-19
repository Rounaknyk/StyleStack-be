from collections import deque
from email.utils import parsedate_to_datetime
import logging
from threading import Condition, Lock
import time

import httpx

from app.core.config import get_settings

logger = logging.getLogger("stylestack.groq")


class GroqRollingRateGate:
    """One process-wide rolling-window limiter for every Groq HTTP request."""

    def __init__(self) -> None:
        self._condition = Condition()
        self._requests: deque[float] = deque()
        self._request_lock = Lock()
        self._last_request_at: float | None = None

    def acquire(self) -> float:
        settings = get_settings()
        limit = max(1, settings.groq_requests_per_minute)
        window = 60.0
        waited = 0.0
        with self._condition:
            while True:
                now = time.monotonic()
                while self._requests and now - self._requests[0] >= window:
                    self._requests.popleft()
                minimum_interval = window / limit
                interval_wait = (
                    max(0.0, minimum_interval - (now - self._last_request_at))
                    if self._last_request_at is not None
                    else 0.0
                )
                if len(self._requests) < limit and interval_wait <= 0:
                    self._requests.append(now)
                    self._last_request_at = now
                    self._condition.notify_all()
                    return waited
                window_wait = (
                    window - (now - self._requests[0]) + 0.01
                    if len(self._requests) >= limit
                    else 0.0
                )
                delay = max(0.05, interval_wait, window_wait)
                started = time.monotonic()
                self._condition.wait(timeout=delay)
                waited += time.monotonic() - started

    def retry_after_seconds(self, response: httpx.Response) -> float:
        raw = response.headers.get("retry-after")
        if raw:
            try:
                return max(0.0, float(raw))
            except ValueError:
                try:
                    target = parsedate_to_datetime(raw)
                    return max(0.0, target.timestamp() - time.time())
                except (TypeError, ValueError):
                    pass
        try:
            payload = response.json()
            message = str(payload.get("error", {}).get("message", ""))
            marker = "try again in "
            if marker in message.lower():
                suffix = message.lower().split(marker, 1)[1]
                value = suffix.split("s", 1)[0].strip()
                return max(0.0, float(value))
        except (TypeError, ValueError, AttributeError):
            pass
        return get_settings().groq_default_retry_after_seconds

    def post(
        self,
        *,
        headers: dict[str, str],
        payload: dict[str, object],
        timeout: float,
    ) -> httpx.Response:
        """POST once, then honor 429 Retry-After and retry exactly once."""
        with self._request_lock:
            last_response: httpx.Response | None = None
            for attempt in range(2):
                waited = self.acquire()
                if waited >= 0.1:
                    logger.info("groq_rate_gate_waited wait_seconds=%.2f", waited)
                response = httpx.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=timeout,
                )
                last_response = response
                if response.status_code != 429:
                    response.raise_for_status()
                    return response
                retry_after = self.retry_after_seconds(response)
                logger.warning(
                    "groq_rate_limited attempt=%s retry_after_seconds=%.2f",
                    attempt + 1,
                    retry_after,
                )
                if attempt == 0:
                    time.sleep(min(max(retry_after, 0.1), 120.0))
            assert last_response is not None
            last_response.raise_for_status()
            return last_response


groq_rate_gate = GroqRollingRateGate()
