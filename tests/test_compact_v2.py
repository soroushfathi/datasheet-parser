"""Contract compaction must preserve source numbers, conditions and original review IDs."""

import copy
import json

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from datasheet_parser.contracts import (
    cited_regions,
    compact_review,
    expand_section,
    output_budget,
    section_contract,
)
from datasheet_parser.providers import strict_schema


def numeric(**updates):
    return {
        "requirement_id": "4.supply",
        "parameter": "VCC",
        "kind": "quantity",
        "raw_value": "3  3.3  5.5 V",
        "quantity": {
            "bounds": [
                {"role": "minimum", "value": 3},
                {"role": "typical", "value": 3.3},
                {"role": "maximum", "value": 5.5},
            ],
            "unit": "V",
            "expression": None,
        },
        "conditions": ["MAX3232C, TA = 0°C to 70°C"],
        "evidence_ids": ["page4:table3"],
        "status": "present",
        "reason": "",
        **updates,
    }


def plain(**updates):
    return {
        "requirement_id": "4.temperature",
        "parameter": "Selected grade",
        "kind": "text",
        "value": "MAX3232C: 0°C to 70°C",
        "conditions": ["Selected MAX3232CD / D package"],
        "evidence_ids": ["page4:table3"],
        "status": "present",
        "reason": "",
        **updates,
    }


def section(claims=None, **updates):
    return {
        "status": "present",
        "findings": [
            {
                "requirement_id": "4.supply",
                "status": "present",
                "evidence_ids": ["page4:table3"],
                "reason": "Applicable recommended conditions are explicit",
            }
        ],
        "claims": claims if claims is not None else [numeric(), plain()],
        "search_requests": [],
        **updates,
    }


def test_v2_strict_schema_uses_nested_anyof_and_validates_both_claim_forms():
    contract = section_contract(4, "compact-v2")
    schema = strict_schema(contract)
    Draft202012Validator(schema).validate(section())
    parsed = contract.model_validate(section())
    assert len(parsed.claims) == 2
    assert "oneOf" not in json.dumps(schema)
    assert "anyOf" in schema["properties"]["claims"]["items"]
    assert schema["type"] == "object"
    assert "anyOf" not in schema


def test_v2_expansion_preserves_raw_numbers_conditions_and_selected_target():
    final = expand_section(section(), 4, "MAX3232CD:D:C", "compact-v2")
    first, second = final.claims
    assert first.id == "4.1"
    assert first.applies_to == ["MAX3232CD:D:C"]
    assert first.raw_value == "3  3.3  5.5 V"
    assert first.value == "min=3 V; typ=3.3 V; max=5.5 V"
    assert first.quantity.model_dump() == {
        "minimum": 3,
        "typical": 3.3,
        "maximum": 5.5,
        "unit": "V",
        "expression": None,
    }
    assert first.conditions == ["MAX3232C, TA = 0°C to 70°C"]
    assert first.evidence_ids == ["page4:table3"]
    assert second.id == "4.2"
    assert second.raw_value == second.value == "MAX3232C: 0°C to 70°C"
    assert second.quantity is None
    assert final.section_id == 4
    assert final.notes == []


def test_unlabelled_number_and_symbolic_limit_are_not_guessed_into_typical():
    unknown_bound = numeric(
        raw_value="±15 kV",
        quantity={"bounds": [], "unit": "kV", "expression": "±15 kV; no min/typ/max column"},
    )
    formula = numeric(
        raw_value="−0.3 to VCC + 0.3",
        quantity={
            "bounds": [{"role": "minimum", "value": -0.3}],
            "unit": "V",
            "expression": "maximum = VCC + 0.3 V",
        },
    )
    source_only = numeric(raw_value="≤0.5 V", quantity=None)
    row = section([unknown_bound, formula, source_only])
    Draft202012Validator(strict_schema(section_contract(3, "compact-v2"))).validate(row)
    final = expand_section(row, 3, "chosen", "compact-v2")
    assert final.claims[0].quantity.typical is None
    assert final.claims[0].quantity.maximum is None
    assert final.claims[0].raw_value == "±15 kV"
    assert final.claims[1].quantity.minimum == -0.3
    assert final.claims[1].quantity.maximum is None
    assert final.claims[1].quantity.expression == "maximum = VCC + 0.3 V"
    assert final.claims[1].raw_value == "−0.3 to VCC + 0.3"
    assert final.claims[2].quantity is None
    assert final.claims[2].raw_value == final.claims[2].value == "≤0.5 V"


