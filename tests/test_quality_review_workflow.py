"""Offline scoped-review context, exact reconciliation audit and budget integration."""

import copy
import json
from hashlib import sha256

import pytest
from test_optimized_workflow import (
    FixtureProvider,
    engine,
    no_network_provider_or_tokenizer,  # noqa: F401 -- prohibit model/tokenizer network use
    source,
)

from datasheet_parser.contracts import cited_regions, compact_review
from datasheet_parser.engine import POLICY
from datasheet_parser.errors import ReviewBudgetError
from datasheet_parser.models import Review
from datasheet_parser.providers import strict_schema
from datasheet_parser.requirements import requirements
from datasheet_parser.review_batches import scoped_view
from datasheet_parser.token_budget import request_input_metrics


def restore_claims(view):
    return view.get("claims", []) or [dict(zip(table["columns"], row, strict=True))
                                    for table in view.get("claim_tables", []) for row in table["rows"]]


def fixture(tmp_path):
    store = source(["Recommended operating conditions\nVCC typical 3.3 V"])
    parser, _, run_id = engine(tmp_path, store)
    run = store.get("runs", run_id)
    # Read fixtures through the established extraction path. Enable the new review
    # contract explicitly afterward so this test isolates the review change.
    run.pop("quality_version", None)
    store.put("runs", run)
    parser.map_retrieval(run)
    original = parser.extract_retrieved(run, 4)
    independent = parser.blind_retrieved(run, 4)
    for role, record in [("original", original), ("independent", independent)]:
        for claim in record["section"]["claims"]:
            req = claim["requirement_id"]
            claim.update(kind="text", quantity=None, raw_value=None,
                         value=f"{req} exact original fact", conditions=["0°C ≤ TA ≤ 55°C"])
            if role == "independent":
                claim["id"] = f"independent:{claim['id']}"
    store.put("results", original)
    store.put("blind_reads", independent)
    return parser, store, run, original, independent


def packet_for(parser, run, original, independent):
    return parser.retrieval_packet(
        run, 4, complement=True, role="review",
        required_regions=cited_regions(original["section"]) | cited_regions(independent["section"]),
        reserved_tokens=parser.request_reserved_tokens(run, 4, Review, (
            compact_review(original["section"], run["target"]["id"]),
            compact_review(independent["section"], run["target"]["id"]),
        )),
    )


class QualityReviewProvider(FixtureProvider):
    def __init__(self, settings, store, original):
        super().__init__(settings, store)
        self.original = original

    def execute(self, request, folder, images):
        assert request.task.startswith("review:4")
        self.requests.append(request)
        actual = request_input_metrics(request.prompt, request.schema, request.images)
        assert actual["input_tokens_estimate"] <= request.metrics["request_token_budget"]
        assert actual == request.metrics["serialized_preflight"]
        view = json.loads(request.prompt.split("ORIGINAL: ", 1)[1].split("\nINDEPENDENT: ", 1)[0])
        claims = restore_claims(view)
        packet = self.store.get("evidence_packets", f"{self.run_id}:4:review:0")
        alias = next(iter(packet["aliases"]))
        local_ids = {claim["id"] for claim in claims}
        sibling = next((claim for claim in self.original["section"]["claims"] if claim["id"] not in local_ids),
                       self.original["section"]["claims"][0])
        issues = [
            {"category": "missing_fact", "claim_id": None, "requirement_id": claims[0]["requirement_id"],
             "severity": "warning", "evidence_ids": [alias],
             "explanation": f"Missing fact: {sibling['value']}"},
            {"category": "incorrect", "claim_id": claims[0]["id"],
             "requirement_id": claims[0]["requirement_id"], "severity": "error", "evidence_ids": [alias],
             "explanation": "A distinct fixture interpretation error remains unresolved."},
        ]
        return {"supported_claim_ids": [claim["id"] for claim in claims], "issues": issues,
                "reviewed_region_ids": list(packet["aliases"])}, {"synthetic_fixture": True}


def test_quality_scoped_inventory_is_exact_routing_only_and_included_in_preflight(tmp_path):
    parser, _, run, original, independent = fixture(tmp_path)
    run["quality_version"] = "quality-v1"
    packet = packet_for(parser, run, original, independent)
    scope = [next(iter(requirements(4)))]
    left, right = (compact_review(record["section"], run["target"]["id"])
                   for record in [original, independent])
    prepared = parser._prepare_review_request(
        run, 4, original, independent, packet, scoped_view(left, scope), scoped_view(right, scope), scope,
    )
    index = json.loads(prepared["prompt"].split("ORIGINAL_PRESENCE_INDEX: ", 1)[1].split("\n", 1)[0])
    rows = restore_claims(index)
    expected = [claim for claim in original["section"]["claims"] if claim["requirement_id"] not in scope]
    assert {row["id"] for row in rows} == {claim["id"] for claim in expected}
    assert [row["value"] for row in rows] == [claim["value"] for claim in expected]
    assert all(not {"conditions", "reason", "evidence_ids"} & set(row) for row in rows)
    assert "not source evidence or a complete claim" in prepared["prompt"]
    assert "Missing fact:" in prepared["prompt"] and "Missing condition:" in prepared["prompt"]
    actual = request_input_metrics(POLICY + prepared["prompt"] + (
        f"\nOutput token target: {prepared['output_budget']}. Be concise; preserve facts and conditions."
    ), strict_schema(prepared["response_model"]), packet.images)
    assert actual == prepared["metrics"]["serialized_preflight"]
    assert prepared["metrics"]["review_presence_index_claims"] == len(expected)
    assert prepared["metrics"]["review_presence_index_tokens"] > 0
    assert actual["input_tokens_estimate"] <= 20000


