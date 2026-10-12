"""Three observed upstream 504s permit bounded whole-requirement recovery on resume."""

import io
import json
from copy import deepcopy

import pytest
from PIL import Image
from test_optimized_workflow import (
    FixtureProvider,
    engine,
    no_network_provider_or_tokenizer,  # noqa: F401
    source,
)
from test_review_batches import REQS, draft, prepare_factory

from datasheet_parser.errors import ReviewBudgetError
from datasheet_parser.providers import ProviderError
from datasheet_parser.requirements import requirements
from datasheet_parser.review_batches import plan_review_batches
from datasheet_parser.token_budget import request_input_metrics


def attempts(statuses=(504, 504, 504)):
    return [{"attempt": n, "status": "failed", "http_status": status} for n, status in enumerate(statuses, 1)]


class GatewayFixtureProvider(FixtureProvider):
    def __init__(self, settings, store):
        super().__init__(settings, store)
        self.fail_whole = True
        self.fail_part = None

    def execute(self, request, folder, images):
        if request.task.startswith("review:") and (
            self.fail_whole and ":part:" not in request.task or request.task == self.fail_part
        ):
            self.requests.append(request)
            (folder / "attempts.json").write_text(json.dumps(attempts()), encoding="utf-8")
            raise ProviderError("Synthetic exhaustion of three upstream HTTP 504 attempts", code="gateway_error")
        return super().execute(request, folder, images)


def fixture(tmp_path, *, quality=True):
    store = source(["Power requirements\nSupply rails sequencing decoupling capacitance startup", ""])
    parser, _, run_id = engine(tmp_path, store)
    run = store.get("runs", run_id)
    run["quality_version"] = "quality-v1" if quality else "baseline"
    store.put("runs", run)
    _, retriever = parser.retrieval_context(run)
    selected = {chunk["id"] for chunk in retriever.chunks}
    retriever.select = lambda *args, **kwargs: (selected, {cid: {"route": "seed", "rank_score": 10} for cid in selected})
    parser.map_retrieval(run)
    original = parser.extract_retrieved(run, 5)
    independent = parser.blind_retrieved(run, 5)
    provider = GatewayFixtureProvider(parser.settings, store)
    provider.run_id = run_id
    parser.provider = provider
    return parser, provider, store, run, original, independent


def seed_failure(store, run, *, stage="review:5", statuses=(504, 504, 504), code="gateway_error", status="failed"):
    store.put("events", {"id": "recorded-gateway-failure", "run_id": run["id"], "stage": stage,
                         "status": status, "kind": "llm", "time": "2026-10-13T00:00:00+00:00",
                         "telemetry": {"code": code, "attempts": attempts(statuses)}})


def prompt_view(request, label):
    view = json.loads(request.prompt.split(f"{label}: ", 1)[1].split("\n", 1)[0])
    if "claim_tables" in view:
        view["claims"] = [dict(zip(table["columns"], row, strict=True))
                          for table in view["claim_tables"] for row in table["rows"]]
    return view


def test_forced_single_requirement_plan_preserves_complete_local_and_global_answers():
    original, independent = draft("original"), draft("independent")
    before = deepcopy((original, independent))
    captured = []
    planned = plan_review_batches(original, independent, REQS, prepare_factory(captured), 10000, 10,
                                  max_requirements_per_part=1)
    assert [part["scope"] for part in planned] == [[req] for req in REQS]
    assert len(captured) == 3  # No oversized whole-section request is planned for inference.
    for left, right, scope in captured:
        for view, full in [(left, original), (right, independent)]:
            assert view["claims"] == [claim for claim in full["claims"] if claim["requirement_id"] in scope]
            assert view["findings"] == [finding for finding in full["findings"] if finding["requirement_id"] in scope]
            assert all(view[key] == full[key] for key in ["pins", "circuits", "notes"])
    assert (original, independent) == before
    default = plan_review_batches(original, independent, REQS, prepare_factory([]), 10000, 10)
    assert len(default) == 1 and default[0]["scope"] == REQS