def test_v2_bound_repetition_rejected_instead_of_overwriting_source_number():
    row = section()
    row["claims"][0]["quantity"]["bounds"].append({"role": "minimum", "value": 4})
    with pytest.raises(ValidationError, match="only once"):
        section_contract(4, "compact-v2").model_validate(row)
    with pytest.raises(ValueError, match="Duplicate numeric bound"):
        expand_section(row, 4, "chosen", "compact-v2")


def test_v2_netlist_preserves_terminals_conditions_and_connectivity():
    circuit = {
        "id": "figure9-1",
        "figure": "Figure 9-1",
        "conditions": ["C3 returned to GND", "VCC = 3.3 V"],
        "components": [
            {
                "ref": "U1",
                "ref_origin": "generated",
                "kind": "MAX3232CD",
                "value": None,
                "terminals": ["15", "16"],
                "evidence_ids": ["p11:figure"],
            },
            {
                "ref": "CBYPASS",
                "ref_origin": "source",
                "kind": "capacitor",
                "value": "0.1 µF",
                "terminals": ["1", "2"],
                "evidence_ids": ["p11:figure"],
            },
        ],
        "nets": [
            {
                "name": "VCC",
                "nodes": ["U1.16", "CBYPASS.1"],
                "evidence_ids": ["p11:figure"],
                "status": "supported",
            },
            {
                "name": "GND",
                "nodes": ["U1.15", "CBYPASS.2"],
                "evidence_ids": ["p11:figure"],
                "status": "supported",
            },
        ],
        "ic_ref": "U1",
        "explicitly_unconnected_pads": [],
        "unobserved_pads": ["9"],
        "evidence_ids": ["p11:figure", "p11:caption"],
        "ambiguities": ["Signal pad 9 has no visible boundary wiring"],
    }
    row = section([], circuits=[circuit])
    schema = strict_schema(section_contract(2, "compact-v2"))
    Draft202012Validator(schema).validate(row)
    final = expand_section(row, 2, "chosen", "compact-v2")
    assert final.circuits[0].model_dump(mode="json") == circuit
    reviewed = compact_review(final, "chosen")
    assert reviewed["circuits"][0]["nets"] == circuit["nets"]
    assert reviewed["circuits"][0]["conditions"] == circuit["conditions"]
    assert reviewed["circuits"][0]["unobserved_pads"] == ["9"]
    assert reviewed["circuits"][0]["components"][1]["value"] == "0.1 µF"
    assert cited_regions(final) == {"page4:table3", "p11:figure", "p11:caption"}


def test_legacy_default_schema_and_expansion_remain_compatible():
    claim = {
        "requirement_id": "4.supply",
        "parameter": "VCC",
        "kind": "quantity",
        "raw_value": "3 3.3 5.5 V",
        "value": "3 to 5.5 V",
        "quantity": {
            "minimum": 3,
            "typical": 3.3,
            "maximum": 5.5,
            "unit": "V",
            "expression": None,
        },
        "conditions": ["TA = 25 C"],
        "evidence_ids": ["page4:table3"],
        "status": "present",
        "explanation": "As printed",
    }
    row = section([claim])
    Draft202012Validator(strict_schema(section_contract(4))).validate(row)
    assert strict_schema(section_contract(4)) == strict_schema(section_contract(4, "exact-v1"))
    result = expand_section(row, 4, "chosen")
    assert result.claims[0].model_dump(mode="json") == {
        **claim,
        "id": "4.1",
        "applies_to": ["chosen"],
    }
    with pytest.raises(ValueError, match="Unknown model contract"):
        section_contract(4, "future-unknown")


