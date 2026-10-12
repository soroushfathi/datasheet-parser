"""False absence checks are exact, bounded-context routing, never source certification."""

from copy import deepcopy

import pytest

from datasheet_parser.models import Review
from datasheet_parser.review_context import (
    detailed_review_claim_ids,
    original_inventory,
    presence_inventory,
    reconcile_review,
)


def original():
    return {"claims": [
        {"id": "10.1", "requirement_id": "10.protocols", "parameter": "UART baud rate",
         "status": "present", "value": "300 to 3 Mbps", "conditions": ["VDD = 3.3 V"],
         "evidence_ids": ["source-1"], "reason": "Check every supported baud code."},
        {"id": "10.40", "requirement_id": "10.levels", "parameter": "Full-Speed USB bus speed",
         "status": "present", "raw_value": "12 Mbps", "quantity": {"typical": 12, "unit": "Mbps"},
         "conditions": ["0°C ≤ TA ≤ 55°C", "VDD = 3.3 V"], "evidence_ids": ["source-2"]},
        {"id": "10.23", "requirement_id": "10.address", "parameter": "Bus/Self Powered default",
         "status": "present", "value": "Self", "conditions": [], "evidence_ids": ["source-3"]},
    ]}


def review(category="missing_fact", explanation="Missing fact: 12 Mbps", *, cid=None, source="source-2"):
    return Review.model_validate({"supported_claim_ids": [], "reviewed_region_ids": [source], "issues": [
        {"category": category, "explanation": explanation, "claim_id": cid,
         "requirement_id": "10.protocols", "severity": "warning", "evidence_ids": [source]},
    ]})


def restore_inventory(index):
    return [dict(zip(table["columns"], row, strict=True))
            for table in index["claim_tables"] for row in table["rows"]]


def test_lossless_sibling_inventory_preserves_all_fact_fields_without_mutating_original():
    data = original()
    before = deepcopy(data)
    inventory = original_inventory(data, ["10.protocols"])
    assert inventory["claim_count"] == 2
    assert restore_inventory(inventory) == data["claims"][1:]
    assert data == before


def test_presence_index_preserves_values_but_explicitly_is_not_a_full_claim():
    data = original()
    before = deepcopy(data)
    index = presence_inventory(data, ["10.protocols"])
    claims = restore_inventory(index)
    assert claims[0]["id"] == "10.40"
    assert claims[0]["raw_value"] == "12 Mbps"
    assert claims[0]["quantity"] == {"typical": 12, "unit": "Mbps"}
    assert claims[1]["value"] == "Self"
    assert all(not {"conditions", "reason", "evidence_ids"} & set(claim) for claim in claims)
    assert data == before


def test_exact_whole_section_fact_existence_reconciles_false_omission_across_requirements():
    raw = review()
    before = raw.model_dump(mode="json")
    effective, audit = reconcile_review(raw, original())
    assert effective.issues == []
    assert effective.supported_claim_ids == []  # Exact presence is not positive source support.
    assert audit["raw_review"] == before
    match = audit["decisions"][0]["matches"][0]
    assert match["claim_id"] == "10.40"
    assert match["requirement_id"] == "10.levels"
    assert match["field"] == "raw_value"
    assert raw.model_dump(mode="json") == before


@pytest.mark.parametrize("explanation", [
    "Missing fact: 12 mbps",  # Scientific units and case remain significant.
    "Missing fact: 12 Gbps",
    "Missing fact: 1.2 Mbps",
    "Original omits the explicit bus speed shown as 12 Mbps.",
    "Missing fact: 12 Mbps and Remote Wake-Up default Enabled",
    "Missing fact: 12 Mbps; Missing fact: Self",
    "The value '12 Mbps' is incorrect for this condition.",
])
def test_partial_overlaps_or_prose_are_not_suppressed(explanation):
    raw = review(explanation=explanation)
    effective, audit = reconcile_review(raw, original())
    assert effective.issues == raw.issues
    assert audit["decisions"] == []


@pytest.mark.parametrize("status", ["not_found", "not_applicable", "ambiguous", "unreadable", "conflicting"])
def test_absent_or_unresolved_original_does_not_disprove_missing_fact(status):
    data = original()
    data["claims"][1]["status"] = status
    effective, audit = reconcile_review(review(), data)
    assert len(effective.issues) == 1
    assert audit["decisions"] == []


def test_value_from_different_source_is_not_enough_to_dismiss_omission():
    raw = review(source="different-row")
    effective, audit = reconcile_review(raw, original())
    assert effective.issues == raw.issues
    assert not audit["decisions"]


def test_exact_qualified_condition_match_requires_same_claim_and_overlapping_source():
    raw = review("missing_condition", "Missing condition: 0°C ≤ TA ≤ 55°C", cid="10.40")
    effective, audit = reconcile_review(raw, original())
    assert not effective.issues
    assert audit["decisions"][0]["matches"][0]["field"] == "conditions[0]"

    wrong_claim = review("missing_condition", "Missing condition: VDD = 3.3 V", cid="other-claim")
    assert reconcile_review(wrong_claim, original())[0].issues == wrong_claim.issues


@pytest.mark.parametrize("condition", ["0°C < TA < 55°C", "0°C ≤ TA ≤ 85°C", "VDD = -3.3 V", "VDD = 3.3 mV"])
def test_condition_comparisons_keep_operators_numbers_signs_and_units(condition):
    raw = review("missing_condition", f"Missing condition: {condition}", cid="10.40")
    assert reconcile_review(raw, original())[0].issues == raw.issues


def test_existence_check_accepts_only_typography_whitespace_and_explicit_complete_field():
    raw = review(explanation='Missing fact: "Full-Speed USB bus speed:\u00a0 12   Mbps"')
    effective, audit = reconcile_review(raw, original())
    assert not effective.issues
    assert audit["decisions"][0]["matches"][0]["field"] == "parameter+raw_value"


def test_distinct_negative_finding_vetoes_existing_raw_positive_without_creating_any():
    raw = review()
    wrong = review("wrong_target", "Missing fact: 12 Mbps", cid="10.40").issues[0]
    raw = raw.model_copy(update={"supported_claim_ids": ["10.1", "10.40"], "issues": [*raw.issues, wrong]})
    effective, audit = reconcile_review(raw, original())
    assert effective.supported_claim_ids == ["10.1"]
    assert effective.issues == [wrong]
    assert len(audit["raw_review"]["issues"]) == 2
    assert detailed_review_claim_ids(effective) == {"10.40"}


def test_same_parameter_different_default_not_reconciled():
    raw = review(explanation="Missing fact: Bus/Self Powered default: Bus", source="source-3")
    assert reconcile_review(raw, original())[0].issues == raw.issues


def test_scientific_compatibility_characters_are_not_equated_with_plain_symbols():
    data = original()
    data["claims"][1]["raw_value"] = "3 m²"
    raw = review(explanation="Missing fact: 3 m2")
    assert reconcile_review(raw, data)[0].issues == raw.issues


def test_export_grouping_accepts_persisted_review_row_without_hiding_detailed_issue():
    row = {"id": "run:10", "section_id": 10, "run_id": "run", **review(cid="10.40").model_dump(mode="json")}
    before = deepcopy(row)
    assert detailed_review_claim_ids(row) == {"10.40"}
    assert row == before
