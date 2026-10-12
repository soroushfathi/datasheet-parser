"""Offline integration coverage for frozen-source, resumable review partitions."""

import copy
import io
import json
from hashlib import sha256

import pytest
from PIL import Image
from test_optimized_workflow import (
    FixtureProvider,
    engine,
    no_network_provider_or_tokenizer,  # noqa: F401 -- activate imported offline fixture
    source,
)

from datasheet_parser.contracts import cited_regions, compact_review
from datasheet_parser.models import Review
from datasheet_parser.providers import ProviderError
from datasheet_parser.requirements import requirements
from datasheet_parser.review_batches import scoped_view
from datasheet_parser.token_budget import request_input_metrics


def draft_from_prompt(prompt, label, next_label):
    draft = json.loads(prompt.split(f"{label}: ", 1)[1].split(f"\n{next_label}: ", 1)[0])
    if "claim_tables" in draft:
        draft["claims"] = [dict(zip(table["columns"], row, strict=True))
                           for table in draft.pop("claim_tables") for row in table["rows"]]
    return draft


class PartitionProvider(FixtureProvider):
    def __init__(self, settings, store):
        super().__init__(settings, store)
        self.fail_on_part = None
        self.invalid = None
        self.drafts = []

    def execute(self, request, folder, images):
        assert request.task.startswith("review:4:part:")
        actual = request_input_metrics(request.prompt, request.schema, request.images)
        assert actual["input_tokens_estimate"] <= request.metrics["request_token_budget"]
        assert actual["input_tokens_estimate"] == request.metrics["serialized_preflight"]["input_tokens_estimate"]
        self.requests.append(request)
        part = int(request.task.rsplit(":", 1)[1])
        if part == self.fail_on_part:
            raise ProviderError("Offline synthetic second-part failure", code="synthetic_failure")
        original = draft_from_prompt(request.prompt, "ORIGINAL", "INDEPENDENT")
        # The independent JSON is followed by packet source, not another JSON label.
        independent = json.loads(request.prompt.split("INDEPENDENT: ", 1)[1].split("\n", 1)[0])
        if "claim_tables" in independent:
            independent["claims"] = [dict(zip(table["columns"], row, strict=True))
                                     for table in independent.pop("claim_tables") for row in table["rows"]]
        scope = json.loads(request.prompt.split("REQUIREMENTS: ", 1)[1].split("\nORIGINAL: ", 1)[0])
        self.drafts.append((scope, original, independent))
        packet = self.store.get("evidence_packets", f"{self.run_id}:4:review:0")
        response = {"supported_claim_ids": [c["id"] for c in original["claims"]],
                    "issues": [], "reviewed_region_ids": list(packet["aliases"])}
        if self.invalid == "supported_claim":
            response["supported_claim_ids"].append("outside-original-claim")
        elif self.invalid:
            issue = {"category": "incorrect", "claim_id": original["claims"][0]["id"],
                     "requirement_id": next(iter(scope)), "severity": "error",
                     "evidence_ids": [next(iter(packet["aliases"]))], "explanation": "Fixture issue"}
            if self.invalid == "requirement":
                issue["requirement_id"] = "4.outside_scope"
            elif self.invalid == "independent_claim":
                issue["claim_id"] = independent["claims"][0]["id"]
            elif self.invalid == "outside_claim":
                issue["claim_id"] = "outside-original-claim"
            response["issues"].append(issue)
        return response, {"usage": None, "synthetic_fixture": True}


def large_review_fixture(tmp_path):
    store = source(["Recommended operating conditions\nVCC typical 3.3 V", ""])
    parser, _, run_id = engine(tmp_path, store)
    run = store.get("runs", run_id)
    _, retriever = parser.retrieval_context(run)
    selected = {chunk["id"] for chunk in retriever.chunks}

    def fixture_selection(*args, **options):
        return selected, {cid: {"route": "seed", "rank_score": 10} for cid in selected}

    retriever.select = fixture_selection
    parser.map_retrieval(run)
    original = parser.extract_retrieved(run, 4)
    independent = parser.blind_retrieved(run, 4)
    assert all(request.images for request in parser.provider.requests)
    for role, record in [("original", original), ("independent", independent)]:
        templates = {c["requirement_id"]: c for c in record["section"]["claims"]}
        record["section"]["claims"] = []
        for req in requirements(4):
            for n in range(2):
                claim = copy.deepcopy(templates[req])
                claim.update(id=f"{role}:{req}:{n}", kind="text", quantity=None, raw_value=None,
                             evidence_ids=list(dict.fromkeys([*claim["evidence_ids"], "parse:p2"])),
                             value="".join(sha256(f"{role}:{req}:{n}:{i}".encode()).hexdigest()
                                           for i in range(48)))
                record["section"]["claims"].append(claim)
        record["section"]["notes"] = [f"Complete {role} global context must stay intact"]
    store.put("results", original)
    store.put("blind_reads", independent)
    original_view = compact_review(original["section"], run["target"]["id"])
    independent_view = compact_review(independent["section"], run["target"]["id"])
    run["retrieval_config"]["request_token_budget"] = 100000
    packet = parser.retrieval_packet(
        run, 4, complement=True, role="review",
        required_regions=cited_regions(original["section"]) | cited_regions(independent["section"]),
        reserved_tokens=parser.request_reserved_tokens(run, 4, Review, (original_view, independent_view)),
    )
    estimates = []
    for req in requirements(4):
        prepared = parser._prepare_review_request(
            run, 4, original, independent, packet,
            scoped_view(original_view, [req]), scoped_view(independent_view, [req]), [req],
        )
        estimates.append(prepared["input_tokens_estimate"])
    run["retrieval_config"]["request_token_budget"] = max(estimates) + 128
    store.put("runs", run)
    whole = parser._prepare_review_request(
        run, 4, original, independent, packet, original_view, independent_view,
    )
    assert whole["input_tokens_estimate"] > run["retrieval_config"]["request_token_budget"]
    provider = PartitionProvider(parser.settings, store)
    provider.run_id = run_id
    parser.provider = provider
    return parser, provider, store, run_id, original, independent, packet


