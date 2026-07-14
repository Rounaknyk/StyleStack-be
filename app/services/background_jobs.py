import logging
from dataclasses import dataclass
from queue import Full, Queue
from threading import Lock, Thread
import time
from typing import Final

import httpx

from app.core.config import get_settings
from app.core.supabase import get_supabase_client
from app.services.ai_tagging import analyze_clothing_image

logger = logging.getLogger("stylestack.jobs")


@dataclass(frozen=True, slots=True)
class ImageTaggingJob:
    item_id: str
    image_path: str


_STOP: Final = object()


class BackgroundJobQueue:
    """Single-process Week 1 job queue backed by one daemon worker thread."""

    def __init__(self, max_size: int = 1000) -> None:
        self._queue: Queue[ImageTaggingJob | object] = Queue(maxsize=max_size)
        self._thread: Thread | None = None
        self._lock = Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = Thread(
                target=self._run,
                name="stylestack-ai-worker",
                daemon=True,
            )
            self._thread.start()
            logger.info("background_worker_started")

    def enqueue(self, job: ImageTaggingJob) -> bool:
        """Enqueue immediately; never wait for queue capacity."""
        try:
            self._queue.put_nowait(job)
            logger.info("background_job_enqueued item_id=%s", job.item_id)
            return True
        except Full:
            logger.error("background_queue_full item_id=%s", job.item_id)
            return False

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            if not thread:
                return
            try:
                self._queue.put(_STOP, timeout=1)
            except Full:
                logger.warning("background_worker_stop_queue_full")
            thread.join(timeout=5)
            self._thread = None
            logger.info("background_worker_stopped")

    def _run(self) -> None:
        while True:
            queued = self._queue.get()
            try:
                if queued is _STOP:
                    return
                if isinstance(queued, ImageTaggingJob):
                    self._process_image_tagging(queued)
            except Exception:
                logger.exception("background_job_crashed")
            finally:
                self._queue.task_done()

    def _process_image_tagging(self, job: ImageTaggingJob) -> None:
        # Kept as a plain console line for the Week 1 acceptance requirement.
        print(f"Processing item {job.item_id}", flush=True)
        logger.info("image_tagging_started item_id=%s", job.item_id)
        client = get_supabase_client()

        try:
            client.table("wardrobe_items").update(
                {"ai_tag_status": "processing", "tagged": False}
            ).eq("id", job.item_id).execute()

            signed = client.storage.from_(
                get_settings().supabase_storage_bucket
            ).create_signed_url(job.image_path, 300)
            image_url = None
            if isinstance(signed, dict):
                image_url = signed.get("signedURL") or signed.get("signedUrl")
            if not image_url:
                raise RuntimeError("Supabase did not return a signed image URL")

            tags = None
            last_error: Exception | None = None
            for attempt in range(1, 4):
                try:
                    tags = analyze_clothing_image(image_url)
                    break
                except Exception as exc:
                    last_error = exc
                    status_code = (
                        exc.response.status_code
                        if isinstance(exc, httpx.HTTPStatusError)
                        else None
                    )
                    logger.warning(
                        "image_tagging_attempt_failed item_id=%s attempt=%s error_type=%s status=%s",
                        job.item_id,
                        attempt,
                        type(exc).__name__,
                        status_code,
                    )
                    if attempt < 3:
                        time.sleep(2 ** (attempt - 1))

            if tags is None:
                raise RuntimeError("AI tagging failed after 3 attempts") from last_error

            client.table("wardrobe_items").update(
                {
                    "tagged": True,
                    "ai_tag_status": "completed",
                    "ai_category": tags.category,
                    "ai_color": tags.color,
                    "ai_season": tags.season,
                    "ai_formality": tags.formality,
                    "ai_description": tags.description,
                    "ai_visual_tags": tags.visual_tags,
                }
            ).eq("id", job.item_id).execute()
            logger.info("image_tagging_completed item_id=%s", job.item_id)
        except Exception as exc:
            logger.error(
                "image_tagging_failed item_id=%s error_type=%s",
                job.item_id,
                type(exc).__name__,
            )
            try:
                client.table("wardrobe_items").update(
                    {"tagged": False, "ai_tag_status": "failed"}
                ).eq("id", job.item_id).execute()
            except Exception:
                logger.exception("image_tagging_failure_status_update_failed item_id=%s", job.item_id)


background_jobs = BackgroundJobQueue()