def test_review_retains_original_ids_status_rationale_and_distinct_lexical_text():
    result = expand_section(
        section(
            [
                numeric(),
                plain(status="ambiguous", reason="Conflicting selected-grade evidence"),
            ]
        ),
        4,
        "chosen",
        "compact-v2",
    )
    result.claims[1].raw_value = "C: 0 … 70 °C"
    result.claims[1].applies_to = ["other-grade"]
    result.claims[0].explanation = "Legacy condition: guaranteed only at the printed test load"
    before = result.model_dump(mode="json")
    reviewed = compact_review(result, "chosen")
    assert [row["id"] for row in reviewed["claims"]] == ["4.1", "4.2"]
    assert "applies_to" not in reviewed["claims"][0]
    assert "value" not in reviewed["claims"][0]
    assert reviewed["claims"][0]["raw_value"] == "3  3.3  5.5 V"
    assert reviewed["claims"][0]["quantity"]["typical"] == 3.3
    assert "printed test load" in reviewed["claims"][0]["reason"]
    assert reviewed["claims"][1]["status"] == "ambiguous"
    assert reviewed["claims"][1]["reason"] == "Conflicting selected-grade evidence"
    assert reviewed["claims"][1]["applies_to"] == ["other-grade"]
    assert reviewed["claims"][1]["raw_value"] == "C: 0 … 70 °C"
    assert reviewed["claims"][1]["value"] == "MAX3232C: 0°C to 70°C"
    assert reviewed["findings"][0]["reason"] == before["findings"][0]["reason"]
    assert result.model_dump(mode="json") == before


def test_compaction_retains_zero_and_all_claims_without_truncation():
    zero = numeric(
        raw_value="0°C",
        quantity={"bounds": [{"role": "minimum", "value": 0}], "unit": "°C", "expression": None},
    )
    result = expand_section(section([zero] * 120), 4, "chosen", "compact-v2")
    original = result.model_dump(mode="json")
    reviewed = compact_review(copy.deepcopy(original), "chosen")
    assert len(reviewed["claims"]) == 120
    assert reviewed["claims"][0]["quantity"]["minimum"] == 0
    assert len(json.dumps(reviewed)) < len(json.dumps(original))
    assert cited_regions(reviewed) == {"page4:table3"}


def test_output_budget_reflects_role_complexity_and_extra_rows():
    assert output_budget(2, "extract") > output_budget(13, "extract")
    assert output_budget(4, "blind") == output_budget(4, "extract")
    assert output_budget(4, "review") < output_budget(4, "extract")
    assert output_budget(4, "extract", rows=65) > output_budget(4, "extract")
    for sid in range(1, 18):
        assert 2500 <= output_budget(sid, "extract", rows=1000) <= 9000
        assert 1200 <= output_budget(sid, "review", rows=1000) <= 4000
    with pytest.raises(ValueError, match="Section ID"):
        output_budget(18, "extract")
    with pytest.raises(ValueError, match="Unknown section role"):
        output_budget(1, "map")


def test_columnar_review_preserves_all_fields_and_reduces_real_token_estimate():
    from datasheet_parser.contracts import columnar_review
    from datasheet_parser.token_budget import estimate_text_tokens

    original = {"section_id": 15, "status": "ambiguous", "findings": [{"reason": "source gap"}],
        "claims": [{"id": f"15-c{i}", "requirement_id": "15.footnotes", "parameter": "note",
                    "kind": "text", "value": "Keep pin 16 supply within specified limits.",
                    "conditions": ["TA=25 C"], "evidence_ids": ["e12"], "status": "present"}
                   for i in range(90)]}
    original["claims"][5]["quantity"] = {"maximum": 5.5, "unit": "V"}
    packed = columnar_review(original)
    restored = {row[table["columns"].index("id")]: dict(zip(table["columns"], row, strict=True))
                for table in packed["claim_tables"] for row in table["rows"]}
    assert restored == {c["id"]: c for c in original["claims"]}
    assert packed["findings"] == original["findings"] and packed["status"] == original["status"]
    tokens = lambda data: estimate_text_tokens(json.dumps(data, separators=(",", ":")))
    assert tokens(packed) < tokens(original) * .8


def test_shared_draft_text_roundtrips_without_touching_ids_or_citations():
    from datasheet_parser.contracts import shared_review_texts

    text = "Keep every numeric bound, qualifier, unit, footnote and variant exactly as in the supplied source. " * 4
    original = [{"claims": [{"id": "15.1", "value": text, "evidence_ids": ["e17"]}]},
                {"claims": [{"id": "15.2", "value": text, "evidence_ids": ["e19"]}]}]
    pool, packed = shared_review_texts(original)
    assert pool and packed[0]["claims"][0]["id"] == "15.1"

    def decode(value):
        if isinstance(value, dict):
            if set(value) == {"text_ref"}:
                return pool[value["text_ref"]]
            return {k: decode(v) for k, v in value.items()}
        if isinstance(value, list):
            return [decode(v) for v in value]
        return value

    assert decode(packed) == original
    assert isinstance(original[0]["claims"][0]["value"], str)
