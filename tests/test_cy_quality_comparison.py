import copy

from datasheet_parser.cy_quality_comparison import EXPECTED_BAUD_RATES, compare_outputs

SOURCE = {
    "application_figures": list(range(3, 10)),
    "physical_pads": [str(i) for i in range(1, 29)],
    "baud_rates": EXPECTED_BAUD_RATES,
    "isb_condition": "VDD = 3.3 V; 0°C ≤ TA ≤ 55°C",
    "isbh_condition": "VDD = 3.3 V; 55°C < TA ≤ 70°C",
    "conditional_resistor": "Different sources require 1 kΩ resistors on RXD and CTS",
}


def output():
    data = {
        "sections": [
            {
                "section_id": sid,
                "status": "present",
                "findings": [],
                "claims": [],
                "pins": [],
                "circuits": [],
                "notes": [],
            }
            for sid in range(1, 18)
        ],
        "issues": [],
        "metadata": {},
        "run_id": "run",
        "parse_id": "parse",
        "status": "needs_review",
    }
    data["sections"][0]["pins"] = [
        {
            "pad": str(i),
            "name": f"PIN{i}",
            "electrical_type": "unknown",
            "functions": [],
            "conditions": [],
            "connection_requirements": [],
            "evidence_ids": ["parse:p6"],
        }
        for i in range(1, 29)
    ]
    return data


def claim(
    rid,
    parameter,
    raw_value="source fact",
    conditions=(),
    *,
    status="present",
    cid="arbitrary-id",
    kind="text",
):
    return {
        "id": cid,
        "requirement_id": rid,
        "parameter": parameter,
        "kind": kind,
        "raw_value": raw_value,
        "value": raw_value,
        "quantity": None,
        "conditions": list(conditions),
        "applies_to": ["target"],
        "evidence_ids": ["parse:p15"],
        "status": status,
        "explanation": "",
    }


def circuit(number, physical=False):
    terminals = [str(i) for i in range(1, 29)] if physical else ["VBUS", "D+", "GND_A", "WAKEUP"]
    return {
        "id": f"arbitrary-circuit-{number}",
        "figure": f"Figure {number}. Application schematic",
        "conditions": [],
        "ic_ref": "U1",
        "components": [
            {
                "ref": "U1",
                "ref_origin": "generated",
                "kind": "IC",
                "value": "CY7C64225",
                "terminals": terminals,
                "evidence_ids": [f"parse:p{number + 4}"],
            }
        ],
        "nets": [
            {
                "name": "VBUS",
                "nodes": ["U1.7" if physical else "U1.VBUS"],
                "evidence_ids": [f"parse:p{number + 4}"],
                "status": "supported",
            }
        ],
        "explicitly_unconnected_pads": [],
        "unobserved_pads": [p for p in terminals if p != "7"] if physical else [],
        "evidence_ids": [f"parse:p{number + 4}"],
        "ambiguities": [],
    }


def probe(report, name):
    return next(row for row in report["source_audited_data_probes"] if row["probe"] == name)


def test_source_probes_measure_real_changes_without_stable_ids():
    before, after = output(), output()
    before["sections"][1]["circuits"] = [circuit(3), circuit(4)]
    after["sections"][1]["circuits"] = [circuit(n, physical=True) for n in range(3, 10)]
    before["sections"][8]["claims"] = [
        claim("9.current", "ISB Sleep mode current", conditions=["0°C < TA < 55°C"], cid="old")
    ]
    after["sections"][8]["claims"] = [
        claim("9.current", "ISB Sleep mode current", conditions=["Applies when: 0°C ≤ T_A ≤ 55°C"], cid="new")
    ]
    report = compare_outputs(before, after, SOURCE)
    assert probe(report, "application_schematic_inventory")["status"] == "improved"
    assert probe(report, "application_schematic_inventory")["before"]["missing_figures"] == [5, 6, 7, 8, 9]
    assert probe(report, "physical_pad_accounting")["status"] == "improved"
    assert probe(report, "section_9_ISB_source_predicate")["status"] == "improved"
    assert report["claim_data_changes"]["changed_matched_claims"] == 1
    assert "accuracy" not in report


def test_operator_regression_is_visible_even_if_issue_count_falls():
    before, after = output(), output()
    before["sections"][8]["claims"] = [
        claim("9.current", "I_SBH sleep current", conditions=["55°C < TA ≤ 70°C"])
    ]
    after["sections"][8]["claims"] = [
        claim("9.current", "I_SBH sleep current", conditions=["55°C < TA < 70°C"])
    ]
    before["issues"] = [{"code": "unverified_claim", "severity": "warning"}]
    report = compare_outputs(before, after, SOURCE)
    assert probe(report, "section_9_ISBH_source_predicate")["status"] == "regressed"
    assert report["diagnostic_bookkeeping"]["before"]["main_issue_count"] == 1
    assert report["diagnostic_bookkeeping"]["after"]["main_issue_count"] == 0


