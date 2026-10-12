"""A deliberate absent quantity has no value; a present quantity must be structured."""

import pytest

from datasheet_parser.models import SectionResult
from datasheet_parser.requirements import requirements
from datasheet_parser.validation import validate_section


def section(status, quantity=None):
    req_ids = list(requirements(3))
    return SectionResult.model_validate({
        "section_id": 3, "status": status,
        "findings": [{"requirement_id": req, "status": status, "evidence_ids": ["p1"],
                      "reason": "No numerical value is available in the supplied source."} for req in req_ids],
        "claims": [{"id": "3.1", "requirement_id": req_ids[0], "parameter": "Voltage",
                    "kind": "quantity", "raw_value": None, "value": None, "quantity": quantity,
                    "conditions": [], "applies_to": ["target"], "evidence_ids": ["p1"],
                    "status": status, "explanation": "No numerical value is available in the supplied source."}],
        "pins": [], "circuits": [], "notes": [],
    })


@pytest.mark.parametrize("status", ["not_found", "not_applicable", "ambiguous", "conflicting", "unreadable"])
def test_missing_quantity_is_not_reported_for_non_present_disposition(status):
    codes = {issue["code"] for issue in validate_section(section(status), "target", {"p1"})}
    assert "missing_quantity" not in codes
    if status in {"ambiguous", "conflicting", "unreadable"}:
        assert "unresolved_claim" in codes


def test_present_quantity_still_requires_structured_numeric_or_symbolic_value():
    issues = validate_section(section("present"), "target", {"p1"})
    assert "missing_quantity" in {issue["code"] for issue in issues}


def test_absent_claim_with_supplied_invalid_quantity_is_still_flagged():
    quantity = {"minimum": 5, "typical": None, "maximum": 4, "unit": "V", "expression": None}
    codes = {issue["code"] for issue in validate_section(section("not_found", quantity), "target", {"p1"})}
    assert "inverted_bounds" in codes
