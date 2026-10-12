import pytest

from datasheet_parser.models import Claim, SectionResult
from datasheet_parser.quality_policy import (
    ownership_diagnostics,
    quality_instructions,
    requirement_coverage,
    semantic_postprocess,
)
from datasheet_parser.requirements import requirements


def claim(
    requirement_id,
    parameter,
    value="source fact",
    *,
    status="present",
    quantity=None,
    conditions=(),
    evidence_ids=("parse:p1",),
    kind="text",
):
    return Claim.model_validate(
        {
            "id": requirement_id + "-claim",
            "requirement_id": requirement_id,
            "parameter": parameter,
            "kind": kind,
            "raw_value": value,
            "value": value,
            "quantity": quantity,
            "conditions": list(conditions),
            "applies_to": ["target"],
            "evidence_ids": list(evidence_ids),
            "status": status,
            "explanation": "Inspected source did not state this subfield" if status == "not_found" else "",
        }
    )


def section(section_id, claims=(), finding_status="present"):
    return SectionResult.model_validate(
        {
            "section_id": section_id,
            "status": "present",
            "claims": list(claims),
            "pins": [],
            "circuits": [],
            "notes": [],
            "findings": [
                {
                    "requirement_id": rid,
                    "status": finding_status,
                    "evidence_ids": ["parse:p1"],
                    "reason": "Source inspected",
                }
                for rid in requirements(section_id)
            ],
        }
    )


def quantity(*, minimum=None, typical=None, maximum=None, unit="MHz", expression=None):
    return {
        "minimum": minimum,
        "typical": typical,
        "maximum": maximum,
        "unit": unit,
        "expression": expression,
    }


@pytest.mark.parametrize("sid", range(1, 18))
def test_each_section_policy_requires_source_math_predicates_and_explicit_subfield_coverage(sid):
    text = quality_instructions(sid)
    assert "< versus ≤" in text and "Scenario:" in text and "Applies when:" in text
    assert "partial coverage" in text and "unseen subfield" in text
    assert all(rid in text for rid in requirements(sid))


@pytest.mark.parametrize("sid", [0, 18])
def test_invalid_policy_section_rejected(sid):
    with pytest.raises(ValueError):
        quality_instructions(sid)


def test_nominal_oscillator_is_not_frequency_bounds():
    source = section(
        12,
        [
            claim(
                "12.clocks",
                "integrated oscillator frequency",
                "24 MHz",
                quantity=quantity(expression="24"),
                conditions=["Applies when: used as reference clock for SIE"],
                kind="quantity",
            )
        ],
    )
    row = next(r for r in requirement_coverage(source) if r["requirement_id"] == "12.clocks")
    fields = {field["field"]: field["status"] for field in row["fields"]}
    assert fields["nominal_frequency"] == "present"
    assert fields["frequency_limits"] == "check_needed"
    assert row["coverage"] == "partial"
    assert row["basis"] == "extracted_claims_not_verified_source_completeness"


def test_explicit_clock_bounds_count_without_rewriting_nominal_clock():
    source = section(
        12,
        [
            claim(
                "12.clocks",
                "clock frequency limit",
                "12 to 24 MHz",
                quantity=quantity(minimum=12, maximum=24),
                kind="quantity",
            )
        ],
    )
    row = next(r for r in requirement_coverage(source) if r["requirement_id"] == "12.clocks")
    assert next(f for f in row["fields"] if f["field"] == "frequency_limits")["status"] == "present"


def test_baud_rate_maximum_is_not_clock_frequency_limit():
    source = section(
        12,
        [
            claim(
                "12.clocks",
                "maximum baud rate",
                "3 Mbps",
                quantity=quantity(maximum=3, unit="Mbps"),
                kind="quantity",
            )
        ],
    )
    row = next(r for r in requirement_coverage(source) if r["requirement_id"] == "12.clocks")
    assert next(f for f in row["fields"] if f["field"] == "frequency_limits")["status"] == "check_needed"


def test_theta_ja_does_not_certify_theta_jc_or_psi_and_test_board_remains_context():
    source = section(
        11,
        [
            claim(
                "11.metrics",
                "Typical θJA",
                "96 °C/W",
                quantity=quantity(typical=96, unit="°C/W"),
                conditions=["Applies when: Assumes 2-layer PCB"],
                kind="quantity",
            )
        ],
    )
    row = next(r for r in requirement_coverage(source) if r["requirement_id"] == "11.metrics")
    fields = {field["field"]: field["status"] for field in row["fields"]}
    assert fields["theta_ja"] == fields["test_board"] == "present"
    assert fields["theta_jc"] == fields["psi_jt"] == "check_needed"
    layout = next(r for r in requirement_coverage(source) if r["requirement_id"] == "11.layout")
    assert layout["coverage"] == "check_needed"


def test_nominal_component_value_does_not_imply_tolerance_or_rating():
    source = section(
        6,
        [
            claim(
                "6.types",
                "LED series resistor",
                "560 Ω",
                quantity=quantity(unit="Ω", expression="560"),
                kind="quantity",
            )
        ],
    )
    row = next(r for r in requirement_coverage(source) if r["requirement_id"] == "6.types")
    assert row["coverage"] == "check_needed"
    assert all(field["status"] == "check_needed" for field in row["fields"])