@pytest.mark.parametrize("invalid", [0, -1, 1.5, True])
def test_invalid_forced_part_size_is_rejected(invalid):
    with pytest.raises(ValueError, match="positive integer"):
        plan_review_batches(draft("o"), draft("i"), REQS, prepare_factory([]), 1000, 10,
                            max_requirements_per_part=invalid)


@pytest.mark.parametrize("stage,statuses,code,event_status", [
    ("review:5", (504, 504), "gateway_error", "failed"),
    ("review:5", (504, 504, 429), "gateway_error", "failed"),
    ("review:5", (502, 502, 502), "gateway_error", "failed"),
    ("review:5", (504, 504, 504), "connection_error", "failed"),
    ("review:1", (504, 504, 504), "gateway_error", "failed"),
    ("review:51", (504, 504, 504), "gateway_error", "failed"),
    ("extract:5", (504, 504, 504), "gateway_error", "failed"),
    ("review:5", (504, 504, 504), "gateway_error", "completed"),
])
def test_other_failures_do_not_trigger_transport_partitioning(tmp_path, stage, statuses, code, event_status):
    parser, provider, store, run, original, _ = fixture(tmp_path)
    seed_failure(store, run, stage=stage, statuses=statuses, code=code, status=event_status)
    provider.fail_whole = False
    row = parser.review_retrieved(run, 5, original)
    assert [request.task for request in provider.requests] == ["review:5"]
    assert "partition_reason" not in row
    assert not store.find("review_batch_plans", {"run_id": run["id"]})


def test_duplicate_attempt_numbers_are_not_three_independent_upstream_failures(tmp_path):
    parser, provider, store, run, original, _ = fixture(tmp_path)
    seed_failure(store, run)
    event = store.get("events", "recorded-gateway-failure")
    for attempt in event["telemetry"]["attempts"]:
        attempt["attempt"] = 1
    store.put("events", event)
    provider.fail_whole = False
    row = parser.review_retrieved(run, 5, original)
    assert [request.task for request in provider.requests] == ["review:5"]
    assert "partition_reason" not in row


def test_baseline_review_is_not_repartitioned_by_recorded_504s(tmp_path):
    parser, provider, store, run, original, _ = fixture(tmp_path, quality=False)
    seed_failure(store, run)
    provider.fail_whole = False
    row = parser.review_retrieved(run, 5, original)
    assert [request.task for request in provider.requests] == ["review:5"]
    assert "partition_reason" not in row


def test_real_failed_event_shape_recovers_one_whole_requirement_per_part_without_expansion(tmp_path):
    parser, provider, store, run, original, independent = fixture(tmp_path)
    with pytest.raises(ProviderError, match="three upstream"):
        parser.review_retrieved(run, 5, original)
    assert provider.requests[0].task == "review:5"
    assert not store.find("reviews", {"run_id": run["id"]})
    failed_packet = store.get("evidence_packets", f"{run['id']}:5:review:0")
    before_run = store.get("runs", run["id"])
    provider.fail_whole = False
    row = parser.review_retrieved(run, 5, original)
    recovered = provider.requests[1:]
    assert len(recovered) == len(requirements(5)) == row["partition_count"]
    assert row["partition_reason"] == "gateway_504_recovery"
    assert row["max_requirements_per_part"] == 1
    assert row["gateway_504_recovery"]["gateway_504_attempts"] == 3
    plan = store.get("review_batch_plans", row["partition_plan_id"])
    assert plan["partition_reason"] == "gateway_504_recovery"
    assert [part["scope"] for part in plan["parts"]] == [[req] for req in requirements(5)]
    final_packet = store.get("evidence_packets", f"{run['id']}:5:review:0")
    assert final_packet["aliases"] == failed_packet["aliases"]
    assert final_packet["chunk_ids"] == failed_packet["chunk_ids"]
    assert all(request.images == provider.requests[0].images for request in recovered)
    for request, part in zip(recovered, plan["parts"], strict=True):
        assert "REVIEW_PARTITION_POLICY: gateway_504_recovery" in request.prompt
        assert "ORIGINAL_PRESENCE_INDEX:" in request.prompt
        assert request.metrics["review_partition_reason"] == "gateway_504_recovery"
        assert request.metrics["request_token_budget"] == 20000
        actual = request_input_metrics(request.prompt, request.schema, request.images)
        assert actual == request.metrics["serialized_preflight"]
        assert actual["input_tokens_estimate"] <= 20000
        for label, complete in [("ORIGINAL", original), ("INDEPENDENT", independent)]:
            scoped = prompt_view(request, label)
            expected = [claim for claim in complete["section"]["claims"] if claim["requirement_id"] in part["scope"]]
            assert {claim["id"] for claim in scoped["claims"]} == {claim["id"] for claim in expected}
            for claim, full in zip(scoped["claims"], expected, strict=True):
                assert claim["value"] == full["value"]
                assert claim["conditions"] == full["conditions"]
    assert store.get("runs", run["id"]) == before_run  # No model/reasoning/cap/run-configuration mutation.
    assert set(row["supported_claim_ids"]) == {claim["id"] for claim in original["section"]["claims"]}
    assert len(store.find("review_parts", {"run_id": run["id"]})) == len(requirements(5))