def test_quality_normal_review_preserves_raw_allegations_and_does_not_create_support(tmp_path):
    parser, store, run, original, _ = fixture(tmp_path)
    run["quality_version"] = "quality-v1"
    store.put("runs", run)
    provider = QualityReviewProvider(parser.settings, store, original)
    provider.run_id = run["id"]
    parser.provider = provider
    row = parser.review_retrieved(run, 4, original)
    assert len(provider.requests) == 1
    assert row["quality_version"] == "quality-v1"
    audit = row["review_reconciliation"]
    assert len(audit["raw_review"]["issues"]) == 2
    assert len(audit["decisions"]) == 1
    assert row["issues"] == [audit["raw_review"]["issues"][1]]
    assert original["section"]["claims"][0]["id"] not in row["supported_claim_ids"]
    assert set(row["supported_claim_ids"]) <= set(audit["raw_review"]["supported_claim_ids"])
    assert store.get("reviews", row["id"]) == row
    assert parser.review_retrieved(run, 4, original) == row
    assert len(provider.requests) == 1  # Durable raw/effective review does not replay inference.


def test_quality_partition_final_reconciliation_keeps_raw_parts_and_frozen_source(tmp_path, monkeypatch):
    parser, store, run, original, independent = fixture(tmp_path)
    run["quality_version"] = "quality-v1"
    for claim in independent["section"]["claims"]:
        claim["value"] = "".join(sha256(f"{claim['id']}:{i}".encode()).hexdigest() for i in range(90))
    store.put("blind_reads", independent)
    run["retrieval_config"]["request_token_budget"] = 100000
    packet = packet_for(parser, run, original, independent)
    left, right = (compact_review(record["section"], run["target"]["id"])
                   for record in [original, independent])
    estimates = [parser._prepare_review_request(
        run, 4, original, independent, packet, scoped_view(left, [req]), scoped_view(right, [req]), [req],
    )["input_tokens_estimate"] for req in requirements(4)]
    run["retrieval_config"]["request_token_budget"] = max(estimates) + 64
    assert run["retrieval_config"]["request_token_budget"] < 20000
    store.put("runs", run)
    monkeypatch.setattr(parser, "retrieval_packet", lambda *args, **kwargs: packet)
    provider = QualityReviewProvider(parser.settings, store, original)
    provider.run_id = run["id"]
    parser.provider = provider
    row = parser.review_retrieved(run, 4, original)
    assert row["partition_count"] >= 2
    assert len(provider.requests) == row["partition_count"]
    assert all("ORIGINAL_PRESENCE_INDEX: " in request.prompt for request in provider.requests)
    assert all(packet.text in request.prompt and packet.images == request.images for request in provider.requests)
    audit = row["review_reconciliation"]
    assert len(audit["decisions"]) == row["partition_count"]
    assert all(issue["category"] == "incorrect" for issue in row["issues"])
    saved_parts = store.find("review_parts", {"run_id": run["id"]})
    assert len(saved_parts) == row["partition_count"]
    assert all(any(issue["category"] == "missing_fact" for issue in part["review"]["issues"])
               for part in saved_parts)


def test_quality_review_budget_guard_rejects_indivisible_draft_before_inference(tmp_path):
    parser, store, run, original, independent = fixture(tmp_path)
    run["quality_version"] = "quality-v1"
    original["section"]["claims"][0]["value"] = "Precise nonrepeating source detail: " + "A" * 22000
    store.put("results", original)
    store.put("blind_reads", independent)
    before = len(parser.provider.requests)
    with pytest.raises(ReviewBudgetError):
        parser.review_retrieved(run, 4, original)
    assert len(parser.provider.requests) == before
    assert not store.find("reviews", {"run_id": run["id"]})


def test_legacy_review_prompt_and_raw_issue_behavior_are_unchanged(tmp_path):
    parser, store, run, original, independent = fixture(tmp_path)
    before_original = copy.deepcopy(original)
    packet = packet_for(parser, run, original, independent)
    left, right = (compact_review(record["section"], run["target"]["id"])
                   for record in [original, independent])
    prepared = parser._prepare_review_request(run, 4, original, independent, packet, left, right, ["4.supply"])
    assert "Review quality v1" not in prepared["prompt"]
    assert "ORIGINAL_PRESENCE_INDEX:" not in prepared["prompt"]
    provider = QualityReviewProvider(parser.settings, store, original)
    provider.run_id = run["id"]
    parser.provider = provider
    row = parser.review_retrieved(run, 4, original)
    assert len(row["issues"]) == 2
    assert "review_reconciliation" not in row
    assert "quality_version" not in row
    assert original == before_original