def test_baud_list_requires_discrete_values_not_interval_or_one_endpoint():
    before, after = output(), output()
    before["sections"][11]["claims"] = [
        claim("12.clocks", "UART baud rates", "300, 600, 1200, 1800, 2400, 3600")
    ]
    after["sections"][11]["claims"] = [
        claim("12.clocks", "UART baud rates", ", ".join(map(str, EXPECTED_BAUD_RATES)))
    ]
    report = compare_outputs(before, after, SOURCE)
    row = probe(report, "section_12_complete_discrete_baud_list")
    assert row["status"] == "improved" and row["after"]["missing_printed_rates"] == []
    interval = copy.deepcopy(after)
    interval["sections"][11]["claims"][0]["raw_value"] = "300 to 230400"
    interval["sections"][11]["claims"][0]["value"] = "300 to 230400"
    row = probe(compare_outputs(after, interval, SOURCE), "section_12_complete_discrete_baud_list")
    assert row["status"] == "regressed" and len(row["after"]["missing_printed_rates"]) == 17


def test_missing_fact_overlap_is_a_manual_candidate_not_automatic_certification():
    data = output()
    data["sections"][9]["claims"] = [claim("10.levels", "USB full-speed signaling rate", "12 Mbps")]
    data["issues"] = [
        {
            "section_id": 10,
            "category": "missing_fact",
            "severity": "warning",
            "explanation": "Original omitted explicit Full-Speed USB 12 Mbps speed and its additional operating mode",
        }
    ]
    row = probe(compare_outputs(data, data, SOURCE), "already_recorded_fact_absence_allegation_candidates")
    assert row["status"] == "needs_manual"
    assert row["before"]["candidate_count"] == 1
    assert row["before"]["examples"][0]["disposition"] == "candidate_false_absence_requires_manual_review"


def test_quantity_false_warning_and_linked_review_changes_are_diagnostic_only():
    before, after = output(), output()
    before["sections"][2]["claims"] = [
        claim(
            "3.currents_power",
            "absolute current limit",
            None,
            status="not_found",
            kind="quantity",
            cid="absent",
        )
    ]
    after["sections"][2]["claims"] = copy.deepcopy(before["sections"][2]["claims"])
    before["issues"] = [
        {"code": "missing_quantity", "claim_id": "absent", "severity": "error"},
        {"section_id": 3, "category": "incorrect", "severity": "error", "explanation": "wrong"},
    ]
    after["issues"] = [before["issues"][1]]
    after["metadata"] = {
        "quality_audit": {"linked_review_diagnostics": [{"claim_id": "absent"}]},
        "review_reconciliation": {
            "3": {
                "raw_review": {"issues": [before["issues"][1], {"category": "missing_fact"}]},
                "decisions": [{"disposition": "already_recorded"}],
            }
        },
    }
    report = compare_outputs(before, after, SOURCE)
    assert probe(report, "absent_quantity_false_validator_warnings")["status"] == "improved"
    assert report["claim_data_changes"]["changed_matched_claims"] == 0
    stats = report["diagnostic_bookkeeping"]["after"]
    assert stats["raw_review_count"] == 2 and stats["model_review_count"] == 1
    assert stats["linked_diagnostic_count"] == stats["reconciled_allegation_count"] == 1


def test_scope_contamination_probe_preserves_facts_in_other_owners():
    before, after = output(), output()
    before["sections"][6]["claims"] = [claim("7.dimensions", "Thermal impedance θJA", "96 °C/W")]
    after["sections"][10]["claims"] = [claim("11.metrics", "Thermal impedance θJA", "96 °C/W")]
    row = probe(compare_outputs(before, after, SOURCE), "high_confidence_requirement_scope_contamination")
    assert row["status"] == "improved"
    assert row["before"]["high_confidence_misplacement_count"] == 1
    assert row["after"]["high_confidence_misplacement_count"] == 0


def test_resistor_context_is_not_conjunctive_validity_predicate():
    before, after = output(), output()
    before["sections"][9]["claims"] = [
        claim(
            "10.protocols",
            "RXD and CTS series resistors",
            "1kΩ resistors required",
            conditions=["both devices powered from VBUS", "when devices powered from different sources"],
        )
    ]
    after["sections"][9]["claims"] = [
        claim(
            "10.protocols",
            "RXD and CTS series resistors",
            "1kΩ resistors required",
            conditions=[
                "Scenario: both devices powered from VBUS",
                "Applies when: devices powered from different sources",
            ],
        )
    ]
    row = probe(compare_outputs(before, after, SOURCE), "RXD_CTS_1k_scenario_and_validity_predicate")
    assert row["status"] == "improved"
    assert row["after"]["clearly_scoped_claims"] == 1


def test_unknown_pin_alias_prevents_structural_certification_even_when_all_pads_accounted():
    data = output()
    data["sections"][1]["circuits"] = [circuit(3, physical=True)]
    data["sections"][1]["circuits"][0]["components"][0]["terminals"].append("WAKEUP")
    row = probe(compare_outputs(data, data, SOURCE), "physical_pad_accounting")
    assert row["after"]["structurally_accounted_circuits"] == 0
    assert row["after"]["circuits"][0]["unknown_ic_terminals"] == ["WAKEUP"]


