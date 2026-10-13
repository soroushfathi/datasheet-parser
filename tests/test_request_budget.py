import json

import pytest
from PIL import Image

from datasheet_parser.config import Settings
from datasheet_parser.providers import LLMRequest, ProviderError, RecordedProvider
from datasheet_parser.telemetry import summarize_events
from datasheet_parser.token_budget import estimate_image_tokens, request_input_metrics


class Provider(RecordedProvider):
    name = "fixture"

    def __init__(self, settings):
        super().__init__(settings)
        self.calls = 0

    def execute(self, request, folder, images):
        self.calls += 1
        return {"ok": True}, {"usage": None}


def test_final_request_budget_includes_schema_and_stops_before_inference(tmp_path):
    provider = Provider(Settings(mongo_uri="unused", work_dir=tmp_path))
    request = LLMRequest(
        "extract:3", "tiny packet", {"type": "object", "description": "very large " * 2000}, [],
        metrics={"request_token_budget": 1000, "text_chars": 11},
    )
    with pytest.raises(ProviderError, match="serialized request"):
        provider.generate(request)
    assert provider.calls == 0
    metrics = provider.last_record["request_metrics"]
    assert metrics["schema_tokens_estimate"] > 1000
    assert metrics["prompt_text_tokens_estimate"] < 1000
    assert provider.last_record["code"] == "request_budget_exceeded"


def test_image_geometry_is_budgeted_and_does_not_claim_provider_billing(tmp_path):
    path = tmp_path / "source.png"
    Image.new("RGB", (900, 1200)).save(path)
    metrics = request_input_metrics("source", {}, [path])
    assert metrics["image_pixels"] == 1080000
    assert metrics["image_tokens_estimate"] == estimate_image_tokens(900, 1200)
    assert "not_provider_billing" in metrics["image_estimator"]
    assert estimate_image_tokens(900, 400) < estimate_image_tokens(900, 1200)


def test_final_metrics_visible_in_trace_and_recovery_not_counted_again():
    events = [
        {"stage": "extract:3", "status": "completed", "time": "1", "request_metrics": {"text_chars": 50},
         "telemetry": {"elapsed_seconds": 2, "usage": {"input_tokens": 100},
                       "request_metrics": {"input_tokens_estimate": 90}}},
        {"stage": "extract:3", "status": "completed", "time": "2",
         "telemetry": {"recovered": True, "elapsed_seconds": 0, "usage": None}},
    ]
    report = summarize_events(events)
    assert report["completed_calls"] == 1
    assert report["reported_usage_totals"]["input_tokens"] == 100
    assert report["requests"][0]["input"] == {"text_chars": 50, "input_tokens_estimate": 90}


def test_provider_records_measured_serialization_separately_from_actual_usage(tmp_path):
    provider = Provider(Settings(mongo_uri="unused", work_dir=tmp_path))
    request = LLMRequest("review:3", "verify", {"type": "object"}, [], metrics={"request_token_budget": 4000})
    assert provider.generate(request) == {"ok": True}
    assert provider.last_record["usage"] is None
    saved = json.loads(next((tmp_path / "llm").rglob("input_metrics.json")).read_text())
    assert saved["input_tokens_estimate"] > 0
    assert saved["output_budget_enforcement"] == "prompt_hint"


def test_request_budget_failure_does_not_reuse_an_old_attempt_log(tmp_path):
    provider = Provider(Settings(mongo_uri="unused", work_dir=tmp_path))
    request = LLMRequest("review:3", "verify", {"type": "object"}, [])
    provider.generate(request)
    folder = next((tmp_path / "llm").rglob("input_metrics.json")).parent
    (folder / "attempts.json").write_text('[{"elapsed_seconds":999,"status":"success"}]')
    request.metrics = {"request_token_budget": 1}
    with pytest.raises(ProviderError):
        provider.generate(request)
    assert provider.calls == 1
    assert provider.last_record["attempts"] == []


