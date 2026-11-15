"""Durable async batch ingestion.

Design principles:
    - Job state is a disk fact, not memory state: one self-contained
      directory per job; after a restart, pending/running jobs are
      recovered and re-run.
    - A small per-job worker pool (default 2, PROVENDOC_BATCH_WORKERS
      env var or create_app(workers=...)): jobs run in parallel, a
      job's files run in order -- deterministic, observable,
      recoverable; throughput scales at job granularity. Claiming
      happens synchronously via claim_next() (no await) inside the
      event loop, so concurrent workers claim each job exactly once.
    - The honest-failure contract extends to jobs: a file-level
      parse_failed is a document fact (the file is completed; the
      fact lives in the result envelope); parameter-validation and
      unexpected errors are file-level failures. All files failed ->
      job failed, otherwise completed.
    - Cancellation is cooperative: DELETE drops a cancel.requested
      marker file; workers check it before each file; completed
      file results are kept.

Layout:
    jobs/                                (jobs_root, create_app(jobs_dir=...))
        {job_id}/
            job.json                     # state machine + file facts (atomic write)
            uploads/{idx:04d}_{slug}    # persisted upload bytes (crash-safe rerun base)
            results/{file_id}.json      # per-file result envelope (incremental)

State machines:
    job:  pending -> running -> completed | failed | cancelled
    file: pending -> running -> completed | failed | skipped

Recovery: at startup (lifespan, before any worker claim) pending/
running jobs are reset to pending and re-enqueued; running files
rerun from the persisted upload bytes (idempotent: result files
are overwritten).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

from provendoc.api.serialization import result_to_dict

# Log hygiene: structured escaping of user filenames / exception
# text before they enter log records.
from provendoc.core.pipeline.log_hygiene import sanitize_log_value

_log = logging.getLogger(__name__)

# SDK methods selectable at submit time (one-to-one with the
# single-file endpoints)
BATCH_METHODS = ("parse", "chunk", "evaluate", "auto_pipeline")

JOB_STATUSES = ("pending", "running", "completed", "failed", "cancelled")

SCHEMA_VERSION = "1.0"

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(name: str) -> str:
    """Upload filename -> safe stored name (suffix kept for domain routing)."""
    base = _SAFE_NAME.sub("_", Path(name).name).strip("._") or "upload"
    return base[-80:]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    """Persistent job storage under jobs_root.

    All writes are atomic (tmp + os.replace): a crash at any moment
    leaves job.json either fully old or fully new, never half-written.
    """

    def __init__(self, root: Path):
        """Initialize storage rooted at root (dirs are created lazily on first write)."""
        self.root = Path(root)

    # ------------------------------------------------------
    # layout helpers
    # ------------------------------------------------------

    def job_dir(self, job_id: str) -> Path:
        """Self-contained job directory (job.json + uploads/ + results/)."""
        return self.root / job_id

    def job_json(self, job_id: str) -> Path:
        """Job state file path (the single source of truth)."""
        return self.job_dir(job_id) / "job.json"

    def upload_path(self, job_id: str, stored: str) -> Path:
        """Path of a staged upload (stored is the sanitized safe name)."""
        return self.job_dir(job_id) / stored

    def result_path(self, job_id: str, file_id: str) -> Path:
        """Path of a completed file's result envelope."""
        return self.job_dir(job_id) / "results" / f"{file_id}.json"

    def cancel_marker(self, job_id: str) -> Path:
        """Cooperative-cancel marker path (existence means cancel requested)."""
        return self.job_dir(job_id) / "cancel.requested"

    # ------------------------------------------------------
    # job lifecycle
    # ------------------------------------------------------

    def _new_job_id(self) -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        while True:
            job_id = f"{stamp}_{hashlib.sha256(os.urandom(8)).hexdigest()[:6]}"
            if not self.job_json(job_id).exists():
                return job_id

    def create(
        self,
        *,
        method: str,
        params: dict,
        include_text: bool,
        files: list[tuple[str, bytes]],
    ) -> dict:
        """Persist upload bytes + the initial job.json -> a pending job."""
        job_id = self._new_job_id()
        job_dir = self.job_dir(job_id)
        (job_dir / "uploads").mkdir(parents=True)
        (job_dir / "results").mkdir()

        records = []
        for idx, (name, data) in enumerate(files):
            stored = f"uploads/{idx:04d}_{_slug(name)}"
            self.upload_path(job_id, stored).write_bytes(data)
            records.append({
                "id": f"f{idx:04d}",
                "name": name,
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "stored": stored,
                "status": "pending",
                "result": None,
                "error": None,
            })

        job = {
            "schema_version": SCHEMA_VERSION,
            "job_id": job_id,
            "created_at": _utcnow(),
            "updated_at": _utcnow(),
            "status": "pending",
            "method": method,
            "params": params,
            "include_text": include_text,
            "files": records,
        }
        self.save(job)
        return job

    def save(self, job: dict) -> None:
        """Atomically write job.json (tmp + os.replace); refreshes updated_at."""
        job["updated_at"] = _utcnow()
        path = self.job_json(job["job_id"])
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        os.replace(tmp, path)

    def get(self, job_id: str) -> dict | None:
        """Read one job; returns None if missing or job.json is unreadable."""
        path = self.job_json(job_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def list_jobs(self) -> list[dict]:
        """List all jobs (created_at ascending); corrupt job.json entries are skipped."""
        if not self.root.is_dir():
            return []
        jobs = []
        for path in sorted(self.root.glob("*/job.json")):
            try:
                jobs.append(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
        jobs.sort(key=lambda j: j.get("created_at", ""))
        return jobs

    def claim_next(self) -> dict | None:
        """Claim the next pending job (pending -> running).

        Synchronous, no await -- atomic within the single event loop:
        concurrent workers claim each job exactly once. Returns None
        when the queue is empty.
        """
        for job in self.list_jobs():
            if job.get("status") == "pending":
                job["status"] = "running"
                self.save(job)
                _log.info(
                    "Job %s claimed (method=%s, files=%d)",
                    job["job_id"], job.get("method"), len(job.get("files", [])),
                )
                return job
        return None

    # ------------------------------------------------------
    # results / cancel
    # ------------------------------------------------------

    def write_result(self, job_id: str, file_id: str, envelope: dict) -> str:
        """Atomically write one file's result envelope; returns the job-relative POSIX path."""
        path = self.result_path(job_id, file_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(envelope, ensure_ascii=False), encoding="utf-8",
        )
        os.replace(tmp, path)
        return str(path.relative_to(self.job_dir(job_id))).replace("\\", "/")

    def read_result(self, job_id: str, file_id: str) -> dict | None:
        """Read one file's result envelope; None if missing or corrupt."""
        path = self.result_path(job_id, file_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def request_cancel(self, job_id: str) -> bool:
        """Drop the cancel marker (cooperative cancel). No-op for terminal jobs."""
        job = self.get(job_id)
        if job is None:
            return False
        if job.get("status") in ("completed", "failed", "cancelled"):
            return False
        self.cancel_marker(job_id).write_text(
            _utcnow(), encoding="utf-8",
        )
        return True

    def is_cancelled(self, job_id: str) -> bool:
        """Whether the cancel marker exists for this job."""
        return self.cancel_marker(job_id).is_file()

    def recover(self) -> int:
        """Crash recovery: reset pending/running jobs to pending.

        Running files fall back to pending (upload bytes are already
        persisted and reruns are idempotent -- result files are
        overwritten). Returns the number of reset jobs.
        """
        reset = 0
        for job in self.list_jobs():
            if job.get("status") in ("pending", "running"):
                for f in job.get("files", []):
                    if f["status"] in ("pending", "running"):
                        f["status"] = "pending"
                job["status"] = "pending"
                self.save(job)
                reset += 1
        if reset:
            _log.info("Batch recovery: %d job(s) reset to pending", reset)
        return reset


class BatchWorker:
    """Background worker: loops claiming and processing jobs,
    persisting per-file results.

    Multiple instances form the worker pool (each is an independent
    asyncio task): claim_next() is synchronously atomic, so tasks
    never conflict. Threading model: SDK calls (synchronous,
    CPU-bound) run via asyncio.to_thread so the event loop stays
    responsive (the API keeps serving).
    """

    def __init__(self, store: JobStore, sdk, poll_seconds: float = 0.2):
        """Bind the job store and SDK; poll_seconds is the idle poll interval
        (the wake event can fire earlier)."""
        self._store = store
        self._sdk = sdk
        self._poll = poll_seconds
        self._wake = asyncio.Event()
        self._running = False

    # ------------------------------------------------------
    # control
    # ------------------------------------------------------

    def wake(self) -> None:
        """Wake an idle worker so it polls immediately (called after a new job is submitted)."""
        self._wake.set()

    def stop(self) -> None:
        """Request the worker to exit its main loop (cooperative; effective after the current job)."""
        self._running = False
        self._wake.set()

    async def run_forever(self) -> None:
        """Main loop: claim jobs and process file by file; when idle, wait
        on the wake event or the poll timeout."""
        # Crash recovery runs once in lifespan (per-worker recover
        # would reset claimed running jobs back to pending and
        # double-process them)
        self._running = True
        while self._running:
            job = self._store.claim_next()
            if job is None:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), self._poll)
                self._wake.clear()
                continue
            await self._process_job(job)

    # ------------------------------------------------------
    # job / file processing
    # ------------------------------------------------------

    async def _process_job(self, job: dict) -> None:
        job_id = job["job_id"]
        if self._store.is_cancelled(job_id):
            self._finish_files(job, "skipped")
            job["status"] = "cancelled"
            self._store.save(job)
            _log.info("Job %s cancelled before processing", job_id)
            return

        # the job was already marked running and saved by claim_next()
        for f in job["files"]:
            if not self._running:
                return  # service shutting down: job stays running; next startup recovers it
            if self._store.is_cancelled(job_id):
                self._finish_files(job, "skipped")
                job["status"] = "cancelled"
                self._store.save(job)
                _log.info("Job %s cancelled mid-processing", job_id)
                return
            await self._process_file(job, f)

        statuses = [f["status"] for f in job["files"]]
        job["status"] = "failed" if statuses and \
            all(s == "failed" for s in statuses) else "completed"
        self._store.save(job)
        counts: dict[str, int] = {}
        for s in statuses:
            counts[s] = counts.get(s, 0) + 1
        _log.info("Job %s %s (files: %s)", job_id, job["status"], counts)

    async def _process_file(self, job: dict, f: dict) -> None:
        job_id = job["job_id"]
        f["status"] = "running"
        self._store.save(job)

        upload = self._store.upload_path(job_id, f["stored"])
        started = time.perf_counter()
        try:
            result = await asyncio.to_thread(
                self._call_sdk, str(upload), job["method"], job["params"],
            )
            envelope = result_to_dict(
                result,
                file_name=f["name"],
                file_size=f["size_bytes"],
                include_text=job["include_text"],
            )
            envelope["processing_ms"] = round(
                (time.perf_counter() - started) * 1000, 3,
            )
            f["result"] = self._store.write_result(job_id, f["id"], envelope)
            f["status"] = "completed"
        except Exception as e:  # noqa: BLE001 -- record the fact, keep the job going
            f["status"] = "failed"
            f["error"] = f"{type(e).__name__}: {e}"
            _log.warning(
                "Job %s file %r failed: %s",
                job_id,
                sanitize_log_value(f["name"]),
                sanitize_log_value(f["error"]),
            )
        self._store.save(job)

    def _call_sdk(self, path: str, method: str, params: dict):
        fn = getattr(self._sdk, method)
        return fn(path, **dict(params))

    @staticmethod
    def _finish_files(job: dict, status: str) -> None:
        for f in job["files"]:
            if f["status"] in ("pending", "running"):
                f["status"] = status
