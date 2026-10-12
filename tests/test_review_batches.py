"""Oversized review planning stays bounded without weakening requirement context."""

import copy
import json
from collections import Counter

import pytest

from datasheet_parser.errors import ReviewBudgetError
from datasheet_parser.models import Review
from datasheet_parser.review_batches import merge_review_batches, plan_review_batches

REQS = ["10.protocols", "10.address", "10.levels"]


def draft(prefix, *, empty_requirement=None):
    return {
        "section_id": 10,
        "status": "present",
        "findings": [
            {"requirement_id": req, "status": "present", "evidence_ids": ["source-table"], "reason": ""}
            for req in REQS
        ],
        "claims": [
            {
                "id": f"{prefix}-{n}",
                "requirement_id": req,
                "parameter": f"fixture-{n}",
                "value": "VIL ≤ 0.8 V at VCC = 3.3 V; mode 0",
                "quantity": {"minimum": 0, "typical": None, "maximum": 0.8, "unit": "V"},
                "conditions": ["VCC = 3.3 V", "mode 0; qualifier applies to this row only"],
                "evidence_ids": ["source-table", "source-footnote"],
                "status": "present",
            }
            for n, req in enumerate(REQS)
            if req != empty_requirement
        ],
        # The planner is serialization infrastructure, not a section validator.
        # These whole structures must never be bisected or replaced by summaries.
        "pins": [{"pad": "1", "name": "RXD", "evidence_ids": ["source-pin"]}],
        "circuits": [{"id": "figure-1", "nets": [{"nodes": ["U1.1", "R1.2"], "supported": False}]}],
        "notes": ["Unseen pads are not NC."],
    }


def prepare_factory(captured):
    def prepare(original, independent, scope):
        captured.append((copy.deepcopy(original), copy.deepcopy(independent), list(scope)))
        estimate = 500 * len(scope)
        return {
            "input_tokens_estimate": estimate,
            "prompt": "frozen source packet",
            "images": ["frozen-page-1.png", "frozen-page-2.png"],
            "metrics": {"serialized_preflight": {"input_tokens_estimate": estimate}},
            "output_budget": 1800,
        }

    return prepare


def test_requirement_batches_preserve_every_fact_and_zero_claim_finding():
    original = draft("original", empty_requirement="10.address")
    independent = draft("independent")
    before = copy.deepcopy((original, independent))
    captured = []
    planned = plan_review_batches(
        original, independent, REQS,
        prepare=prepare_factory(captured), limit=1000, section_id=10,
    )

    assert len(planned) == 2
    assert Counter(req for part in planned for req in part["scope"]) == Counter(REQS)
    assert Counter(cid for part in planned for cid in part["original_claim_ids"]) == Counter(
        c["id"] for c in original["claims"]
    )
    assert all(part["input_tokens_estimate"] <= 1000 for part in planned)
    assert all(part["prompt"] == "frozen source packet" for part in planned)
    assert all(part["images"] == ["frozen-page-1.png", "frozen-page-2.png"] for part in planned)
    assert (original, independent) == before

    # Every proposed view, including oversized candidates, keeps whole requirements
    # from BOTH drafts. An absent original claim is still audited against source.
    for oview, iview, scope in captured:
        for full, view in [(original, oview), (independent, iview)]:
            assert view["claims"] == [c for c in full["claims"] if c["requirement_id"] in scope]
            assert view["findings"] == [f for f in full["findings"] if f["requirement_id"] in scope]
            for key in ["pins", "circuits", "notes"]:
                assert view[key] == full[key]


def test_identical_inputs_produce_deterministic_partition_and_exact_limit_fits():
    original, independent = draft("original"), draft("independent")
    first = plan_review_batches(
        original, independent, REQS, prepare=prepare_factory([]), limit=1000, section_id=10,
    )
    second = plan_review_batches(
        original, independent, REQS, prepare=prepare_factory([]), limit=1000, section_id=10,
    )
    assert first == second
    assert any(part["input_tokens_estimate"] == 1000 for part in first)

    all_at_limit = plan_review_batches(
        original, independent, REQS, prepare=prepare_factory([]), limit=1500, section_id=10,
    )
    assert len(all_at_limit) == 1
    assert all_at_limit[0]["scope"] == REQS


