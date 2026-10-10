"""Single-process public sample runner. Secrets and artifacts are never static files."""

import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Lock, Thread
from typing import Annotated
from uuid import uuid4

import pdfplumber
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.formparsers import MultiPartException

from .config import Settings
from .engine import Engine
from .ingestion import ingest
from .providers import make_provider
from .requirements import SECTIONS
from .store import MongoStore

MAX_UPLOAD = 20 * 1024 * 1024
MAX_PAGES = 100
STAGE = re.compile(r"^(extract|blind|review):(\d+): (started|completed|failed)$")
LOG = logging.getLogger(__name__)


def timestamp():
    return datetime.now(UTC).isoformat()


def validate_pdf(data):
    if not data.startswith(b"%PDF-"):
        raise ValueError("فایل معتبر PDF نیست.")
    try:
        with pdfplumber.open(BytesIO(data)) as pdf:
            count = len(pdf.pages)
            if not 1 <= count <= MAX_PAGES:
                raise ValueError("فایل باید بین ۱ تا ۱۰۰ صفحه داشته باشد.")
            if any(p.width > 2000 or p.height > 2000 for p in pdf.pages):
                raise ValueError("ابعاد صفحات این PDF بیش از حد مجاز است.")
            return count
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("PDF خوانده نشد؛ فایل رمزگذاری‌شده یا خراب را بررسی کن.") from error


