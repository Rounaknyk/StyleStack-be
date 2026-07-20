import time
import unittest
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from app.services.ai_request_queue import AiRequestJob
from app.services.background_jobs import BackgroundJobQueue, ImageTaggingJob


class ControlledJobQueue(BackgroundJobQueue):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()

    def _process_image_pipeline(self, job: ImageTaggingJob) -> None:
        self.started.set()
        self.release.wait(timeout=2)


class BackgroundJobQueueTests(unittest.TestCase):
    def test_enqueue_returns_without_waiting_for_job(self) -> None:
        queue = ControlledJobQueue()
        queue.start()
        started_at = time.perf_counter()

        queued = queue.enqueue(ImageTaggingJob("item-123", "uid/image.jpg"))
        elapsed = time.perf_counter() - started_at

        self.assertTrue(queued)
        self.assertLess(elapsed, 0.1)
        self.assertTrue(queue.started.wait(timeout=1))
        queue.release.set()
        queue.stop()

    def test_pipeline_optimizes_and_completes_outside_request(self) -> None:
        class FakeBucket:
            def __init__(self) -> None:
                self.uploads: list[str] = []
                self.removed: list[str] = []

            def download(self, path: str) -> bytes:
                return b"incoming-image"

            def upload(self, *, path: str, file: bytes, file_options: dict) -> None:
                self.uploads.append(path)

            def remove(self, paths: list[str]) -> None:
                self.removed.extend(paths)

        class FakeTable:
            def __init__(self) -> None:
                self.updates: list[dict] = []

            def update(self, payload: dict):
                self.updates.append(payload)
                return self

            def eq(self, field: str, value: str):
                return self

            def execute(self):
                return SimpleNamespace(data=[])

        bucket = FakeBucket()
        table = FakeTable()
        client = SimpleNamespace(
            storage=SimpleNamespace(from_=lambda name: bucket),
            table=lambda name: table,
        )
        settings = SimpleNamespace(
            supabase_storage_bucket="wardrobe-images",
            background_removal_enabled=True,
        )

        with (
            patch("app.services.background_jobs.get_supabase_client", return_value=client),
            patch("app.services.background_jobs.get_settings", return_value=settings),
            patch(
                "app.services.background_jobs.put_item_on_white_background",
                return_value=b"isolated",
            ),
            patch(
                "app.services.background_jobs.optimize_item_image",
                return_value=b"optimized",
            ),
            patch(
                "app.services.background_jobs.create_item_thumbnail",
                return_value=b"thumbnail",
            ),
        ):
            BackgroundJobQueue()._process_image_pipeline(
                ImageTaggingJob(
                    "item-123",
                    "uid/incoming/source.jpg",
                    category="shirt",
                    skip_ai=True,
                )
            )

        self.assertEqual(
            bucket.uploads,
            ["processed/item-123.jpg", "thumbnails/item-123.jpg"],
        )
        self.assertIn("uid/incoming/source.jpg", bucket.removed)
        self.assertEqual(table.updates[-1]["ai_tag_status"], "completed")
        self.assertEqual(
            table.updates[-1]["thumbnail_path"], "thumbnails/item-123.jpg"
        )

    def test_automatic_tagging_persists_detected_brand_tags_and_name(self) -> None:
        class FakeTable:
            def __init__(self) -> None:
                self.updates: list[dict] = []

            def update(self, payload: dict):
                self.updates.append(payload)
                return self

            def eq(self, field: str, value: str):
                return self

            def execute(self):
                return SimpleNamespace(data=[])

        table = FakeTable()
        client = SimpleNamespace(table=lambda name: table)
        analysis = AiRequestJob(
            id="analysis-1",
            owner_uid="user-1",
            kind="single",
            image=b"",
            content_type="image/jpeg",
            image_hash="hash",
            state="completed",
            result={
                "brand": "Crocs",
                "category": "shoes",
                "color": "black",
                "season": "all",
                "formality": "casual",
                "description": "Black Crocs clogs for casual everyday wear.",
                "tags": ["clogs", "rubber", "Crocs"],
                "visual_tags": ["perforated", "slip-on"],
            },
        )

        with (
            patch(
                "app.services.background_jobs.get_supabase_client",
                return_value=client,
            ),
        ):
            BackgroundJobQueue()._finish_ai_tagging(
                ImageTaggingJob(
                    item_id="item-123",
                    image_path="processed/item-123.jpg",
                    generate_name=True,
                ),
                {"image_path": "processed/item-123.jpg"},
                analysis,
            )

        completed = table.updates[-1]
        self.assertEqual(completed["name"], "Crocs Black Shoes")
        self.assertEqual(completed["brand"], "Crocs")
        self.assertEqual(completed["tags"], ["clogs", "rubber", "Crocs"])
        self.assertEqual(completed["ai_tag_status"], "completed")


if __name__ == "__main__":
    unittest.main()
