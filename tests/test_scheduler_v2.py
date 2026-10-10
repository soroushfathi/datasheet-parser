"""Dependency, durable recovery and artifact safety checks without real inference."""

import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_core import TARGET

from datasheet_parser.config import Settings
from datasheet_parser.engine import Engine
from datasheet_parser.providers import PendingResponse
from datasheet_parser.store import MemoryStore, artifact_id, materialize


def setup_scheduler(tmp_path, workers=2, optimization="budget-v1"):
    store = MemoryStore()
    store.put("catalogs", {"id": "parse", "targets": [TARGET], "ambiguities": []})
    settings = Settings(
        mongo_uri="unused", work_dir=tmp_path, workers=workers, optimization=optimization,
        model="test-model", reasoning="medium", provider="test-provider",
    )
    engine = Engine(store, object(), settings)
    run_id = engine.create_run("parse", "target")
    return engine, store, store.get("runs", run_id)


def fake_stages(monkeypatch, store, callback=lambda worker, run, sid, role: None):
    def stage(worker, run, sid, role):
        callback(worker, run, sid, role)
        collection = {"extract": "results", "blind": "blind_reads", "review": "reviews"}[role]
        key = f"{run['id']}:{sid}" + (":blind" if role == "blind" else "")
        row = {"id": key, "run_id": run["id"], "section_id": sid, "section": {}}
        store.put(collection, row)
        return row

    def review(worker, run, sid, record):
        assert record["id"] == f"{run['id']}:{sid}"
        store.get("results", record["id"])
        store.get("blind_reads", record["id"] + ":blind")
        return stage(worker, run, sid, "review")

    monkeypatch.setattr(Engine, "extract_section", lambda w, r, sid: stage(w, r, sid, "extract"))
    monkeypatch.setattr(Engine, "blind_retrieved", lambda w, r, sid: stage(w, r, sid, "blind"))
    monkeypatch.setattr(Engine, "review_section", review)
    monkeypatch.setattr(Engine, "map_retrieval", lambda *args: None)


def test_new_runs_freeze_versions_and_budgets_without_changing_baseline(tmp_path):
    engine, store, run = setup_scheduler(tmp_path)
    assert (run["scheduler_version"], run["packet_version"], run["contract_version"]) == (
        "dag-v1", "budget-v1", "compact-v2"
    )
    assert run["prompt_version"] == "3"
    assert run["workers"] == 2
    assert run["retrieval_config"]["index_version"] == "structural-9"
    assert run["retrieval_config"]["text_token_budget"] == 6000
    engine.settings.packet_token_budget = 1000
    engine.settings.workers = 3
    assert store.get("runs", run["id"])["retrieval_config"]["text_token_budget"] == 6000
    assert store.get("runs", run["id"])["workers"] == 2
    _, _, baseline = setup_scheduler(tmp_path, optimization="baseline")
    assert baseline["prompt_version"] == "2"
    assert "scheduler_version" not in baseline
    assert "packet_version" not in baseline
    assert "contract_version" not in baseline
    assert "index_version" not in baseline["retrieval_config"]


@pytest.mark.parametrize("workers", [2, 3])
def test_global_bound_independent_reads_and_dependent_reviews(tmp_path, monkeypatch, workers):
    engine, store, run = setup_scheduler(tmp_path, workers)
    gate = threading.Barrier(workers)
    lock = threading.Lock()
    calls = []
    factories = []
    providers_by_thread = {}
    active = maximum = 0

    def provider_factory(settings):
        assert settings.workers == workers
        assert settings.model == run["model"]
        assert settings.reasoning == run["reasoning"]
        provider = object()
        with lock:
            factories.append(provider)
        return provider

    def callback(worker, current_run, sid, role):
        nonlocal active, maximum
        with lock:
            first_wave = len(calls) < workers
            calls.append((sid, role))
            active += 1
            maximum = max(maximum, active)
            thread = threading.get_ident()
            providers_by_thread.setdefault(thread, id(worker.provider))
            assert providers_by_thread[thread] == id(worker.provider)
        if first_wave:
            # The first independent readers must overlap; a nested serial section runner deadlocks here.
            gate.wait(timeout=5)
        with lock:
            active -= 1

    monkeypatch.setattr("datasheet_parser.engine.make_provider", provider_factory)
    fake_stages(monkeypatch, store, callback)
    engine.resume(run["id"], [7, 1, 14, 2])
    assert maximum == workers
    assert len(factories) == workers
    assert len(set(providers_by_thread.values())) == workers
    assert {(2, "extract"), (2, "blind")} <= set(calls[:workers])
    assert len(store.find("results")) == 4
    assert len(store.find("blind_reads")) == 4
    assert len(store.find("reviews")) == 4
    assert len(calls) == 12
    assert store.get("runs", run["id"])["status"] == "partial"
    # No provider creation or model-stage repeat after durable completion.
    engine.resume(run["id"], [7, 1, 14, 2])
    assert len(calls) == 12
    assert len(factories) == workers
    events = store.find("events", {"run_id": run["id"], "status": "completed"})
    assert events[-1]["reused_stages"] == 12
    assert events[-1]["executed_stages"] == 0


