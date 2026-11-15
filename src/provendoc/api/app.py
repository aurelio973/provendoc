"""FastAPI application factory -- HTTP mapping of the four SDK interfaces.

    GET  /health
    GET  /api/v1/domains
    POST /api/v1/parse
    POST /api/v1/chunk
    POST /api/v1/evaluate
    POST /api/v1/auto-pipeline
    POST /api/v1/batch                async batch submit (202 + job_id)
    GET  /api/v1/batch                job list
    GET  /api/v1/batch/{job_id}      job status (per-file progress)
    GET  /api/v1/batch/{job_id}/results  job results
    DELETE /api/v1/batch/{job_id}    cooperative cancel

Tuning frontend (static hosting, zero build, zero deps):
    GET  /                            -> 302 /ui/
    GET  /ui/                          index.html (tuning workbench)

Upload contract: multipart file field name ``file`` (``files`` for
batch). Files land in a temp directory first (the original suffix is
kept -- domain routing depends on it) and are deleted after parsing.

Error contract:
    - 422: no file carried
    - 400: parameter validation failure (ValueError from ParameterSpace)
    - 200 + parse_failed: parse failures (corrupt files etc.) are
      document facts, not transport errors (honest-failure principle)

Batch jobs persist under jobs_root (default ./jobs; the
PROVENDOC_JOBS_DIR env var or create_app(jobs_dir=...) overrides);
after a restart, pending/running jobs are automatically recovered
and re-run. Jobs are handled by a small worker pool
(PROVENDOC_BATCH_WORKERS or create_app(workers=...), default 2):
parallel across jobs, sequential per job's files. The job list
supports status/method filters plus limit/offset paging (the
response carries total).
"""

import asyncio
import hmac
import os
import re
import tempfile
from contextlib import asynccontextmanager, suppress
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

try:
    from fastapi import (
        FastAPI,
        Form,
        HTTPException,
        Query,
        Request,
        UploadFile,
    )
    from fastapi.responses import JSONResponse, RedirectResponse
    from fastapi.staticfiles import StaticFiles
except ImportError as exc:  # pragma: no cover - environment guard
    raise ImportError(
        "REST API requires the optional 'api' extras: "
        "pip install 'provendoc[api]'"
    ) from exc

import json as _json

from provendoc.api.batch import (
    BATCH_METHODS,
    JOB_STATUSES,
    BatchWorker,
    JobStore,
)
from provendoc.api.serialization import result_to_dict
from provendoc.core.domain.domain_registry import get_all_domains
from provendoc.sdk.client import Provendoc


def _parse_params(params_json: str) -> dict:
    """Form field ``params`` (a JSON string) -> dict.

    A multipart upload cannot carry a standalone JSON body, so the
    semantic parameters ride as a JSON string in a form field
    (engineering convention). Invalid JSON -> 400.
    """
    if not params_json:
        return {}
    try:
        parsed = _json.loads(params_json)
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=f"invalid params JSON: {e}",
        ) from e
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=400,
            detail="params must be a JSON object",
        )
    return parsed


def _package_version() -> str:
    try:
        return version("provendoc")
    except PackageNotFoundError:
        return "0.0.0+unknown"


# Single SDK instance -- state initialized at construction, reused
# across requests (thread-safe: the four interfaces are pure call
# chains with no shared mutable state).
_sdk = Provendoc()

# Tuning frontend static assets (index.html / style.css / app.js).
_WEB_DIR = Path(__file__).resolve().parent / "web"


async def _run(method: str, upload: UploadFile, include_text: bool,
               **kwargs):
    """Stage to disk -> SDK call -> response envelope (shared upload path)."""
    if not upload.filename:
        raise HTTPException(status_code=422, detail="missing file name")

    suffix = Path(upload.filename).suffix.lower()
    size = 0
    with tempfile.TemporaryDirectory(prefix="provendoc-api-") as tmp:
        staged = Path(tmp) / f"upload{suffix}"
        with staged.open("wb") as fh:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                fh.write(chunk)
        if size == 0:
            raise HTTPException(status_code=422, detail="empty upload")

        try:
            fn = getattr(_sdk, method)
            result = fn(str(staged), **kwargs)
        except ValueError as e:
            # ParameterSpace validation failure -> a user-input error
            raise HTTPException(status_code=400, detail=str(e)) from e

    return result_to_dict(
        result,
        file_name=upload.filename,
        file_size=size,
        include_text=include_text,
    )