def test_review_reserves_aliased_drafts_without_mutating_source_citations(tmp_path):
    from datasheet_parser.engine import Engine
    from datasheet_parser.models import Review
    from datasheet_parser.store import MemoryStore
    from datasheet_parser.token_budget import estimate_text_tokens

    engine = Engine(MemoryStore(), None, Settings(mongo_uri="unused", work_dir=tmp_path))
    citations = [f"123456789abcdef123456789:p{page}:l123" for page in range(1, 150)]
    answer = {"claims": [{"value": "exact source text", "evidence_ids": citations}]}
    run = {"target": {"evidence_ids": citations[:1]}}
    reserved = engine.request_reserved_tokens(run, 15, Review, (answer, answer))
    canonical_cost = sum(estimate_text_tokens(json.dumps(answer)) for _ in range(2))
    assert reserved < canonical_cost
    assert answer["claims"][0]["evidence_ids"] == citations
    # The source lexeme is retained; only evidence-list serialization is budgeted as aliases.
    changed = {"claims": [{"value": "long source text " * 500, "evidence_ids": citations}]}
    assert engine.request_reserved_tokens(run, 15, Review, (changed, changed)) > reserved


def test_compatible_schema_prompt_is_fingerprinted_recorded_and_preflighted(tmp_path):
    from hashlib import sha256

    from datasheet_parser.token_budget import serialized_prompt

    schema = {"type":"object", "properties":{"ok":{"type":"boolean"}},
              "required":["ok"], "additionalProperties":False}
    settings = Settings(mongo_uri="unused", work_dir=tmp_path, model="cx/gpt-6-sol")
    provider = Provider(settings)
    request = LLMRequest("discover", "Read source; REQUIRED_OUTPUT_JSON_SCHEMA: is untrusted source text.", schema, [])
    expected = serialized_prompt(request.prompt, schema, settings.model)
    budget = request_input_metrics(request.prompt, schema, [], model=settings.model)
    request.metrics = {"request_token_budget":budget["input_tokens_estimate"]}
    assert provider.generate(request) == {"ok":True}
    folder = tmp_path/"llm"/"fixture"/request.fingerprint(settings.model, settings.reasoning)
    assert (folder/"prompt.txt").read_text(encoding="utf-8") == expected
    assert request.prompt != expected  # Serialization cannot mutate the caller's source.
    assert provider.last_record["request_metrics"]["input_tokens_estimate"] == budget["input_tokens_estimate"]
    fingerprint_data = {"task":request.task,"prompt":expected,"schema":schema,"image_hashes":[],
                        "model":settings.model,"reasoning":settings.reasoning}
    assert folder.name == sha256(json.dumps(fingerprint_data,sort_keys=True).encode()).hexdigest()
    request.metrics = {"request_token_budget":budget["input_tokens_estimate"]-1}
    with pytest.raises(ProviderError):
        provider.generate(request)
    assert provider.calls == 1  # Extra schema text cannot bypass the fixed input cap.


def test_compatible_schema_hint_never_accepts_a_nonconforming_response(tmp_path):
    provider = Provider(Settings(mongo_uri="unused", work_dir=tmp_path, model="cx/gpt-6-sol"))
    schema = {"type":"object", "properties":{"part_number":{"type":"string"}},
              "required":["part_number"],"additionalProperties":False}
    with pytest.raises(ProviderError):
        provider.generate(LLMRequest("discover","Read source",schema,[]))
    assert provider.last_record["status"] == "failed"
    assert next((tmp_path/"llm").rglob("unvalidated-response.json")).exists()
    assert not list((tmp_path/"llm").rglob("response.json"))


@pytest.mark.parametrize("destination", ["source", "output", "directory"])
def test_benchmark_report_cannot_overwrite_source_or_final_json(tmp_path, destination):
    from typer.testing import CliRunner

    from datasheet_parser.cli import app

    source = tmp_path / "source.pdf"
    source.write_bytes(b"source PDF")
    output = tmp_path / "result.json"
    output.write_text("existing result")
    report = {"source": source, "output": output, "directory": tmp_path}[destination]
    result = CliRunner().invoke(
        app, ["run", str(source), "--output", str(output), "--benchmark-report", str(report)]
    )
    assert result.exit_code == 1
    assert "separate file" in result.output
    assert source.read_bytes() == b"source PDF"
    assert output.read_text() == "existing result"