def test_partitioned_review_freezes_source_and_preserves_every_requirement(tmp_path):
    parser, provider, store, run_id, original, independent, packet = large_review_fixture(tmp_path)
    parser.resume(run_id, [4])
    result = store.get("reviews", f"{run_id}:4")
    assert result["partition_count"] == len(provider.requests) == 3
    plan = store.get("review_batch_plans", result["partition_plan_id"])
    assert {req for part in plan["parts"] for req in part["scope"]} == set(requirements(4))
    assert {cid for part in plan["parts"] for cid in part["original_claim_ids"]} == {
        c["id"] for c in original["section"]["claims"]}
    assert set(result["supported_claim_ids"]) == {c["id"] for c in original["section"]["claims"]}
    assert set(result["reviewed_region_ids"]) == packet.allowed
    frozen = store.get("evidence_packets", f"{run_id}:4:review:0")
    assert frozen["aliases"] == packet.aliases
    assert frozen["chunk_ids"] == packet.chunk_ids
    assert packet.images
    original_rows = {c["id"]: c for c in original["section"]["claims"]}
    independent_rows = {c["id"]: c for c in independent["section"]["claims"]}
    for request, (scope, left, right) in zip(provider.requests, provider.drafts, strict=True):
        assert packet.text in request.prompt
        assert request.images == packet.images
        assert request.metrics["review_requirements"] == list(scope)
        for draft, expected in [(left, original_rows), (right, independent_rows)]:
            assert {row["requirement_id"] for row in draft["findings"]} == set(scope)
            assert {row["id"] for row in draft["claims"]} == {
                row["id"] for row in expected.values() if row["requirement_id"] in scope}
            assert draft["notes"]
            for row in draft["claims"]:
                assert row["value"] == expected[row["id"]]["value"]
                assert row["conditions"] == expected[row["id"]]["conditions"]
                assert set(row["evidence_ids"]) <= set(packet.aliases)
    assert all(row["review_status"] == "reviewed"
               for row in store.find("coverage", {"run_id": run_id}) if row["requirement_id"] in requirements(4))
    parser.resume(run_id, [4])
    assert len(provider.requests) == 3


def test_failed_second_part_keeps_only_verified_first_and_resume_reuses_it(tmp_path):
    parser, provider, store, run_id, *_ = large_review_fixture(tmp_path)
    provider.fail_on_part = 2
    with pytest.raises(ProviderError, match="second-part failure"):
        parser.resume(run_id, [4])
    parts = store.find("review_parts", {"run_id": run_id})
    assert len(parts) == 1 and parts[0]["part"] == 1
    assert not store.find("reviews", {"run_id": run_id})
    assert store.get("runs", run_id)["status"] == "failed"
    assert all(row["review_status"] == "pending"
               for row in store.find("coverage", {"run_id": run_id}) if row["requirement_id"] in requirements(4))
    provider.fail_on_part = None
    parser.resume(run_id, [4])
    assert [request.task for request in provider.requests] == [
        "review:4:part:1", "review:4:part:2", "review:4:part:2", "review:4:part:3"]
    assert len(store.find("review_parts", {"run_id": run_id})) == 3
    assert store.get("reviews", f"{run_id}:4")["partition_count"] == 3


def test_changed_source_image_invalidates_successful_part_cache(tmp_path):
    parser, provider, store, run_id, *_ = large_review_fixture(tmp_path)
    provider.fail_on_part = 2
    with pytest.raises(ProviderError):
        parser.resume(run_id, [4])
    first = store.find("review_parts", {"run_id": run_id})[0]
    old_packet = store.get("evidence_packets", f"{run_id}:4:review:0")
    bitmap = io.BytesIO()
    Image.new("RGB", (240, 320), "black").save(bitmap, format="PNG")
    image_id = store.artifact(bitmap.getvalue(), "image/png", "changed-source.png")
    page = store.get("pages", "parse:p2")
    store.put("pages", {**page, "image_artifact_id": image_id})
    provider.fail_on_part = None
    parser.resume(run_id, [4])
    result = store.get("reviews", f"{run_id}:4")
    new_packet = store.get("evidence_packets", f"{run_id}:4:review:0")
    assert result["partition_plan_hash"] != first["plan_hash"]
    assert new_packet["aliases"] == old_packet["aliases"]
    assert new_packet["chunk_ids"] == old_packet["chunk_ids"]
    assert [request.task for request in provider.requests] == [
        "review:4:part:1", "review:4:part:2", "review:4:part:1", "review:4:part:2", "review:4:part:3"]
    assert len(store.find("review_parts", {"run_id": run_id})) == 4


@pytest.mark.parametrize("invalid", ["supported_claim", "requirement", "independent_claim", "outside_claim"])
def test_invalid_partition_scope_or_claim_ids_never_finalize_review(tmp_path, invalid):
    parser, provider, store, run_id, *_ = large_review_fixture(tmp_path)
    provider.invalid = invalid
    with pytest.raises(ValueError, match="unknown claims|invalid requirement|invalid original claim"):
        parser.resume(run_id, [4])
    assert len(provider.requests) == 1
    assert not store.find("review_parts", {"run_id": run_id})
    assert not store.find("reviews", {"run_id": run_id})
    assert store.get("runs", run_id)["status"] == "failed"