def test_long_example_excerpt_does_not_hide_a_data_change_after_excerpt_boundary():
    before, after = output(), output()
    before["sections"][8]["claims"] = [claim("9.current", "source condition", "a" * 700 + "<")]
    after["sections"][8]["claims"] = [claim("9.current", "source condition", "a" * 700 + "≤", cid="new")]
    original = copy.deepcopy([before, after])
    report = compare_outputs(before, after, SOURCE)
    assert [before, after] == original
    assert report["claim_data_changes"]["changed_matched_claims"] == 1
    assert "raw_value" in report["claim_data_changes"]["changed_examples"][0]["before"]["excerpted_fields"]


def test_baud_list_owned_by_protocol_section_need_not_be_duplicated_under_timing():
    before, after = output(), output()
    values = ", ".join(map(str, EXPECTED_BAUD_RATES))
    before["sections"][11]["claims"] = [claim("12.clocks", "UART baud rates", values)]
    after["sections"][9]["claims"] = [claim("10.protocols", "UART supported baud rates", values)]
    nominal = claim("12.clocks", "Integrated oscillator frequency", "24 MHz")
    nominal["quantity"] = {
        "minimum": None,
        "typical": None,
        "maximum": None,
        "unit": "MHz",
        "expression": "24",
    }
    after["sections"][11]["claims"] = [nominal]
    report = compare_outputs(before, after, SOURCE)
    row = probe(report, "section_12_complete_discrete_baud_list")
    assert row["status"] == "unchanged"
    assert row["local_copy_change_informational_only"] == "regressed"
    assert row["after"]["local_copy_is_required"] is False
    assert row["after"]["complete_owned_list_available"] is True
    assert row["after"]["nominal_versus_limit_distinction_recorded"] is True
    assert probe(report, "section_10_complete_discrete_baud_list")["status"] == "improved"


def test_nominal_frequency_is_not_a_guaranteed_minimum_or_maximum():
    data = output()
    raw = claim("12.clocks", "Oscillator frequency", "24 MHz")
    raw["quantity"] = {"minimum": 24, "typical": None, "maximum": 24, "unit": "MHz", "expression": None}
    data["sections"][11]["claims"] = [raw]
    row = probe(compare_outputs(data, data, SOURCE), "section_12_complete_discrete_baud_list")
    assert row["after"]["nominal_versus_limit_distinction_recorded"] is False
    assert len(row["after"]["frequency_bound_claims_needing_source_check"]) == 1


def test_numeric_formatting_issues_are_separate_from_real_alias_and_name_disagreement():
    data = output()
    data["sections"][1]["circuits"] = [circuit(3)]
    data["sections"][1]["circuits"][0]["components"][0]["terminals"] = [
        "16(VDD)",
        "16(GND)",
        "WAKEUP",
        "GND_A",
        "VBUS",
        "99 VDD",
    ]
    source = {
        **SOURCE,
        "pin_names": {"1": "GND", "13": "GND", "7": "VBUS", "16": "VDD", "28": "VDD", "22": "WAKE"},
    }
    row = probe(compare_outputs(data, data, source), "physical_pad_accounting")
    counts = row["after"]["unknown_pin_token_counts"]
    assert counts["numeric_format_candidate"] == 1
    assert counts["explicit_pad_name_disagreement"] == 1
    assert counts["unknown_numeric_pad"] == 1
    assert counts["unique_literal_name_format_candidate"] == 1
    assert counts["repeated_pin_occurrence_label_needs_graph_context"] == 1
    assert counts["unresolved_symbolic_or_prose_label"] == 1


def test_nc_singleton_marker_warning_is_manual_bookkeeping_candidate_only():
    data = output()
    c = circuit(3, physical=True)
    c["explicitly_unconnected_pads"] = ["6"]
    c["unobserved_pads"].remove("6")
    c["nets"].append(
        {
            "name": "NO_CONNECT",
            "nodes": ["U1.6", "schematic label NO CONNECT"],
            "evidence_ids": ["parse:p7"],
            "status": "supported",
        }
    )
    data["sections"][1]["circuits"] = [c]
    data["issues"] = [{"code": "nc_connected", "message": "6", "severity": "error"}]
    report = compare_outputs(data, data, SOURCE)
    candidate = report["diagnostic_bookkeeping"]["after"]["nc_marker_warning_candidates"]
    assert candidate["nc_connected_warning_count"] == candidate["warning_candidate_upper_bound"] == 1
    assert (
        candidate["examples"][0]["disposition"]
        == "candidate_NC_marker_or_singleton_stub_requires_manual_source_review"
    )
    assert report["diagnostic_bookkeeping"]["after"]["main_issue_count"] == 1
    c["nets"][-1]["nodes"].append("R1.1")
    report = compare_outputs(data, data, SOURCE)
    assert (
        report["diagnostic_bookkeeping"]["after"]["nc_marker_warning_candidates"][
            "warning_candidate_upper_bound"
        ]
        == 0
    )