@pytest.mark.parametrize("invalid", ["unknown_requirement", "duplicate_claim_id"])
def test_invalid_draft_cannot_silently_lose_a_claim_during_partition(invalid):
    original, independent = draft("original"), draft("independent")
    if invalid == "unknown_requirement":
        independent["claims"][0]["requirement_id"] = "10.unrecognized"
    else:
        original["claims"][1]["id"] = original["claims"][0]["id"]
    prepared = []
    with pytest.raises(ValueError):
        plan_review_batches(
            original, independent, REQS, prepare=prepare_factory(prepared), limit=1000, section_id=10,
        )
    assert prepared == []


@pytest.mark.parametrize("cause", ["one_large_claim", "whole_circuit", "fixed_source"])
def test_irreducible_input_is_rejected_without_truncating_claim_or_circuit(cause):
    original, independent = draft("original"), draft("independent")
    if cause == "one_large_claim":
        original["claims"][0]["conditions"] = ["exact printed condition " * 1000]
    if cause == "whole_circuit":
        original["circuits"][0]["conditions"] = ["unchanged source wiring condition " * 1000]
    before = copy.deepcopy((original, independent))
    visited = []

    def prepare(oview, iview, scope):
        visited.append((copy.deepcopy(oview), copy.deepcopy(iview), list(scope)))
        # Conservative offline UTF-8 bound: no provider or tokenizer download.
        source_cost = 20000 if cause == "fixed_source" else 100
        estimate = source_cost + len(json.dumps([oview, iview], ensure_ascii=False).encode("utf-8"))
        return {
            "input_tokens_estimate": estimate,
            "metrics": {"serialized_preflight": {"input_tokens_estimate": estimate}},
            "prompt": "frozen source packet", "images": ["source.png"], "output_budget": 1800,
        }

    with pytest.raises(ReviewBudgetError) as failure:
        plan_review_batches(original, independent, REQS, prepare=prepare, limit=20000, section_id=10)
    assert failure.value.section_id == 10
    assert failure.value.estimate > 20000
    assert failure.value.limit == 20000
    assert any(len(scope) == 1 for _, _, scope in visited)
    assert (original, independent) == before
    assert all(view["circuits"] == original["circuits"] for view, _, _ in visited)
    # Even an oversized singleton keeps the whole original claim, with exact qualifiers.
    for view, _, scope in visited:
        assert view["claims"] == [c for c in original["claims"] if c["requirement_id"] in scope]


def issue(category, claim_id, *, requirement="10.protocols", severity="error"):
    return {
        "category": category, "claim_id": claim_id, "requirement_id": requirement,
        "severity": severity, "evidence_ids": ["source-footnote", "source-table"],
        "explanation": "Source condition does not support the proposed interpretation.",
    }


def review(*, supported=(), issues=(), sources=("source-table",)):
    return Review.model_validate({
        "supported_claim_ids": list(supported), "issues": list(issues), "reviewed_region_ids": list(sources),
    })


@pytest.mark.parametrize("category", [
    "incorrect", "missing_condition", "wrong_target", "conflicting_sources", "insufficient_evidence",
])
def test_negative_issue_vetoes_support_even_from_another_batch(category):
    parts = [
        review(supported=["10.1", "10.2"], sources=["source-table"]),
        review(supported=["10.1"], issues=[issue(category, "10.1")], sources=["source-footnote"]),
    ]
    before = [part.model_dump() for part in parts]
    merged = merge_review_batches(parts)
    assert set(merged.supported_claim_ids) == {"10.2"}
    assert set(merged.reviewed_region_ids) == {"source-table", "source-footnote"}
    assert merged.issues[0].category == category
    assert [part.model_dump() for part in parts] == before


def test_merge_deduplicates_exact_issues_and_retains_distinct_qualifiers():
    first = issue("missing_condition", "10.1")
    distinct = {**first, "explanation": "A different source qualifier must also be checked."}
    omitted = issue("missing_fact", None, requirement="10.address", severity="warning")
    parts = [
        review(supported=["10.2"], issues=[first, omitted]),
        review(supported=["10.2"], issues=[copy.deepcopy(first), distinct, copy.deepcopy(omitted)]),
    ]
    merged = merge_review_batches(parts)
    assert merged.supported_claim_ids == ["10.2"]
    assert len(merged.issues) == 3
    assert sum(i.category == "missing_fact" for i in merged.issues) == 1
    assert merged == merge_review_batches(parts)