def test_only_explicit_subfield_absence_is_reported_absent():
    source = section(
        6, [claim("6.types", "external capacitor ESR", None, status="not_found", evidence_ids=())]
    )
    row = next(r for r in requirement_coverage(source) if r["requirement_id"] == "6.types")
    fields = {field["field"]: field["status"] for field in row["fields"]}
    assert fields["esr"] == "reported_absent"
    assert fields["tolerance"] == fields["ratings"] == "check_needed"


def test_related_sibling_fact_is_metadata_without_falsely_filling_requirement():
    source = section(
        10,
        [claim("10.protocols", "configuration default", "Self-powered by default")],
        finding_status="not_found",
    )
    row = next(r for r in requirement_coverage(source) if r["requirement_id"] == "10.address")
    assert row["related_claim_ids"] == ["10.protocols-claim"]
    assert row["coverage"] == "reported_absent"
    assert all(field["status"] == "check_needed" for field in row["fields"])


@pytest.mark.parametrize(
    "sid,rid,parameter,value,destination",
    [
        (7, "7.dimensions", "Thermal impedance θJA", "96 °C/W", "11.metrics"),
        (6, "6.types", "Thermal impedance θJC", "12 °C/W", "11.metrics"),
        (8, "8.ground", "Typical θJA", "96 °C/W", "11.metrics"),
        (7, "7.dimensions", "Solder reflow peak temperature", "260 °C", "17.reflow"),
        (11, "11.metrics", "Time at maximum solder reflow peak temperature", "20 s", "17.reflow"),
        (13, "13.behavior", "VDD supply voltage", "3.0 to 5.25 V", "4.supply"),
        (
            8,
            "8.routing",
            "D+ application connection",
            "D+ line connects USB connector to IC through resistor",
            "2.connections",
        ),
        (8, "8.ground", "GND connection", "Pins tied to ground symbol", "2.connections"),
        (8, "8.keepout", "Package overall width", "7.5 to 8.1 mm", "7.dimensions"),
    ],
)
def test_narrow_wrong_ownership_is_flagged_with_original_citations(sid, rid, parameter, value, destination):
    source = section(sid, [claim(rid, parameter, value)])
    issues = ownership_diagnostics(source)
    assert len(issues) == 1
    assert issues[0]["suggested_requirement_id"] == destination
    assert issues[0]["evidence_ids"] == ["parse:p1"]
    assert issues[0]["action"] == "guard_only_original_claim_preserved"


@pytest.mark.parametrize(
    "sid,rid,parameter,value",
    [
        (13, "13.behavior", "VDD supply voltage", "UVLO threshold is 2.8 V with recovery at 3 V"),
        (13, "13.behavior", "USB hardware nonfunctional VDD range", "3.5 to 4.35 V"),
        (8, "8.routing", "D+ routing", "Route D+ trace with controlled differential impedance"),
        (8, "8.ground", "Return path", "Keep ground-plane return path beneath connection"),
        (8, "8.keepout", "Package placement", "Leave keep-out clearance 2 mm around package"),
        (11, "11.metrics", "Thermal impedance θJA", "96 °C/W"),
        (17, "17.reflow", "Solder reflow peak temperature", "260 °C"),
    ],
)
def test_guards_do_not_reclassify_uncertain_or_correctly_owned_facts(sid, rid, parameter, value):
    assert ownership_diagnostics(section(sid, [claim(rid, parameter, value)])) == []


def test_guard_only_postprocess_preserves_every_fact_field_and_source_operator():
    source = section(
        7,
        [
            claim(
                "7.dimensions",
                "Thermal impedance θJA",
                "96 °C/W",
                conditions=["Scenario: Figure 4", "Applies when: 0°C ≤ TA ≤ 55°C"],
                quantity=quantity(typical=96, unit="°C/W"),
                kind="quantity",
            )
        ],
    )
    before = source.model_dump(mode="json")
    result = semantic_postprocess([source])
    assert source.model_dump(mode="json") == result.sections[0].model_dump(mode="json") == before
    assert result.sections[0] is not source
    assert result.diagnostics[0]["suggested_requirement_id"] == "11.metrics"
    assert result.provenance[0]["action"] == "semantic_guards_only_no_fact_values_or_ownership_changed"
    assert not any(row["requirement_id"] == "11.metrics" for row in result.coverage)


def test_all_51_requirements_have_explicit_noncertifying_coverage_metadata():
    result = semantic_postprocess([section(sid) for sid in range(1, 18)])
    assert len(result.coverage) == 51
    assert {row["requirement_id"] for row in result.coverage} == set(requirements())
    assert all(row["coverage"] == "check_needed" for row in result.coverage)
    assert all(row["fields"] and "not_verified_source" in row["basis"] for row in result.coverage)


def test_uncited_positive_claim_never_counts_as_present_coverage():
    row = next(
        r
        for r in requirement_coverage(section(11, [claim("11.metrics", "θJA", "96 °C/W", evidence_ids=())]))
        if r["requirement_id"] == "11.metrics"
    )
    assert next(f for f in row["fields"] if f["field"] == "theta_ja")["status"] == "check_needed"