def test_saved_final_review_returns_before_scanning_old_failure_events(tmp_path, monkeypatch):
    parser, provider, store, run, original, _ = fixture(tmp_path)
    provider.fail_whole = False
    complete = parser.review_retrieved(run, 5, original)
    seed_failure(store, run)

    def forbidden_scan(*args, **kwargs):
        raise AssertionError("A saved completed review must not reopen recovery planning")

    monkeypatch.setattr(parser, "_review_gateway_504_recovery", forbidden_scan)
    assert parser.review_retrieved(run, 5, original) == complete
    assert len(provider.requests) == 1


def test_failed_recovery_part_resumes_with_stable_plan_and_reuses_successful_part(tmp_path):
    parser, provider, store, run, original, _ = fixture(tmp_path)
    seed_failure(store, run, stage="review:5:part:7")
    provider.fail_whole = False
    provider.fail_part = "review:5:part:2"
    with pytest.raises(ProviderError, match="three upstream"):
        parser.review_retrieved(run, 5, original)
    old_plan = store.find("review_batch_plans", {"run_id": run["id"]})[0]
    parts = store.find("review_parts", {"run_id": run["id"]})
    assert len(parts) == 1 and parts[0]["part"] == 1
    provider.fail_part = None
    row = parser.review_retrieved(run, 5, original)
    assert row["partition_plan_hash"] == old_plan["plan_hash"]
    assert [request.task for request in provider.requests] == [
        "review:5:part:1", "review:5:part:2", "review:5:part:2", "review:5:part:3", "review:5:part:4",
    ]


def test_changed_source_image_invalidates_the_recovery_plan_and_part_checkpoint(tmp_path):
    parser, provider, store, run, original, _ = fixture(tmp_path)
    seed_failure(store, run)
    provider.fail_whole = False
    provider.fail_part = "review:5:part:2"
    with pytest.raises(ProviderError):
        parser.review_retrieved(run, 5, original)
    old_plan = store.find("review_batch_plans", {"run_id": run["id"]})[0]
    bitmap = io.BytesIO()
    Image.new("RGB", (240, 320), "black").save(bitmap, format="PNG")
    page = store.get("pages", "parse:p2")
    store.put("pages", {**page, "image_artifact_id": store.artifact(bitmap.getvalue(), "image/png", "changed.png")})
    provider.fail_part = None
    row = parser.review_retrieved(run, 5, original)
    assert row["partition_plan_hash"] != old_plan["plan_hash"]
    assert [request.task for request in provider.requests].count("review:5:part:1") == 2


def test_all_forced_parts_preflight_before_inference_and_oversized_singleton_is_not_clipped(tmp_path):
    parser, provider, store, run, original, _ = fixture(tmp_path)
    seed_failure(store, run)
    # The last requirement fails, proving no earlier preflight success starts a call.
    original["section"]["claims"][-1]["conditions"].append("Exact indivisible source condition: " + "A" * 22000)
    before = deepcopy(original)
    with pytest.raises(ReviewBudgetError):
        parser.review_retrieved(run, 5, original)
    assert provider.requests == []
    assert original == before
    assert store.find("review_parts", {"run_id": run["id"]}) == []
