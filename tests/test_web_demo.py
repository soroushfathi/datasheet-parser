import json

import httpx
import pytest
from PIL import Image

from datasheet_parser.config import Settings
from datasheet_parser.engine import Engine
from datasheet_parser.models import Catalog, Target
from datasheet_parser.providers import LLMRequest, OpenAIChatProvider, ProviderError
from datasheet_parser.store import MemoryStore

TestClient = pytest.importorskip("fastapi.testclient", reason="Install the web extra to test the demo").TestClient
web_demo = pytest.importorskip("datasheet_parser.web_demo", exc_type=ImportError)
DemoJobs, create_app, validate_pdf = web_demo.DemoJobs, web_demo.create_app, web_demo.validate_pdf


class EventStore(MemoryStore):
    def find(self, collection, query=None):
        if query and "sequence" in query:
            return [e for e in super().find(collection, {"job_id": query["job_id"]})
                    if e["sequence"] > query["sequence"]["$gt"]]
        return super().find(collection, query)


@pytest.fixture
def setup(tmp_path):
    store = EventStore()
    settings = Settings(mongo_uri="unused", work_dir=tmp_path, model="fixture", provider="openai-chat")
    return store, settings, DemoJobs(store, settings)


def target(name, status="supported"):
    return Target(id=name, manufacturer="Fixture", part_number=name, ordering_code=name,
                  package_name="SOIC-16", package_code="D", temperature_grade=None,
                  document_revision=None, evidence_ids=["p1"], status=status, reason="fixture")


@pytest.mark.parametrize("joined", ["FIRST; SECOND", "FIRST,SECOND", "FIRST and SECOND"])
def test_demo_discovery_rejects_grouped_skus(tmp_path, joined):
    settings = Settings(mongo_uri="unused", work_dir=tmp_path, pipeline="legacy")
    engine = Engine(MemoryStore(), None, settings)
    engine.pages = lambda parse_id: [{"number": 1, "requires_visual_read": False, "text": "source"}]
    engine.packet = lambda *args, **kwargs: ("source", [], {"p1"})

    def call(run_id, stage, model, prompt, images):
        assert "one target per explicitly printed orderable SKU" in prompt
        return Catalog(targets=[target(joined)], ambiguities=[])

    engine.call = call
    with pytest.raises(ValueError, match="combined several ordering codes"):
        engine.discover("parse", separate_ordering_codes=True)


def test_first_target_notice_and_only_one_run(setup):
    _store, _settings, jobs = setup
    calls = []

    class Engine:
        def discover(self, parse_id, *, separate_ordering_codes):
            assert separate_ordering_codes is True
            return Catalog(targets=[target("FIRST"), target("SECOND")], ambiguities=[])

        def create_run(self, parse_id, target_id):
            calls.append(target_id)
            return "run"

        def resume(self, run_id, review):
            assert review is True
            jobs.event(identifier, "extract:1: completed")

        def export(self, run_id):
            class Result:
                def model_dump(self, **kwargs):
                    return {"sections": [{"findings": [1], "claims": [1]}] * 17,
                            "issues": [{"code": "unverified"}], "status": "needs_review"}
            return Result()

    identifier = jobs.submit(b"fake fixture", "source.pdf", 1)
    jobs.update(identifier, parse_id="parse")
    jobs.engine_factory = lambda callback: Engine()
    jobs.run(identifier)
    public = jobs.snapshot(identifier)
    assert calls == ["FIRST"]
    assert public["target_count"] == 2 and public["target"]["ordering_code"] == "FIRST"
    assert public["status"] == "completed" and public["summary"]["issues"] == 1
    assert "result_id" not in public and "upload_id" not in public
    assert jobs.snapshot(identifier, public["sequence"])["events"] == []


def test_ambiguous_first_never_silently_selects_second(setup):
    _store, _settings, jobs = setup

    class Engine:
        def discover(self, parse_id, *, separate_ordering_codes):
            return Catalog(targets=[target("FIRST", "ambiguous"), target("SECOND")], ambiguities=[])

        def create_run(self, *args):
            pytest.fail("Must not create run for an ambiguous first target")

    identifier = jobs.submit(b"fixture", "x.pdf", 1)
    jobs.update(identifier, parse_id="parse")
    jobs.engine_factory = lambda callback: Engine()
    jobs.run(identifier)
    public = jobs.snapshot(identifier)
    assert public["status"] == "failed" and public["target"]["id"] == "FIRST"
    assert public["target_count"] == 2


def test_restart_resumes_recorded_run_without_discovery(setup):
    _store, _settings, jobs = setup
    identifier = jobs.submit(b"fixture", "x.pdf", 1)
    jobs.update(identifier, parse_id="parse", run_id="existing")

    class Engine:
        def resume(self, run_id, review):
            assert run_id == "existing"
            raise RuntimeError("test stop before export")

    jobs.engine_factory = lambda callback: Engine()
    with pytest.raises(RuntimeError):
        jobs.run(identifier)