class DemoJobs:
    """One paid job at a time, two waiting; three bounded inference workers per run.

    Records and uploads survive restarts. Existing run checkpoints are resumed.
    A single Uvicorn process owns this queue; do not deploy with multiple workers.
    """

    def __init__(self, store, settings, engine_factory=None):
        self.store, self.settings = store, settings
        self.engine_factory = engine_factory or (
            lambda callback: Engine(store, make_provider(settings), settings, callback)
        )
        self.queue = Queue(maxsize=2)
        self.lock = Lock()
        self.stopping = False
        self.worker = None

    def start(self):
        self.worker = Thread(target=self._work, daemon=True, name="demo-jobs")
        self.worker.start()
        for job in sorted(self.store.find("demo_jobs"), key=lambda j: j["created_at"]):
            if job["status"] in {"queued", "running"}:
                self.queue.put(job["id"])

    def submit(self, data, filename, pages):
        with self.lock:
            if self.queue.full():
                raise Full
            identifier = uuid4().hex
            original = self.store.artifact(data, "application/pdf", filename)
            self.store.put("demo_jobs", {
                "id": identifier, "filename": filename, "page_count": pages,
                "upload_id": original, "status": "queued", "created_at": timestamp(),
                "updated_at": timestamp(), "stage": "queued", "sequence": 0,
                "stages": {}, "target": None, "target_count": None,
                "model": self.settings.model,
            })
            self.queue.put_nowait(identifier)
            return identifier

    def update(self, identifier, **fields):
        with self.lock:
            job = self.store.get("demo_jobs", identifier)
            self.store.put("demo_jobs", {**job, **fields, "updated_at": timestamp()})

    @staticmethod
    def can_resume(job):
        code = (job.get("target") or {}).get("ordering_code") or ""
        return (job["status"] == "failed" and bool(job.get("run_id"))
                and not any(c in code for c in [";", ",", "\n"]) and len(code.split()) <= 1)

    def requeue(self, identifier):
        with self.lock:
            job = self.store.get("demo_jobs", identifier)
            if not self.can_resume(job):
                raise ValueError("این اجرا قابل ادامه نیست؛ یک نمونهٔ جدید شروع کن.")
            if self.queue.full():
                raise Full
            self.store.put("demo_jobs", {**job, "status": "queued", "error": None,
                "finished_at": None, "updated_at": timestamp()})
            self.queue.put_nowait(identifier)

    def event(self, identifier, message):
        with self.lock:
            job = self.store.get("demo_jobs", identifier)
            sequence = job["sequence"] + 1
            stages = dict(job["stages"])
            match = STAGE.match(message)
            if match:
                role, section, status = match.groups()
                stages[f"{role}:{section}"] = status
            self.store.put("demo_events", {"id": f"{identifier}:{sequence:05d}",
                "job_id": identifier, "sequence": sequence, "time": timestamp(), "message": message})
            self.store.put("demo_jobs", {**job, "sequence": sequence, "stages": stages,
                "stage": message, "updated_at": timestamp()})

    def snapshot(self, identifier, after=0):
        job = self.store.get("demo_jobs", identifier)
        events = sorted(self.store.find("demo_events", {"job_id": identifier,
            "sequence": {"$gt": after}}), key=lambda e: e["sequence"])
        # Only selected public fields leave the API. No paths, prompts, credentials or artifacts.
        keys = {"id", "filename", "page_count", "status", "created_at", "started_at", "finished_at",
                "stage", "sequence", "stages", "target", "target_count", "model", "summary", "error"}
        partial = []
        if job.get("run_id"):
            for record in sorted(self.store.find("results", {"run_id": job["run_id"]}),
                                 key=lambda r: r["section_id"]):
                section = record["section"]
                partial.append({"section_id": section["section_id"], "status": section["status"],
                    "claims": len(section["claims"]),
                    "reviewed": job["stages"].get(f"review:{section['section_id']}") == "completed",
                    "preview": [{"parameter": c["parameter"], "value": c["raw_value"] or c["value"] or "—"}
                                for c in section["claims"][:3]]})
        return {**{k: v for k, v in job.items() if k in keys}, "partial_sections": partial,
                "can_resume": self.can_resume(job),
                "events": [{k: e[k] for k in ("sequence", "time", "message")} for e in events]}

    def _work(self):
        while not self.stopping:
            try:
                identifier = self.queue.get(timeout=1)
            except Empty:
                continue
            try:
                self.run(identifier)
            except Exception as error:  # noqa: BLE001 - isolate jobs; never log arbitrary upstream text.
                # Avoid dumping arbitrary upstream errors containing auth or source data.
                LOG.error("Job failed: %s (%s)", identifier, type(error).__name__)
                self.update(identifier, status="failed", finished_at=timestamp(),
                            error="اجرای نمونه متوقف شد. اتصال API یا سند نیاز به بررسی دارد؛ شناسهٔ اجرا را ارسال کن.")
                self.event(identifier, "pipeline: failed")
            finally:
                self.queue.task_done()

    def run(self, identifier):
        job = self.store.get("demo_jobs", identifier)
        self.update(identifier, status="running", started_at=job.get("started_at", timestamp()))
        callback = lambda message: self.event(identifier, message)
        engine = self.engine_factory(callback)
        parse_id = job.get("parse_id")
        if not parse_id:
            self.event(identifier, "ingest: started")
            path = self.settings.work_dir / "uploads" / f"{identifier}.pdf"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.store.read_artifact(job["upload_id"]))
            try:
                parse_id = ingest(path, self.store, callback)
            finally:
                path.unlink(missing_ok=True)
            self.update(identifier, parse_id=parse_id)
            self.event(identifier, "ingest: completed")
        run_id = job.get("run_id")
        if not run_id:
            self.event(identifier, "targets: started")
            catalog = engine.discover(parse_id, separate_ordering_codes=True)
            if not catalog.targets:
                self.update(identifier, target_count=0)
                raise ValueError("No targets")
            # The first entry really is first: never silently choose the second
            # entry when the first is ambiguous or create outputs for all variants.
            target = catalog.targets[0]
            self.update(identifier, target_count=len(catalog.targets), target=target.model_dump(mode="json"))
            self.event(identifier, "targets: completed; first target selected")
            if target.status != "supported":
                self.update(identifier, status="failed", finished_at=timestamp(),
                            error="گزینهٔ اول در سند مبهم است؛ بدون رفع ابهام نمی‌توان برای آن خروجی معتبر ساخت.")
                return
            run_id = engine.create_run(parse_id, target.id)
            self.update(identifier, run_id=run_id)
        engine.resume(run_id, review=True)
        self.event(identifier, "export: started")
        result = engine.export(run_id).model_dump(mode="json")
        blob = self.store.artifact(json.dumps(result, ensure_ascii=False, indent=2).encode(),
                                   "application/json", f"{identifier}.json")
        self.update(identifier, result_id=blob, summary={
            "run_id": run_id, "sections": len(result["sections"]),
            "requirements": sum(len(s["findings"]) for s in result["sections"]),
            "claims": sum(len(s["claims"]) for s in result["sections"]),
            "issues": len(result["issues"]), "review_status": result["status"],
        }, status="completed", finished_at=timestamp())
        self.event(identifier, "export: completed")


