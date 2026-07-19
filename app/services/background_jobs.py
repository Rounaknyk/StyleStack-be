import logging
from dataclasses import dataclass
from queue import Full, Queue
from threading import Lock, Thread
from typing import Final

from app.core.config import get_settings
from app.core.supabase import get_supabase_client
from app.models.ai_tags import ClothingTags
from app.services.ai_request_queue import AiRequestJob, ai_request_queue
from app.services.image_fingerprint import perceptual_hash
from app.services.image_processing import (
    create_item_thumbnail,
    optimize_item_image,
    put_item_on_transparent_background,
    put_item_on_white_background,
)

logger = logging.getLogger("stylestack.jobs")


@dataclass(frozen=True, slots=True)
class ImageTaggingJob:
    item_id: str
    image_path: str
    owner_uid: str = ""
    category: str | None = None
    skip_ai: bool = False
    generate_name: bool = False


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

    def retry_ai_tagging(self, job: ImageTaggingJob) -> None:
        """Retry only AI tagging for an already prepared wardrobe image."""
        client = get_supabase_client()
        settings = get_settings()
        bucket = client.storage.from_(settings.supabase_storage_bucket)
        downloaded = bucket.download(job.image_path)
        if isinstance(downloaded, bytes):
            image = downloaded
        elif hasattr(downloaded, "content"):
            image = downloaded.content
        else:
            raise RuntimeError("Supabase did not return image bytes")
        if not image:
            raise RuntimeError("Downloaded wardrobe image is empty")

        base_update = {"image_path": job.image_path}
        image_hash = perceptual_hash(image)
        suffix = job.image_path.rsplit(".", 1)[-1].casefold()
        content_type = {
            "png": "image/png",
            "webp": "image/webp",
        }.get(suffix, "image/jpeg")
        client.table("wardrobe_items").update(
            {"ai_tag_status": "pending", "tagged": False}
        ).eq("id", job.item_id).eq(
            "owner_firebase_uid", job.owner_uid
        ).execute()
        ai_request_queue.enqueue(
            owner_uid=job.owner_uid,
            kind="single",
            image=image,
            content_type=content_type,
            image_hash=image_hash,
            on_complete=lambda analysis_job: self._finish_ai_tagging(
                job,
                base_update,
                analysis_job,
            ),
        )
        logger.info("image_tagging_retry_queued item_id=%s", job.item_id)

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
                    self._process_image_pipeline(queued)
            except Exception:
                logger.exception("background_job_crashed")
            finally:
                self._queue.task_done()

    def _process_image_pipeline(self, job: ImageTaggingJob) -> None:
        # Kept as a plain console line for the Week 1 acceptance requirement.
        print(f"Processing item {job.item_id}", flush=True)
        logger.info("image_processing_started item_id=%s", job.item_id)
        client = get_supabase_client()
        settings = get_settings()
        bucket = client.storage.from_(settings.supabase_storage_bucket)
        processed_path = f"processed/{job.item_id}.jpg"
        thumbnail_path = f"thumbnails/{job.item_id}.jpg"
        cutout_path = f"cutouts/{job.item_id}.png"
        images_ready = False
        cutout_ready = False

        try:
            client.table("wardrobe_items").update(
                {"ai_tag_status": "processing", "tagged": False}
            ).eq("id", job.item_id).execute()

            downloaded = bucket.download(job.image_path)
            if isinstance(downloaded, bytes):
                original = downloaded
            elif hasattr(downloaded, "content"):
                original = downloaded.content
            else:
                raise RuntimeError("Supabase did not return image bytes")
            if not original:
                raise RuntimeError("Downloaded wardrobe image is empty")

            cutout = None
            if settings.background_removal_enabled:
                try:
                    cutout = put_item_on_transparent_background(original, job.category)
                    bucket.upload(
                        path=cutout_path,
                        file=cutout,
                        file_options={"content-type": "image/png", "upsert": "true"},
                    )
                    cutout_ready = True
                    logger.info("wardrobe_cutout_created item_id=%s bytes=%s", job.item_id, len(cutout))
                except Exception as exc:
                    logger.warning(
                        "wardrobe_cutout_failed item_id=%s error_type=%s",
                        job.item_id, type(exc).__name__,
                    )

            # The transparent cutout is also the best source for the white
            # display image, so avoid running the heavy model twice.
            prepared = cutout or original
            if settings.background_removal_enabled and cutout is None:
                try:
                    prepared = put_item_on_white_background(original, job.category)
                    logger.info("wardrobe_background_removed item_id=%s", job.item_id)
                except Exception as exc:
                    # A segmentation issue must not discard a successful upload.
                    logger.warning(
                        "wardrobe_background_removal_failed item_id=%s error_type=%s using_original=true",
                        job.item_id,
                        type(exc).__name__,
                    )

            optimized = optimize_item_image(prepared)
            thumbnail = create_item_thumbnail(optimized)
            upload_options = {"content-type": "image/jpeg", "upsert": "true"}
            bucket.upload(
                path=processed_path,
                file=optimized,
                file_options=upload_options,
            )
            bucket.upload(
                path=thumbnail_path,
                file=thumbnail,
                file_options=upload_options,
            )
            images_ready = True
            logger.info(
                "wardrobe_images_optimized item_id=%s full_bytes=%s thumbnail_bytes=%s",
                job.item_id,
                len(optimized),
                len(thumbnail),
            )

            base_update = {
                "image_path": processed_path,
                "thumbnail_path": thumbnail_path,
            }
            if cutout_ready:
                base_update["cutout_path"] = cutout_path
            if job.skip_ai:
                client.table("wardrobe_items").update(
                    {**base_update, "tagged": True, "ai_tag_status": "completed"}
                ).eq("id", job.item_id).execute()
                try:
                    bucket.remove([job.image_path])
                except Exception:
                    logger.warning("incoming_image_cleanup_failed path=%s", job.image_path)
                logger.info("image_processing_completed item_id=%s", job.item_id)
                if job.owner_uid:
                    from app.services.notifications import notification_scheduler

                    notification_scheduler.notify_wardrobe_item_ready(job.owner_uid)
                return

            tags = None
            image_hash = perceptual_hash(original)
            try:
                cached = (
                    client.table("ai_image_analysis_cache")
                    .select("analysis")
                    .eq("image_hash", image_hash)
                    .eq("analysis_kind", "single")
                    .limit(1)
                    .execute()
                )
                if cached.data:
                    tags = ClothingTags.model_validate(cached.data[0]["analysis"])
                    logger.info(
                        "background_tagging_cache_hit item_id=%s hash=%s",
                        job.item_id,
                        image_hash,
                    )
            except Exception:
                logger.warning(
                    "background_tagging_cache_lookup_failed item_id=%s",
                    job.item_id,
                )
            if tags is None:
                ai_request_queue.enqueue(
                    owner_uid=job.owner_uid,
                    kind="single",
                    image=optimized,
                    content_type="image/jpeg",
                    image_hash=image_hash,
                    on_complete=lambda analysis_job: self._finish_ai_tagging(
                        job,
                        base_update,
                        analysis_job,
                    ),
                )
                try:
                    bucket.remove([job.image_path])
                except Exception:
                    logger.warning("incoming_image_cleanup_failed path=%s", job.image_path)
                logger.info("image_processing_queued_for_ai item_id=%s", job.item_id)
                return

            completed_update = {
                **base_update,
                "tagged": True,
                "ai_tag_status": "completed",
                "ai_category": tags.category,
                "ai_color": tags.color,
                "ai_season": tags.season,
                "ai_formality": tags.formality,
                "ai_description": tags.description,
                "ai_visual_tags": tags.visual_tags,
            }
            if job.generate_name:
                completed_update["name"] = f"{tags.color} {tags.category}".title()
            client.table("wardrobe_items").update(completed_update).eq(
                "id", job.item_id
            ).execute()
            try:
                bucket.remove([job.image_path])
            except Exception:
                logger.warning("incoming_image_cleanup_failed path=%s", job.image_path)
            logger.info("image_processing_completed item_id=%s", job.item_id)
            if job.owner_uid:
                from app.services.notifications import notification_scheduler

                notification_scheduler.notify_wardrobe_item_ready(job.owner_uid)
        except Exception as exc:
            logger.error(
                "image_processing_failed item_id=%s error_type=%s",
                job.item_id,
                type(exc).__name__,
            )
            try:
                failure_update = {"tagged": False, "ai_tag_status": "failed"}
                if images_ready:
                    failure_update.update(
                        {
                            "image_path": processed_path,
                            "thumbnail_path": thumbnail_path,
                        }
                    )
                    if cutout_ready:
                        failure_update["cutout_path"] = cutout_path
                client.table("wardrobe_items").update(
                    failure_update
                ).eq("id", job.item_id).execute()
                if images_ready:
                    try:
                        bucket.remove([job.image_path])
                    except Exception:
                        logger.warning(
                            "incoming_image_cleanup_failed path=%s", job.image_path
                        )
            except Exception:
                logger.exception("image_tagging_failure_status_update_failed item_id=%s", job.item_id)

    def _finish_ai_tagging(
        self,
        job: ImageTaggingJob,
        base_update: dict[str, str],
        analysis_job: AiRequestJob,
    ) -> None:
        client = get_supabase_client()
        try:
            if analysis_job.state != "completed" or not analysis_job.result:
                raise RuntimeError(analysis_job.error or "AI analysis failed")
            tags = ClothingTags.model_validate(analysis_job.result)
            completed_update = {
                **base_update,
                "tagged": True,
                "ai_tag_status": "completed",
                "ai_category": tags.category,
                "ai_color": tags.color,
                "ai_season": tags.season,
                "ai_formality": tags.formality,
                "ai_description": tags.description,
                "ai_visual_tags": tags.visual_tags,
            }
            if job.generate_name:
                completed_update["name"] = f"{tags.color} {tags.category}".title()
            client.table("wardrobe_items").update(completed_update).eq(
                "id", job.item_id
            ).execute()
            logger.info("image_tagging_completed item_id=%s", job.item_id)
            if job.owner_uid:
                from app.services.notifications import notification_scheduler

                notification_scheduler.notify_wardrobe_item_ready(job.owner_uid)
        except Exception:
            logger.exception("image_tagging_completion_failed item_id=%s", job.item_id)
            client.table("wardrobe_items").update(
                {**base_update, "tagged": False, "ai_tag_status": "failed"}
            ).eq("id", job.item_id).execute()


background_jobs = BackgroundJobQueue()
