import time
import unittest
from threading import Event

from app.services.background_jobs import BackgroundJobQueue, ImageTaggingJob


class ControlledJobQueue(BackgroundJobQueue):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()

    def _process_image_tagging(self, job: ImageTaggingJob) -> None:
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


if __name__ == "__main__":
    unittest.main()