def create_app(store=None, settings=None, jobs=None, static_dir=None):
    settings = settings or Settings.load()
    store = store or MongoStore(settings.mongo_uri, settings.database)
    jobs = jobs or DemoJobs(store, settings)
    static_dir = Path(static_dir or os.getenv("DATASHEET_DEMO_STATIC", "landing"))

    @asynccontextmanager
    async def lifespan(app):
        if hasattr(store, "ping"):
            store.ping()
            store.ensure_indexes()
            store.db.demo_events.create_index([("job_id", 1), ("sequence", 1)])
        jobs.start()
        yield
        jobs.stopping = True

    app = FastAPI(title="Datasheet sample", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.jobs = jobs

    @app.middleware("http")
    async def bounds_and_headers(request: Request, call_next):
        length = request.headers.get("content-length", "0")
        if length.isdigit() and int(length) > MAX_UPLOAD + 1024 * 1024:
            return Response(status_code=413, content="Upload too large")
        # Also bound chunked multipart uploads before Starlette spools them to disk.
        if request.method == "POST":
            receive = request._receive
            received = 0

            async def bounded_receive():
                nonlocal received
                message = await receive()
                received += len(message.get("body", b""))
                if received > MAX_UPLOAD + 1024 * 1024:
                    # The multipart parser closes any temporary files on this exception.
                    raise MultiPartException("Upload too large")
                return message

            request._receive = bounded_receive
        response = await call_next(request)
        if request.method == "POST" and received > MAX_UPLOAD + 1024 * 1024:
            response = Response(status_code=413, content="Upload too large")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; font-src 'self'; connect-src 'self'; frame-ancestors 'none'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/healthz")
    def health():
        return {"status": "ok", "service": "datasheet-demo"}

    @app.get("/api/config")
    def config():
        return {"model": settings.model, "max_upload_mb": 20, "max_pages": MAX_PAGES,
                "sections": [{"id": sid, "name": section[0]} for sid, section in SECTIONS.items()]}

    @app.post("/api/jobs", status_code=202)
    def upload(file: Annotated[UploadFile, File()]):
        try:
            data = file.file.read(MAX_UPLOAD + 1)
            if len(data) > MAX_UPLOAD:
                raise HTTPException(413, "حداکثر حجم فایل ۲۰ مگابایت است.")
            try:
                pages = validate_pdf(data)
            except ValueError as error:
                raise HTTPException(400, str(error)) from error
            filename = Path((file.filename or "datasheet.pdf").replace("\\", "/")).name[:200]
            try:
                identifier = jobs.submit(data, filename, pages)
            except Full as error:
                raise HTTPException(429, "صف آزمایش پر است؛ چند دقیقه بعد دوباره امتحان کن.") from error
            return {"id": identifier}
        finally:
            file.file.close()

    def get_job(identifier):
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise HTTPException(404, "اجرا پیدا نشد.")
        try:
            return store.get("demo_jobs", identifier)
        except KeyError as error:
            raise HTTPException(404, "اجرا پیدا نشد.") from error

    @app.get("/api/jobs/{identifier}")
    def status(identifier: str, after: int = Query(default=0, ge=0)):
        get_job(identifier)
        return jobs.snapshot(identifier, after)

    @app.get("/api/jobs/{identifier}/result")
    def result(identifier: str):
        job = get_job(identifier)
        if job["status"] != "completed":
            raise HTTPException(409, "خروجی هنوز آماده نیست.")
        return Response(store.read_artifact(job["result_id"]), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="datasheet-{identifier}.json"'})

    @app.post("/api/jobs/{identifier}/resume", status_code=202)
    def resume(identifier: str):
        get_job(identifier)
        try:
            jobs.requeue(identifier)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        except Full as error:
            raise HTTPException(429, "صف آزمایش پر است؛ کمی بعد دوباره امتحان کن.") from error
        return {"id": identifier}

    @app.get("/")
    @app.get("/demo")
    def page():
        return FileResponse(static_dir / "demo.html")

    # Expose an explicit static allowlist; never mount the repo, work directory or deploy config.
    @app.get("/demo.css")
    def styles():
        return FileResponse(static_dir / "demo.css")

    @app.get("/demo.js")
    def scripts():
        return FileResponse(static_dir / "demo.js")

    app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")
    return app
