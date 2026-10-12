"""Pre-inference failures retain the primary stage as well as bounded worker telemetry."""

import json

import pytest
from test_scheduler_v2 import fake_stages, setup_scheduler

from datasheet_parser.errors import ReviewBudgetError


def test_scheduler_records_primary_budget_failure_and_preserves_completed_dependencies(tmp_path, monkeypatch, caplog):
    engine, store, run = setup_scheduler(tmp_path, workers=1)

    def fail_review(worker, current_run, sid, role):
        if role == "review":
            raise ReviewBudgetError(sid, 20577, 20000, "fixture-secret")

    fake_stages(monkeypatch, store, fail_review)
    with pytest.raises(ReviewBudgetError) as raised:
        engine.resume(run["id"], [10])
    assert raised.value._datasheet_stage == "review:10"
    failed = store.find("events", {"run_id": run["id"], "status": "failed"})[-1]
    primary = failed["primary_failure"]
    assert primary["stage"] == "review:10" and primary["code"] == "review_budget_exceeded"
    assert primary["details"]["input_tokens_estimate"] == 20577
    assert primary["details"]["request_token_budget"] == 20000
    assert failed["executed_stages"] == 3 and failed["secondary_failures"] == []
    assert store.get("results", f"{run['id']}:10")
    assert store.get("blind_reads", f"{run['id']}:10:blind")
    assert not store.find("reviews")
    assert "fixture-secret" not in json.dumps(failed) + caplog.text
    assert "review:10" in caplog.text and "review_budget_exceeded" in caplog.text