def test_failure_stops_submission_drains_inflight_and_resume_reuses_it(tmp_path, monkeypatch):
    from datasheet_parser import engine as module

    engine, store, run = setup_scheduler(tmp_path)
    blind_started = threading.Event()
    release_blind = threading.Event()
    calls = []
    first_attempt = True
    wait_original = module.wait

    def callback(worker, current_run, sid, role):
        calls.append((sid, role))
        if first_attempt and sid == 2:
            if role == "blind":
                blind_started.set()
                assert release_blind.wait(timeout=5)
            elif role == "extract":
                assert blind_started.wait(timeout=5)
                raise RuntimeError("failed source read")

    def wait_first_wave(*args, **kwargs):
        result = wait_original(*args, **kwargs)
        # The scheduler sees the failed extractor before the blind reader is allowed to finish.
        release_blind.set()
        return result

    fake_stages(monkeypatch, store, callback)
    monkeypatch.setattr(module, "make_provider", lambda settings: object())
    monkeypatch.setattr(module, "wait", wait_first_wave)
    with pytest.raises(RuntimeError, match="failed source read"):
        engine.resume(run["id"], [1, 2, 14])
    assert Counter(calls) == Counter([(2, "extract"), (2, "blind")])
    assert store.get("blind_reads", f"{run['id']}:2:blind")
    assert store.get("runs", run["id"])["status"] == "failed"
    first_attempt = False
    engine.resume(run["id"], [1, 2, 14])
    assert Counter(calls)[(2, "blind")] == 1
    assert Counter(calls)[(2, "extract")] == 2
    assert len(store.find("reviews")) == 3


def test_single_worker_preserves_injected_provider_and_external_pending_status(tmp_path, monkeypatch):
    engine, store, run = setup_scheduler(tmp_path, workers=1)
    provider = engine.provider

    def callback(worker, current_run, sid, role):
        assert worker.provider is provider
        if role == "blind":
            raise PendingResponse("Awaiting reviewer input")

    fake_stages(monkeypatch, store, callback)
    monkeypatch.setattr(
        "datasheet_parser.engine.make_provider", lambda settings: pytest.fail("Unexpected new provider")
    )
    with pytest.raises(PendingResponse):
        engine.resume(run["id"], [2])
    assert store.get("runs", run["id"])["status"] == "awaiting_external_response"
    assert store.get("results", f"{run['id']}:2")
    assert not store.find("reviews")


def test_old_runs_keep_section_scheduler_when_settings_now_enable_optimization(tmp_path, monkeypatch):
    engine, _, run = setup_scheduler(tmp_path, optimization="baseline")
    engine.settings.optimization = "budget-v1"
    called = []
    monkeypatch.setattr(engine, "map_retrieval", lambda current_run: None)
    monkeypatch.setattr(engine, "parallel_sections", lambda *args: called.append(args))
    monkeypatch.setattr(engine, "schedule_stages", lambda *args: pytest.fail("Upgraded existing run"))
    engine.resume(run["id"], [1, 2])
    assert len(called) == 1
    assert called[0][0].get("scheduler_version") is None


def test_extract_only_never_launches_blind_or_review(tmp_path, monkeypatch):
    engine, store, run = setup_scheduler(tmp_path)
    calls = []
    fake_stages(monkeypatch, store, lambda worker, run, sid, role: calls.append(role))
    monkeypatch.setattr("datasheet_parser.engine.make_provider", lambda settings: object())
    engine.resume(run["id"], [1, 2], review=False)
    assert calls == ["extract", "extract"]
    assert not store.find("blind_reads")
    assert not store.find("reviews")


def test_concurrent_materialization_never_publishes_partial_files(tmp_path, monkeypatch):
    store = MemoryStore()
    data = b"complete image content" * 10000
    identifier = store.artifact(data, "image/png", "fixture.png")
    path = tmp_path / f"{identifier}.png"
    gate = threading.Barrier(3)
    release = threading.Event()
    original_write = Path.write_bytes

    def slow_write(temporary, blob):
        if temporary.suffix != ".tmp":
            return original_write(temporary, blob)
        with temporary.open("wb") as stream:
            midpoint = len(blob) // 2
            stream.write(blob[:midpoint])
            stream.flush()
            gate.wait(timeout=5)
            assert release.wait(timeout=5)
            stream.write(blob[midpoint:])
        return len(blob)

    monkeypatch.setattr(Path, "write_bytes", slow_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(materialize, store, identifier, tmp_path, ".png") for _ in range(2)]
        gate.wait(timeout=5)
        assert not path.exists()
        release.set()
        assert [future.result() for future in futures] == [path, path]
    assert artifact_id(path.read_bytes()) == identifier
    assert not list(tmp_path.glob("*.tmp"))


def test_materialization_rejects_corrupt_artifact_before_publication(tmp_path):
    store = MemoryStore()
    identifier = store.artifact(b"original", "image/png", "fixture.png")
    store.blobs[identifier] = b"corrupted"
    with pytest.raises(ValueError, match="checksum"):
        materialize(store, identifier, tmp_path, ".png")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("published", [b"original", b"corrupted", None])
def test_windows_publication_collision_accepts_only_complete_matching_winner(tmp_path, monkeypatch, published):
    store = MemoryStore()
    identifier = store.artifact(b"original", "image/png", "fixture.png")
    destination = tmp_path / f"{identifier}.png"

    def denied_replace(temporary, target):
        if published is not None:
            Path(target).write_bytes(published)
        raise PermissionError("Competing cache reader holds destination")

    monkeypatch.setattr(Path, "replace", denied_replace)
    if published == b"original":
        assert materialize(store, identifier, tmp_path, ".png") == destination
    else:
        with pytest.raises(PermissionError):
            materialize(store, identifier, tmp_path, ".png")
    assert not list(tmp_path.glob("*.tmp"))