def _default_jobs_dir() -> Path:
    return Path(os.environ.get("PROVENDOC_JOBS_DIR", "jobs"))


def _default_worker_count() -> int:
    raw = os.environ.get("PROVENDOC_BATCH_WORKERS", "2")
    try:
        count = int(raw)
    except ValueError:
        return 2
    return max(1, count)


def _env_api_key() -> str | None:
    """Optional API key from the PROVENDOC_API_KEY env var.

    Authentication is opt-in: an unset/empty variable keeps the
    historical zero-config local behavior; a set value means every
    ``/api/*`` request must carry a matching ``X-API-Key`` header
    (401 otherwise). ``/health`` and the ``/ui/`` static console stay
    open (the console's own API calls still 401).
    """
    raw = os.environ.get("PROVENDOC_API_KEY", "")
    return raw or None


def _job_summary(job: dict) -> dict:
    files = job.get("files", [])
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "method": job["method"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "counts": {
            "total": len(files),
            "completed": sum(f["status"] == "completed" for f in files),
            "failed": sum(f["status"] == "failed" for f in files),
            "pending": sum(f["status"] in ("pending", "running") for f in files),
            "skipped": sum(f["status"] == "skipped" for f in files),
        },
    }


def create_app(
    jobs_dir: str | Path | None = None,
    workers: int | None = None,
    api_key: str | None = None,
) -> FastAPI:
    """Build the Provendoc REST API application.

    jobs_dir: batch-job persistence root (default PROVENDOC_JOBS_DIR
    env var or ./jobs). On restart, workers recover pending jobs
    from disk.
    workers: batch worker-pool size (default PROVENDOC_BATCH_WORKERS
    env var or 2). Jobs run in parallel; a job's files run in order.
    api_key: opt-in request authentication. ``None`` (default) reads
    the PROVENDOC_API_KEY env var; an empty string explicitly
    disables auth. When a key is configured, ``/api/*`` requests must
    carry the ``X-API-Key`` header (constant-time comparison, 401
    otherwise); ``/health`` and ``/ui/`` stay open.
    """
    jobs_root = Path(jobs_dir) if jobs_dir else _default_jobs_dir()
    jobs_root.mkdir(parents=True, exist_ok=True)
    worker_count = (
        workers if workers and workers > 0 else _default_worker_count()
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """App lifespan: build the JobStore, recover crashed jobs, start
        the worker pool on startup; stop everything on shutdown."""
        store = JobStore(jobs_root)
        # Crash recovery runs once, before workers start (avoids
        # interleaved recover/claim double-processing across workers)
        store.recover()
        pool = [BatchWorker(store, _sdk) for _ in range(worker_count)]
        _app.state.jobs = store
        _app.state.workers = pool
        tasks = [asyncio.create_task(w.run_forever()) for w in pool]
        try:
            yield
        finally:
            for worker in pool:
                worker.stop()
            for task in tasks:
                task.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.gather(*tasks)

    app = FastAPI(
        title="Provendoc API",
        description=(
            "Evidence-first, domain-aware document ingestion — "
            "HTTP mapping of the SDK's four interfaces."
        ),
        version=_package_version(),
        lifespan=lifespan,
    )

    # ------------------------------------------
    # opt-in authentication (X-API-Key, PROVENDOC_API_KEY)
    # ------------------------------------------

    resolved_key = api_key if api_key is not None else _env_api_key()
    if resolved_key:

        @app.middleware("http")
        async def _require_api_key(request: Request, call_next):
            """Gate ``/api/*`` behind a matching X-API-Key header.

            ``/health`` (liveness probe for orchestrators) and the
            ``/ui/`` static console stay reachable; the console's own
            API calls still 401 without the key. The comparison uses
            hmac.compare_digest (constant time) so mismatched keys do
            not leak key material through response timing.
            """
            if request.url.path.startswith("/api/"):
                provided = request.headers.get("X-API-Key", "")
                if not hmac.compare_digest(provided, resolved_key):
                    return JSONResponse(
                        status_code=401,
                        content={
                            "detail": "invalid or missing API key: "
                                      "send the X-API-Key header",
                        },
                    )
            return await call_next(request)

    # ------------------------------------------
    # tuning frontend (/ui static hosting + root redirect)
    # ------------------------------------------

    if _WEB_DIR.is_dir():
        app.mount(
            "/ui",
            StaticFiles(directory=_WEB_DIR, html=True),
            name="ui",
        )

        @app.get("/", include_in_schema=False)
        async def root():
            """Root path 302 redirect to the /ui/ frontend (not in the schema)."""
            return RedirectResponse(url="/ui/")

    # ------------------------------------------
    # health / registry
    # ------------------------------------------

    @app.get("/health")
    async def health():
        """Liveness probe -- service status + package version + active domains."""
        return {
            "status": "ok",
            "version": _package_version(),
            "domains": sorted(get_all_domains()),
        }

    @app.get("/api/v1/domains")
    async def domains():
        """Domain registry -- per-domain suffixes, quality applicability,
        pipeline stages, and capability notes."""
        return {
            name: {
                "suffixes": sorted(info.suffixes),
                "quality_applicable": info.quality_applicable,
                "pipeline_stages": info.pipeline_stages,
                "status": info.status,
                # domain investment tier: core = actively developed /
                # frozen = maintenance mode (no deletion, no expansion)
                "investment": info.investment,
                "warnings": info.warnings,
                "representation": info.representation,
                "evaluation": info.evaluation,
                "optimization": info.optimization,
            }
            for name, info in get_all_domains().items()
        }

    # ------------------------------------------
    # the four SDK interfaces
    # ------------------------------------------

    @app.post("/api/v1/parse")
    async def parse(
        file: UploadFile,
        include_text: bool = True,
    ):
        """Parse only -- no representations, no evaluation.

        Parse failures (corrupt files etc.) return 200 with
        document.metadata.parse_failed=true in the envelope
        (honest-failure principle).
        """
        return await _run("parse", file, include_text)

    @app.post("/api/v1/chunk")
    async def chunk(
        file: UploadFile,
        include_text: bool = True,
    ):
        """Default-parameter semantic chunking -- parse + the default
        semantic representation."""
        return await _run("chunk", file, include_text)

    @app.post("/api/v1/evaluate")
    async def evaluate(
        file: UploadFile,
        include_text: bool = True,
        params: str = Form(""),
        stability_sweep: bool = Form(False),
        profile: bool = Form(False),
    ):
        """Representation Stack Evaluation -- user-tuned semantic
        representation + quality baseline.

        Semantic parameters ride as a JSON string in the ``params``
        form field (multipart cannot carry a standalone JSON body);
        invalid JSON -> 400.
        """
        return await _run(
            "evaluate", file, include_text,
            stability_sweep=stability_sweep,
            profile=profile,
            **_parse_params(params),
        )

    @app.post("/api/v1/auto-pipeline")
    async def auto_pipeline(
        file: UploadFile,
        include_text: bool = True,
        stability_sweep: bool = Form(False),
        profile: bool = Form(False),
    ):
        """Evaluation + automatic parameter optimization -- returns the
        default and optimized results side by side for comparison."""
        return await _run(
            "auto_pipeline", file, include_text,
            stability_sweep=stability_sweep,
            profile=profile,
        )

    # ------------------------------------------
    # config multi-file layered merge
    # ------------------------------------------

    @app.post("/api/v1/config/merge")
    async def config_merge(files: list[UploadFile]):
        """Config-family multi-file layered merge -- later files win.

        The eight structured-config families (yaml/yml, json/jsonc,
        xml, ini-family suffixes, properties/env/plist, toml+
        cargo.lock); upload order is the layering order (later files
        shadow earlier ones). All files must hit the same family,
        otherwise 400 (cross-format merging has no market precedent;
        refuse rather than guess). Returns the merged contract
        (attribution under entries[].source_file).
        """
        if len(files) < 2:
            raise HTTPException(
                status_code=422,
                detail="layered merge requires at least 2 files "
                       f"(got {len(files)})",
            )

        total_size = 0
        with tempfile.TemporaryDirectory(prefix="provendoc-api-") as tmp:
            staged_paths = []
            for idx, upload in enumerate(files):
                if not upload.filename:
                    raise HTTPException(
                        status_code=422, detail="missing file name",
                    )
                # staged path = numbered subdir + sanitized original
                # basename: the subdir isolates multipart name clashes;
                # the basename keeps the registry routing (dotfiles
                # whose canonical suffix is the file name, e.g. .env /
                # .gitconfig) and the merged contract's source_file
                # attribution aligned with the user's view.
                safe = re.sub(r"[^A-Za-z0-9._-]+", "_", upload.filename)
                staged = Path(tmp) / f"{idx:04d}" / (safe or "upload")
                staged.parent.mkdir(parents=True, exist_ok=True)
                size = 0
                with staged.open("wb") as fh:
                    while chunk := await upload.read(1024 * 1024):
                        size += len(chunk)
                        fh.write(chunk)
                if size == 0:
                    raise HTTPException(
                        status_code=422, detail="empty upload",
                    )
                total_size += size
                staged_paths.append(str(staged))

            try:
                merged = _sdk.merge_configs(staged_paths)
            except ValueError as e:
                # facade validation failure (mixed family / non-config
                # suffix) -> 400
                raise HTTPException(status_code=400, detail=str(e)) from e

        return {
            "files": [f.filename for f in files],
            "total_size": total_size,
            "merged": merged,
        }

    # ------------------------------------------
    # persistent batch jobs (async, durable)
    # ------------------------------------------

    @app.post("/api/v1/batch", status_code=202)
    async def submit_batch(
        files: list[UploadFile],
        method: str = Form("parse"),
        params: str = Form(""),
        include_text: bool = Form(True),
    ):
        """Submit a durable async batch job -- returns 202 + job_id
        immediately.

        File bytes and job config persist under jobs_root; a worker
        pool processes them in the background; after a restart,
        pending/running jobs are recovered and re-run.
        """
        if not files:
            raise HTTPException(status_code=422, detail="no files uploaded")
        if method not in BATCH_METHODS:
            raise HTTPException(
                status_code=400,
                detail=f"unknown method: {method} (choose from "
                       f"{', '.join(BATCH_METHODS)})",
            )
        kwargs = _parse_params(params)

        payloads = []
        for upload in files:
            if not upload.filename:
                raise HTTPException(status_code=422, detail="missing file name")
            data = await upload.read()
            if not data:
                raise HTTPException(
                    status_code=422,
                    detail=f"empty upload: {upload.filename}",
                )
            payloads.append((upload.filename, data))

        job = app.state.jobs.create(
            method=method, params=kwargs,
            include_text=include_text, files=payloads,
        )
        for worker in app.state.workers:
            worker.wake()
        return _job_summary(job)

    @app.get("/api/v1/batch")
    async def list_batch_jobs(
        status: str | None = None,
        method: str | None = None,
        limit: int | None = Query(default=None, ge=1),
        offset: int = Query(default=0, ge=0),
    ):
        """Job list -- status/method filters + limit/offset paging.

        The post-filter total rides in ``total``; ordering is stable
        (created_at ascending).
        """
        if status is not None and status not in JOB_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"unknown status: {status} (choose from "
                       f"{', '.join(JOB_STATUSES)})",
            )
        jobs = app.state.jobs.list_jobs()
        if status is not None:
            jobs = [j for j in jobs if j["status"] == status]
        if method is not None:
            jobs = [j for j in jobs if j["method"] == method]
        total = len(jobs)
        jobs = jobs[offset:offset + limit] if limit else jobs[offset:]
        return {
            "total": total,
            "jobs": [_job_summary(j) for j in jobs],
        }

    @app.get("/api/v1/batch/{job_id}")
    async def batch_job_status(job_id: str):
        """Job status -- aggregate counts + per-file progress."""
        job = app.state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
        return {**_job_summary(job), "files": job["files"]}

    @app.get("/api/v1/batch/{job_id}/results")
    async def batch_job_results(job_id: str):
        """Job results -- response envelopes of the completed files
        (incomplete ones are absent)."""
        job = app.state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
        results = []
        for f in job["files"]:
            if f["status"] == "completed":
                envelope = app.state.jobs.read_result(job_id, f["id"])
                if envelope is not None:
                    results.append(envelope)
        return {
            "job_id": job_id,
            "status": job["status"],
            **_job_summary(job)["counts"],
            "results": results,
        }

    @app.delete("/api/v1/batch/{job_id}")
    async def cancel_batch_job(job_id: str):
        """Cooperative cancel -- completed file results are kept,
        remaining files are marked skipped."""
        job = app.state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
        app.state.jobs.request_cancel(job_id)
        for worker in app.state.workers:
            worker.wake()
        job = app.state.jobs.get(job_id)
        return _job_summary(job)

    return app