def test_resume_endpoint_reuses_run_and_rejects_double_enqueue(setup):
    store, settings, jobs = setup
    identifier = jobs.submit(b"fixture", "x.pdf", 1)
    jobs.queue.get_nowait()
    jobs.update(identifier, status="failed", run_id="existing", target=target("FIRST").model_dump(),
                stages={"extract:1": "completed"}, finished_at="fixture", error="fixture")
    client = TestClient(create_app(store, settings, jobs))
    assert client.get(f"/api/jobs/{identifier}").json()["can_resume"]
    assert client.post(f"/api/jobs/{identifier}/resume").status_code == 202
    job = store.get("demo_jobs", identifier)
    assert job["run_id"] == "existing" and job["stages"] == {"extract:1": "completed"}
    assert job["finished_at"] is None and job["error"] is None
    assert client.post(f"/api/jobs/{identifier}/resume").status_code == 409
    assert jobs.queue.get_nowait() == identifier and jobs.queue.empty()


def test_api_rejects_invalid_pdf_and_hides_unready_output(setup):
    store, settings, jobs = setup
    app = create_app(store, settings, jobs)
    client = TestClient(app)  # Deliberately no lifespan: never starts paid worker in tests.
    r = client.post("/api/jobs", files={"file": ("x.pdf", b"not a pdf", "application/pdf")})
    assert r.status_code == 400
    identifier = jobs.submit(b"fixture", "x.pdf", 1)
    assert client.get(f"/api/jobs/{identifier}/result").status_code == 409
    assert client.get("/api/jobs/invalid").status_code == 404
    assert client.get("/.env").status_code == 404
    assert client.get("/deploy/server.py").status_code == 404
    assert client.get("/api/config").headers["cache-control"] == "no-store"


def test_queue_limit_and_api_upload_normalizes_filename(setup, monkeypatch):
    store, settings, jobs = setup
    monkeypatch.setattr("datasheet_parser.web_demo.validate_pdf", lambda data: 1)
    client = TestClient(create_app(store, settings, jobs))
    for _ in range(2):
        r = client.post("/api/jobs", files={"file": ("../../secret.pdf", b"fixture", "application/pdf")})
        assert r.status_code == 202
        job = store.get("demo_jobs", r.json()["id"])
        assert job["filename"] == "secret.pdf"
    assert client.post("/api/jobs", files={"file": ("x.pdf", b"fixture")}).status_code == 429


def test_pdf_validation_on_real_fixture():
    # Existing supplied MAX3232 is not required on another developer's machine.
    with pytest.raises(ValueError):
        validate_pdf(b"%PDF-1.7\ninvalid structure")


def test_chunked_body_limit_before_multipart_spooling(setup, monkeypatch):
    store, settings, jobs = setup
    monkeypatch.setattr("datasheet_parser.web_demo.MAX_UPLOAD", 100)
    client = TestClient(create_app(store, settings, jobs))
    r = client.post("/api/jobs", content=iter([b"x" * (1024 * 1024 + 200)]),
                    headers={"Content-Type": "multipart/form-data; boundary=fixture"})
    assert r.status_code == 413


@pytest.mark.parametrize("finish,refusal", [("length", None), ("stop", "declined")])
def test_chat_transport_rejects_truncated_or_refused_json(tmp_path, monkeypatch, finish, refusal):
    configure_transport(monkeypatch, finish, refusal)
    settings = Settings(mongo_uri="unused", work_dir=tmp_path, model="fixture", provider="openai-chat")
    with pytest.raises(ProviderError, match="Incomplete"):
        OpenAIChatProvider(settings).generate(request())


def request(images=None):
    return LLMRequest("smoke", "Return ok true", {"type": "object", "properties": {
        "ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}, images or [], 1024)


def configure_transport(monkeypatch, finish="stop", refusal=None):
    import openai
    real_client = openai.OpenAI
    seen = []

    def handler(req):
        payload = json.loads(req.content)
        seen.append(payload)
        chunks = [
            {"id": "fixture", "object": "chat.completion.chunk", "created": 0, "model": "fixture",
             "choices": [{"index": 0, "delta": {"content": '{"ok":true}', "refusal": refusal},
                          "finish_reason": None}]},
            {"id": "fixture", "object": "chat.completion.chunk", "created": 0, "model": "fixture",
             "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
             "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
        ]
        body = "".join("data: " + json.dumps(ch) + "\n\n" for ch in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=body)

    monkeypatch.setenv("OPENAI_API_KEY", "fixture-secret")
    monkeypatch.setattr(openai, "OpenAI", lambda **kwargs: real_client(
        **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler)), base_url="https://fixture/v1"))
    return seen


def test_chat_vision_schema_usage_and_secret_isolation(tmp_path, monkeypatch):
    seen = configure_transport(monkeypatch)
    bitmap = tmp_path / "vision.png"
    Image.new("RGB", (20, 20), "red").save(bitmap)
    settings = Settings(mongo_uri="unused", work_dir=tmp_path, model="fixture", provider="openai-chat")
    provider = OpenAIChatProvider(settings)
    assert provider.generate(request([bitmap])) == {"ok": True}
    payload = seen[0]
    assert payload["stream"] is True and payload["response_format"]["json_schema"]["strict"]
    assert payload["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert provider.last_record["usage"]["total_tokens"] == 15
    assert payload["max_completion_tokens"] > request().output_budget
    assert all("fixture-secret" not in p.read_text() for p in (tmp_path / "llm").rglob("*.json"))
