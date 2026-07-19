"""Process-wide background queue for complete Gmail closet imports.

The queue deliberately keeps short-lived Google access tokens in memory only.
It serializes imports so a user can leave the initiating screen while the
backend scans every eligible delivered Amazon email.
"""

from dataclasses import dataclass
import logging
from queue import Full, Queue
from threading import Lock, Thread
from typing import Literal
from uuid import uuid4

from app.core.supabase import get_supabase_client
from app.services.gmail_import import import_gmail_orders

logger = logging.getLogger("stylestack.gmail_import_jobs")
GmailJobStatus = Literal["queued", "processing", "completed", "failed"]


@dataclass(slots=True)
class GmailImportJob:
    job_id: str
    owner_uid: str
    access_token: str
    status: GmailJobStatus = "queued"
    scanned_messages: int = 0
    imported_items: int = 0
    skipped_items: int = 0
    error: str | None = None

    def public_snapshot(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "scanned_messages": self.scanned_messages,
            "imported_items": self.imported_items,
            "skipped_items": self.skipped_items,
            "error": self.error,
        }


class GmailImportJobQueue:
    """A single-worker MVP queue. Jobs survive navigation, not process restarts."""

    def __init__(self, max_size: int = 100) -> None:
        self._queue: Queue[str] = Queue(maxsize=max_size)
        self._jobs: dict[str, GmailImportJob] = {}
        self._active_by_user: dict[str, str] = {}
        self._lock = Lock()
        self._thread: Thread | None = None

    def enqueue(self, owner_uid: str, access_token: str) -> dict[str, object]:
        self._start()
        with self._lock:
            active_id = self._active_by_user.get(owner_uid)
            if active_id:
                active = self._jobs.get(active_id)
                if active and active.status in {"queued", "processing"}:
                    return active.public_snapshot()

            job = GmailImportJob(
                job_id=str(uuid4()),
                owner_uid=owner_uid,
                access_token=access_token,
            )
            self._jobs[job.job_id] = job
            self._active_by_user[owner_uid] = job.job_id
            try:
                self._queue.put_nowait(job.job_id)
            except Full:
                self._jobs.pop(job.job_id, None)
                self._active_by_user.pop(owner_uid, None)
                raise RuntimeError("Gmail import queue is full")
            logger.info(
                "gmail_import_job_queued uid=%s job_id=%s",
                owner_uid,
                job.job_id,
            )
            return job.public_snapshot()

    def get(self, job_id: str, owner_uid: str) -> dict[str, object] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.owner_uid != owner_uid:
                return None
            return job.public_snapshot()

    def _start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = Thread(
                target=self._run,
                name="stylestack-gmail-import-worker",
                daemon=True,
            )
            self._thread.start()
            logger.info("gmail_import_worker_started")

    def _run(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                self._process(job_id)
            except Exception:
                logger.exception("gmail_import_job_worker_crashed job_id=%s", job_id)
            finally:
                self._queue.task_done()

    def _process(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.status = "processing"
            owner_uid = job.owner_uid
            access_token = job.access_token

        def update_progress(scanned: int, imported: int, skipped: int) -> None:
            with self._lock:
                current = self._jobs.get(job_id)
                if not current:
                    return
                current.scanned_messages = scanned
                current.imported_items = imported
                current.skipped_items = skipped

        try:
            scanned, imported, skipped = import_gmail_orders(
                get_supabase_client(),
                owner_uid,
                access_token,
                limit=None,
                on_progress=update_progress,
            )
            with self._lock:
                job = self._jobs[job_id]
                job.scanned_messages = scanned
                job.imported_items = imported
                job.skipped_items = skipped
                job.status = "completed"
                job.access_token = ""
                self._active_by_user.pop(owner_uid, None)
            logger.info(
                "gmail_import_job_completed uid=%s job_id=%s scanned=%s imported=%s skipped=%s",
                owner_uid,
                job_id,
                scanned,
                imported,
                skipped,
            )
        except Exception as exc:
            with self._lock:
                job = self._jobs[job_id]
                job.status = "failed"
                job.error = "Could not finish Gmail sync. Reconnect and try again."
                job.access_token = ""
                self._active_by_user.pop(owner_uid, None)
            logger.error(
                "gmail_import_job_failed uid=%s job_id=%s error_type=%s",
                owner_uid,
                job_id,
                type(exc).__name__,
            )


gmail_import_jobs = GmailImportJobQueue()
