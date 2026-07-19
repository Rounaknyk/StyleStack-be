import io
import unittest
from unittest.mock import MagicMock, patch

import httpx
from PIL import Image

from app.services.ai_request_queue import AiRequestQueue
from app.services.groq_rate_limit import GroqRollingRateGate
from app.services.image_fingerprint import perceptual_hash


class PerceptualHashTests(unittest.TestCase):
    def _image(self, color: tuple[int, int, int]) -> bytes:
        image = Image.new("RGB", (48, 32), color)
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    def test_same_visual_content_has_same_hash(self) -> None:
        first = self._image((20, 40, 80))
        with Image.open(io.BytesIO(first)) as source:
            output = io.BytesIO()
            source.save(output, format="JPEG", quality=95)
        self.assertEqual(perceptual_hash(first), perceptual_hash(output.getvalue()))


class AiRequestQueueTests(unittest.TestCase):
    def test_cancel_removes_only_owned_queued_job(self) -> None:
        queue = AiRequestQueue()
        job = queue.enqueue(
            owner_uid="user-1",
            kind="single",
            image=b"image",
            content_type="image/jpeg",
            image_hash="abc",
        )
        self.assertIsNone(queue.cancel(job.id, "user-2"))
        canceled = queue.cancel(job.id, "user-1")
        self.assertIsNotNone(canceled)
        self.assertEqual(canceled.state, "canceled")
        self.assertEqual(queue.snapshot(canceled)["queue_position"], None)

    def test_snapshot_reports_position_and_eta(self) -> None:
        queue = AiRequestQueue()
        first = queue.enqueue(
            owner_uid="user-1",
            kind="single",
            image=b"one",
            content_type="image/jpeg",
            image_hash="one",
        )
        second = queue.enqueue(
            owner_uid="user-2",
            kind="single",
            image=b"two",
            content_type="image/jpeg",
            image_hash="two",
        )
        self.assertEqual(queue.snapshot(first)["queue_position"], 1)
        self.assertEqual(queue.snapshot(second)["items_ahead"], 1)
        self.assertGreater(queue.snapshot(second)["estimated_wait_seconds"], 0)

    def test_simultaneous_duplicate_jobs_share_one_queue_position(self) -> None:
        queue = AiRequestQueue()
        leader = queue.enqueue(
            owner_uid="user-1",
            kind="single",
            image=b"one",
            content_type="image/jpeg",
            image_hash="same-image",
        )
        duplicate = queue.enqueue(
            owner_uid="user-2",
            kind="single",
            image=b"two",
            content_type="image/jpeg",
            image_hash="same-image",
        )

        self.assertEqual(duplicate.leader_job_id, leader.id)
        self.assertEqual(queue.snapshot(leader)["queue_position"], 1)
        self.assertEqual(queue.snapshot(duplicate)["queue_position"], 1)


class GroqRetryTests(unittest.TestCase):
    @patch("app.services.groq_rate_limit.time.sleep")
    @patch("app.services.groq_rate_limit.httpx.post")
    def test_429_honors_retry_after_then_retries(
        self, post: MagicMock, sleep: MagicMock
    ) -> None:
        request = httpx.Request("POST", "https://api.groq.com")
        limited = httpx.Response(
            429, headers={"Retry-After": "1.5"}, request=request
        )
        success = httpx.Response(200, json={"ok": True}, request=request)
        post.side_effect = [limited, success]
        gate = GroqRollingRateGate()
        gate.acquire = MagicMock(return_value=0.0)  # type: ignore[method-assign]

        response = gate.post(headers={}, payload={}, timeout=2)

        self.assertEqual(response.status_code, 200)
        sleep.assert_called_once_with(1.5)
        self.assertEqual(post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
