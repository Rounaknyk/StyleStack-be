from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from threading import Condition, Thread
import time
from typing import Any, Callable, Literal
from uuid import uuid4

from app.core.config import get_settings
from app.core.supabase import get_supabase_client
from app.models.ai_tags import ClothingDetection, ClothingTags
from app.services.ai_tagging import analyze_clothing_bytes, analyze_multiple_clothing_bytes

logger = logging.getLogger("stylestack.ai_queue")

JobKind = Literal["single", "multiple"]
JobState = Literal["queued", "processing", "completed", "failed", "canceled"]


@dataclass(slots=True)
class AiRequestJob:
    id: str
    owner_uid: str
    kind: JobKind
    image: bytes
    content_type: str
    image_hash: str
    leader_job_id: str | None = None
    on_complete: Callable[["AiRequestJob"], None] | None = None
    state: JobState = "queued"
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None


class AiRequestQueue:
    """Fair, observable, process-local queue for wardrobe vision requests."""

    def __init__(self) -> None:
        self._condition = Condition()
        self._pending: list[str] = []
        self._jobs: dict[str, AiRequestJob] = {}
        self._user_started: dict[str, deque[float]] = {}
        self._thread: Thread | None = None
        self._stopping = False

    def start(self) -> None:
        with self._condition:
            if self._thread and self._thread.is_alive():
                return
            self._stopping = False
            self._thread = Thread(target=self._run, name="stylestack-groq-queue", daemon=True)
            self._thread.start()
        logger.info("ai_request_queue_started")

    def stop(self) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
            thread = self._thread
        if thread:
            thread.join(timeout=5)
        logger.info("ai_request_queue_stopped")

    def enqueue(
        self,
        *,
        owner_uid: str,
        kind: JobKind,
        image: bytes,
        content_type: str,
        image_hash: str,
        on_complete: Callable[[AiRequestJob], None] | None = None,
    ) -> AiRequestJob:
        settings = get_settings()
        with self._condition:
            cutoff = time.time() - settings.ai_request_job_retention_seconds
            expired = [
                job_id
                for job_id, existing in self._jobs.items()
                if existing.state in ("completed", "failed", "canceled")
                and (existing.completed_at or existing.created_at) < cutoff
            ]
            for job_id in expired:
                self._jobs.pop(job_id, None)
            active = sum(
                job.state in ("queued", "processing") for job in self._jobs.values()
            )
            if active >= settings.ai_request_queue_max_size:
                raise RuntimeError("AI request queue is full")
            job = AiRequestJob(
                id=uuid4().hex,
                owner_uid=owner_uid,
                kind=kind,
                image=image,
                content_type=content_type,
                image_hash=image_hash,
                on_complete=on_complete,
            )
            duplicate_leader = next(
                (
                    existing
                    for existing in self._jobs.values()
                    if existing.image_hash == image_hash
                    and existing.kind == kind
                    and existing.leader_job_id is None
                    and existing.state in ("queued", "processing")
                ),
                None,
            )
            if duplicate_leader is not None:
                job.leader_job_id = duplicate_leader.id
                job.state = duplicate_leader.state
                self._jobs[job.id] = job
                logger.info(
                    "ai_request_coalesced job_id=%s leader_job_id=%s uid=%s",
                    job.id,
                    duplicate_leader.id,
                    owner_uid,
                )
                return job
            self._jobs[job.id] = job
            self._pending.append(job.id)
            self._condition.notify_all()
            logger.info(
                "ai_request_queued job_id=%s uid=%s kind=%s queue_size=%s",
                job.id,
                owner_uid,
                kind,
                len(self._pending),
            )
            return job

    def completed_from_cache(
        self,
        *,
        owner_uid: str,
        kind: JobKind,
        image_hash: str,
        result: dict[str, Any],
    ) -> AiRequestJob:
        job = AiRequestJob(
            id=uuid4().hex,
            owner_uid=owner_uid,
            kind=kind,
            image=b"",
            content_type="",
            image_hash=image_hash,
            state="completed",
            result=result,
            completed_at=time.time(),
        )
        with self._condition:
            self._jobs[job.id] = job
        logger.info("ai_analysis_cache_hit job_id=%s hash=%s kind=%s", job.id, image_hash, kind)
        return job

    def get(self, job_id: str, owner_uid: str) -> AiRequestJob | None:
        with self._condition:
            job = self._jobs.get(job_id)
            return job if job and job.owner_uid == owner_uid else None

    def cancel(self, job_id: str, owner_uid: str) -> AiRequestJob | None:
        with self._condition:
            job = self._jobs.get(job_id)
            if not job or job.owner_uid != owner_uid:
                return None
            if job.state == "queued":
                job.state = "canceled"
                job.completed_at = time.time()
                if job.leader_job_id is None and job_id in self._pending:
                    followers = [
                        candidate
                        for candidate in self._jobs.values()
                        if candidate.leader_job_id == job.id
                        and candidate.state == "queued"
                    ]
                    position = self._pending.index(job_id)
                    if followers:
                        promoted = followers[0]
                        promoted.leader_job_id = None
                        self._pending[position] = promoted.id
                        for follower in followers[1:]:
                            follower.leader_job_id = promoted.id
                    else:
                        self._pending.remove(job_id)
                job.image = b""
                self._condition.notify_all()
            return job

    def retry(self, job_id: str, owner_uid: str) -> AiRequestJob | None:
        with self._condition:
            job = self._jobs.get(job_id)
            if not job or job.owner_uid != owner_uid:
                return None
            if job.state == "failed" and job.image:
                job.state = "queued"
                job.leader_job_id = None
                job.error = None
                job.started_at = None
                job.completed_at = None
                self._pending.append(job.id)
                self._condition.notify_all()
            return job

    def snapshot(self, job: AiRequestJob) -> dict[str, Any]:
        with self._condition:
            queue_job = (
                self._jobs.get(job.leader_job_id)
                if job.leader_job_id is not None
                else job
            )
            position = None
            if (
                queue_job is not None
                and job.state == "queued"
                and queue_job.id in self._pending
            ):
                position = self._pending.index(queue_job.id) + 1
            seconds_per_request = 60 / max(1, get_settings().groq_requests_per_minute)
            eta_seconds = (position or 0) * seconds_per_request
            recent = self._user_started.get(job.owner_uid, deque())
            limit = max(1, get_settings().ai_requests_per_user_per_minute)
            same_user_ahead = 0
            if position:
                same_user_ahead = sum(
                    self._jobs[pending_id].owner_uid == job.owner_uid
                    for pending_id in self._pending[: position - 1]
                )
                eta_seconds = max(
                    eta_seconds,
                    (same_user_ahead // limit) * 60 + seconds_per_request,
                )
            now = time.monotonic()
            active_recent = [stamp for stamp in recent if now - stamp < 60]
            if job.state == "queued" and len(active_recent) >= limit:
                eta_seconds = max(eta_seconds, 60 - (now - active_recent[0]))
            eta = int(max(0, round(eta_seconds)))
            return {
                "job_id": job.id,
                "status": job.state,
                "kind": job.kind,
                "queue_position": position,
                "items_ahead": max(0, position - 1) if position else 0,
                "estimated_wait_seconds": eta,
                "result": job.result,
                "error": job.error,
                "created_at": datetime.fromtimestamp(
                    job.created_at, timezone.utc
                ).isoformat(),
            }

    def _eligible_index(self) -> tuple[int | None, float]:
        now = time.monotonic()
        limit = max(1, get_settings().ai_requests_per_user_per_minute)
        earliest_wait = 60.0
        for index, job_id in enumerate(self._pending):
            job = self._jobs[job_id]
            recent = self._user_started.setdefault(job.owner_uid, deque())
            while recent and now - recent[0] >= 60:
                recent.popleft()
            if len(recent) < limit:
                return index, 0.0
            earliest_wait = min(earliest_wait, 60 - (now - recent[0]))
        return None, max(0.05, earliest_wait)

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._pending and not self._stopping:
                    self._condition.wait()
                if self._stopping:
                    return
                index, wait = self._eligible_index()
                if index is None:
                    self._condition.wait(timeout=wait)
                    continue
                job_id = self._pending.pop(index)
                job = self._jobs[job_id]
                if job.state != "queued":
                    continue
                job.state = "processing"
                job.started_at = time.time()
                followers = [
                    candidate
                    for candidate in self._jobs.values()
                    if candidate.leader_job_id == job.id
                    and candidate.state == "queued"
                ]
                for follower in followers:
                    follower.state = "processing"
                    follower.started_at = job.started_at
                self._user_started.setdefault(job.owner_uid, deque()).append(time.monotonic())

            try:
                if job.kind == "multiple":
                    result = analyze_multiple_clothing_bytes(
                        job.image, job.content_type, owner_uid=job.owner_uid
                    )
                else:
                    result = analyze_clothing_bytes(
                        job.image, job.content_type, owner_uid=job.owner_uid
                    )
                job.result = result.model_dump(mode="json")
                job.state = "completed"
                self._store_cache(job)
                logger.info("ai_request_completed job_id=%s uid=%s", job.id, job.owner_uid)
            except Exception as exc:
                job.state = "failed"
                job.error = "AI analysis failed. Tap retry to try again."
                logger.error(
                    "ai_request_failed job_id=%s uid=%s error_type=%s",
                    job.id,
                    job.owner_uid,
                    type(exc).__name__,
                )
            finally:
                if job.state == "completed":
                    job.image = b""
                job.completed_at = time.time()
                with self._condition:
                    followers = [
                        candidate
                        for candidate in self._jobs.values()
                        if candidate.leader_job_id == job.id
                        and candidate.state != "canceled"
                    ]
                    for follower in followers:
                        follower.state = job.state
                        follower.result = job.result
                        follower.error = job.error
                        follower.completed_at = job.completed_at
                        follower.image = b"" if job.state == "completed" else follower.image
                    self._condition.notify_all()
                for completed_job in [job, *followers]:
                    if completed_job.on_complete is None:
                        continue
                    try:
                        completed_job.on_complete(completed_job)
                    except Exception:
                        logger.exception(
                            "ai_request_completion_callback_failed job_id=%s",
                            completed_job.id,
                        )

    def wait(self, job: AiRequestJob, timeout: float = 600) -> AiRequestJob:
        deadline = time.monotonic() + timeout
        with self._condition:
            while job.state in ("queued", "processing"):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("AI analysis queue timed out")
                self._condition.wait(timeout=min(remaining, 1.0))
        return job

    def _store_cache(self, job: AiRequestJob) -> None:
        try:
            get_supabase_client().table("ai_image_analysis_cache").upsert(
                {
                    "image_hash": job.image_hash,
                    "analysis_kind": job.kind,
                    "analysis": job.result,
                    "last_used_at": datetime.now(timezone.utc).isoformat(),
                },
                on_conflict="image_hash,analysis_kind",
            ).execute()
        except Exception:
            logger.exception("ai_analysis_cache_store_failed hash=%s", job.image_hash)


ai_request_queue = AiRequestQueue()
