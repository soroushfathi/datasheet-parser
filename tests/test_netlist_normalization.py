import json
from pathlib import Path

import pytest

from datasheet_parser.models import SectionResult
from datasheet_parser.netlist_normalization import (
    NORMALIZATION_VERSION,
    PinAlias,
    normalize_circuit_pads,
    source_pin_inventory,
)
from datasheet_parser.validation import validate_cross_sections


def sections(pin_rows, terminals, nets=None, nc=(), unobserved=()):
    pin_section = SectionResult.model_validate({
        "section_id": 1, "status": "present", "claims": [], "findings": [], "circuits": [], "notes": [],
        "pins": [{"pad": str(pad), "name": name, "electrical_type": "unknown", "functions": [],
                  "conditions": [], "connection_requirements": [], "evidence_ids": ["parse:p1:t1"]}
                 for pad, name in pin_rows],
    })
    circuit_section = SectionResult.model_validate({
        "section_id": 2, "status": "present", "claims": [], "findings": [], "pins": [], "notes": [],
        "circuits": [{"id": "fig1", "figure": "Figure 1", "conditions": [], "ic_ref": "U1",
                      "components": [{"ref": "U1", "ref_origin": "generated", "kind": "IC", "value": "TEST",
                                      "terminals": terminals, "evidence_ids": ["parse:p2"]}],
                      "nets": nets or [], "explicitly_unconnected_pads": list(nc),
                      "unobserved_pads": list(unobserved), "evidence_ids": ["parse:p2"], "ambiguities": []}],
    })
    return pin_section, circuit_section


def net(name, *labels, status="supported"):
    return {"name": name, "nodes": ["U1." + label for label in labels],
            "evidence_ids": ["parse:p2"], "status": status}


def test_unique_labels_preserve_other_components_and_remove_disposition_prefix():
    pins, circuits = sections([(1, "TxD"), (2, "D–"), (3, "RESET"), (4, "RxD")],
                             ["txd", "D-", "RESET"], [net("USB", "D-")],
                             nc=["U1.RESET"], unobserved=["U1.txd"])
    circuits.circuits[0].nets[0].nodes.append("J1.D-")
    before = circuits.model_dump()
    result = normalize_circuit_pads(circuits, pins)
    circuit = result.section.circuits[0]
    assert circuits.model_dump() == before
    assert circuit.nets[0].nodes == ["U1.2", "J1.D-"]
    assert circuit.explicitly_unconnected_pads == ["3"]
    assert circuit.unobserved_pads == ["1", "4"]
    assert circuit.components[0].terminals == ["1", "2", "3", "4"]
    assert not result.diagnostics
    assert not validate_cross_sections([pins, result.section])


def test_unknown_alias_is_not_guessed_and_package_nc_is_only_unobserved():
    pins, circuits = sections([(1, "WAKE"), (2, "NC")], ["WAKEUP"], [net("WAKE", "WAKEUP")])
    result = normalize_circuit_pads(circuits, pins)
    circuit = result.section.circuits[0]
    assert "U1.WAKEUP" in circuit.nets[0].nodes
    assert circuit.explicitly_unconnected_pads == []
    assert circuit.unobserved_pads == ["1", "2"]
    assert {d["code"] for d in result.diagnostics} == {"unresolved_pin_label"}
    assert {i["code"] for i in validate_cross_sections([pins, result.section])} == {
        "unknown_pad_disposition", "package_pin_mismatch",
    }


def test_semantic_alias_requires_explicit_source_proof():
    pins, circuits = sections([(22, "WAKE")], ["WAKEUP"], [net("WAKE", "WAKEUP")])
    alias = PinAlias("WAKEUP", "22", ("parse:p2:pin22",), "Schematic labels pad 22 WAKEUP")
    result = normalize_circuit_pads(circuits, pins, aliases=(alias,))
    assert result.section.circuits[0].nets[0].nodes == ["U1.22"]
    assert result.provenance[0]["strategy"] == "explicit_source_alias"
    assert "parse:p2:pin22" in result.provenance[0]["evidence_ids"]
    assert not result.diagnostics


@pytest.mark.parametrize("alias", [
    PinAlias("WAKEUP", "22", (), "No evidence"),
    PinAlias("WAKEUP", "99", ("parse:p2",), "Wrong physical pad"),
    PinAlias("WAKEUP", "22", ("parse:p2",), ""),
])
def test_unproven_or_invalid_alias_rejected(alias):
    pins, circuits = sections([(22, "WAKE")], ["WAKEUP"])
    with pytest.raises(ValueError):
        normalize_circuit_pads(circuits, pins, aliases=(alias,))


def test_complete_repeated_group_on_same_net_has_no_individual_pad_assignment():
    pins, circuits = sections([(1, "GND"), (13, "GND"), (16, "VDD"), (28, "VDD")],
                             ["GND_A", "GND_B", "VDD_A", "VDD_B"],
                             [net("GND", "GND_A", "GND_B"), net("VDD", "VDD_A", "VDD_B")])
    result = normalize_circuit_pads(circuits, pins)
    assert result.section.circuits[0].nets[0].nodes == ["U1.1", "U1.13"]
    assert result.section.circuits[0].nets[1].nodes == ["U1.16", "U1.28"]
    assert all(p["strategy"] in {"complete_same_net_pin_set", "pin_inventory_basis"} for p in result.provenance)
    assert result.provenance[0]["labels"] == ["GND_A", "GND_B"]
    assert result.provenance[0]["pads"] == ["1", "13"]
    assert not result.diagnostics
    assert not validate_cross_sections([pins, result.section])


@pytest.mark.parametrize("terminals,nets", [
    (["GND_A"], [net("GND", "GND_A")]),
    (["GND"], [net("GND", "GND")]),
    (["GND_A", "GND_B"], [net("AGND", "GND_A"), net("DGND", "GND_B")]),
    (["GND_A", "GND_B"], [net("GND", "GND_A", "GND_B", status="ambiguous")]),
    (["GND_A", "GND_B"], [net("GND", "GND_A", "GND_B"), net("OTHER", "GND_A", "GND_B")]),
    (["1", "GND_A", "GND_B"], [net("GND", "1", "GND_A", "GND_B")]),
])
def test_repeated_group_insufficient_or_conflicting_observation_stays_ambiguous(terminals, nets):
    pins, circuits = sections([(1, "GND"), (13, "GND")], terminals, nets)
    result = normalize_circuit_pads(circuits, pins)
    assert any(d["code"] == "ambiguous_repeated_pin" for d in result.diagnostics)
    assert result.section.circuits[0].nets[0].nodes == nets[0]["nodes"]
    assert not any(p["strategy"] == "complete_same_net_pin_set" for p in result.provenance)


def test_unique_name_conflict_is_exposed_not_silently_removed():
    pins, circuits = sections([(7, "VBUS")], ["VBUS", "vbus"],
                             [net("A", "VBUS"), net("B", "vbus")])
    result = normalize_circuit_pads(circuits, pins)
    assert [n.nodes for n in result.section.circuits[0].nets] == [["U1.7"], ["U1.7"]]
    assert any(d["code"] == "physical_pad_net_conflict" and d["severity"] == "critical"
               for d in result.diagnostics)


def test_duplicate_physical_inventory_never_normalized():
    pins, circuits = sections([(1, "A"), (1, "B")], ["A"])
    result = normalize_circuit_pads(circuits, pins)
    assert result.section == circuits
    assert result.diagnostics[0]["code"] == "unusable_pin_inventory"


def test_meaningful_punctuation_is_not_ignored():
    pins, circuits = sections([(1, "EN"), (2, "EN_N"), (3, "D+"), (4, "D-")],
                             ["EN-N", "D"], [net("N", "EN-N", "D")])
    result = normalize_circuit_pads(circuits, pins)
    assert result.section.circuits[0].nets[0].nodes == ["U1.EN-N", "U1.D"]
    assert len([d for d in result.diagnostics if d["code"] == "unresolved_pin_label"]) == 2


def test_cy_baseline_reconciles_full_inventory_but_retains_wakeup_ambiguity():
    fixture = json.loads((Path(__file__).parent / "fixtures/cy_pin_netlist.json").read_text(encoding="utf-8"))
    pins, circuits = [SectionResult.model_validate(row) for row in fixture]
    result = normalize_circuit_pads(circuits, pins)
    assert len(pins.pins) == 28 and len(result.section.circuits) == 2
    for circuit in result.section.circuits:
        ic = next(c for c in circuit.components if c.ref == circuit.ic_ref)
        assert {str(i) for i in range(1, 29)} <= set(ic.terminals)
        assert not {"GND_A", "GND_B", "VDD_A", "VDD_B", "D-", "VBUS"} & set(ic.terminals)
        assert "WAKEUP" in ic.terminals
        assert circuit.explicitly_unconnected_pads == ["6"]
        observed = {node.removeprefix("U1.") for net in circuit.nets for node in net.nodes
                    if node.startswith("U1.")}
        assert {str(i) for i in range(1, 29)} <= (
            observed | set(circuit.explicitly_unconnected_pads) | set(circuit.unobserved_pads))
    assert len([p for p in result.provenance if p["strategy"] == "complete_same_net_pin_set"]) == 4
    assert all(d["code"] == "unresolved_pin_label" and "WAKEUP" in d["message"] for d in result.diagnostics)
    assert len(result.diagnostics) == 2


def raw_pin_regions():
    return [{"id": "parse:p6", "page": 6, "kind": "page",
             "text": "CY7C64225\nTable 2. 2-pin part pinout (SSOP)"},
            {"id": "parse:p6:t1", "page": 6, "kind": "table",
             "rows": [["Pin No.", "Name", "I/O", "Description"],
                      ["1", "D–", "USB", "USB D– Line"],
                      ["2", "V\nDD", "Power", "Supply Voltage"],
                      ["", None, None, None]]}]


def test_source_inventory_uses_raw_evidence_and_preserves_usb_classification():
    rows = source_pin_inventory(raw_pin_regions(), {"part_number": "CY7C64225", "package_name": "2-pin SSOP"})
    assert len(rows) == 2
    assert rows[0]["source_io"] == "USB" and rows[0]["name"] == "D–"
    assert rows[1]["name"] == "VDD" and rows[1]["raw_name"] == "V\nDD"
    assert rows[0]["evidence_ids"] == ["parse:p6:t1", "parse:p6"]


def test_cy_raw_pin_table_returns_complete_28_pad_inventory():
    regions = json.loads((Path(__file__).parent / "fixtures/cy_pin_table.json").read_text(encoding="utf-8"))
    rows = source_pin_inventory(regions, {"part_number": "CY7C64225", "package_name": "28-pin SSOP"})
    assert [row["pad"] for row in rows] == [str(i) for i in range(1, 29)]
    assert rows[13]["name"] == "D+" and rows[13]["source_io"] == "USB"
    assert rows[14]["name"] == "D–" and rows[14]["source_io"] == "USB"
    assert rows[15]["name"] == rows[27]["name"] == "VDD"
    assert rows[21]["name"] == "WAKE"


@pytest.mark.parametrize("change", ["other_package", "two_packages", "duplicate", "partial", "second_table",
                                   "wrong_part", "ambiguous_header"])
def test_source_inventory_declines_ambiguous_source(change):
    rows = raw_pin_regions()
    if change == "other_package":
        rows[0]["text"] = rows[0]["text"].replace("SSOP", "SOIC")
    elif change == "two_packages":
        rows[0]["text"] += "\nSOIC pinout"
    elif change == "duplicate":
        rows[1]["rows"][2][0] = "1"
    elif change == "partial":
        rows[1]["rows"].pop(2)
    elif change == "second_table":
        rows.append({**rows[1], "id": "parse:p6:t2"})
    elif change == "wrong_part":
        rows[0]["text"] = rows[0]["text"].replace("CY7C64225", "OTHERCHIP")
    else:
        rows[1]["rows"][0].append("Pin No.")
    assert source_pin_inventory(rows, {"part_number": "CY7C64225", "package_name": "2-pin SSOP"}) == []


def test_missing_pin_evidence_cannot_normalize_literal_label():
    pins, circuits = sections([(1, "WAKE")], ["WAKE"], [net("WAKE", "WAKE")])
    pins.pins[0].evidence_ids = []
    result = normalize_circuit_pads(circuits, pins)
    assert result.section.circuits[0].nets[0].nodes == ["U1.WAKE"]
    assert any(d["code"] == "uncited_pin_inventory" for d in result.diagnostics)


def test_source_alias_conflicting_with_exact_literal_pin_is_flagged():
    pins, circuits = sections([(1, "WAKE"), (2, "OTHER")], ["WAKE"], [net("WAKE", "WAKE")])
    alias = PinAlias("WAKE", "2", ("parse:p2",), "Conflicting source label")
    result = normalize_circuit_pads(circuits, pins, aliases=(alias,))
    assert result.section.circuits[0].nets[0].nodes == ["U1.1"]
    assert any(d["code"] == "conflicting_pin_alias" for d in result.diagnostics)


def source_rows(*pairs):
    return [{"pad": str(pad), "name": name, "evidence_ids": ["parse:p1:t1", "parse:p1"]}
            for pad, name in pairs]


def test_raw_pin_table_prevents_wrong_ai_name_pad_mapping_propagation():
    pins, circuits = sections([(1, "D-"), (2, "D+")], ["D+"], [net("USB", "D+")])
    before = pins.model_dump()
    result = normalize_circuit_pads(circuits, pins, source_inventory=source_rows((1, "D+"), (2, "D-")),
                                    source_evidence_allowlist={"parse:p1:t1"})
    assert result.section.circuits[0].nets[0].nodes == ["U1.1"]
    assert pins.model_dump() == before
    conflict = next(row for row in result.diagnostics if row["code"] == "pin_inventory_source_conflict")
    assert conflict["section_id"] == 1 and len(conflict["name_disagreements"]) == 2
    basis = result.provenance[-1]
    assert basis["basis"] == "deterministic_selected_source_pin_table"
    assert basis["source_certified"] is False
    assert basis["evidence_ids"] == ["parse:p1:t1"]


def test_raw_pin_mapping_does_not_authorize_unprovided_global_page_region():
    pins, circuits = sections([(1, "A"), (2, "B")], ["A"], [net("A", "A")])
    result = normalize_circuit_pads(circuits, pins, source_inventory=source_rows((1, "A"), (2, "B")),
                                    source_evidence_allowlist={"parse:p1:t1"})
    assert all("parse:p1" not in row["evidence_ids"] for row in result.provenance)
    assert "parse:p1" not in result.section.circuits[0].components[0].evidence_ids


@pytest.mark.parametrize("change", ["missing_allowlist", "only_global_page", "partial", "duplicate", "different_table"])
def test_invalid_raw_table_declines_instead_of_falling_back_to_ai(change):
    pins, circuits = sections([(1, "A"), (2, "B")], ["A"], [net("A", "A")])
    rows = source_rows((1, "A"), (2, "B"))
    allowed = {"parse:p1:t1", "parse:p1:t2"}
    if change == "missing_allowlist":
        allowed = None
    elif change == "only_global_page":
        allowed = {"parse:p1"}
    elif change == "partial":
        rows[1]["pad"] = "3"
    elif change == "duplicate":
        rows[1]["pad"] = "1"
    else:
        rows[1]["evidence_ids"] = ["parse:p1:t2"]
    result = normalize_circuit_pads(circuits, pins, source_inventory=rows, source_evidence_allowlist=allowed)
    assert result.section == circuits
    assert result.diagnostics[0]["code"] == "unusable_source_pin_inventory"


def test_no_detected_source_table_keeps_explicit_uncertified_fallback_provenance():
    pins, circuits = sections([(1, "A")], ["A"], [net("A", "A")])
    result = normalize_circuit_pads(circuits, pins, source_inventory=[])
    assert result.section.circuits[0].nets[0].nodes == ["U1.1"]
    assert result.provenance[-1]["basis"] == "extracted_section1_not_source_certified"
    assert result.provenance[-1]["source_certified"] is False


def test_raw_inventory_can_reconcile_even_when_ai_pad_inventory_is_missing_a_row():
    pins, circuits = sections([(1, "A")], ["B"], [net("B", "B")])
    result = normalize_circuit_pads(circuits, pins, source_inventory=source_rows((1, "A"), (2, "B")),
                                    source_evidence_allowlist={"parse:p1:t1"})
    assert result.section.circuits[0].nets[0].nodes == ["U1.2"]
    assert next(row for row in result.diagnostics if row["code"] == "pin_inventory_source_conflict")["missing_source_pads"] == ["2"]
    assert len(pins.pins) == 1


def test_actual_cy_source_table_normalizes_with_only_supplied_table_proof():
    fixture = Path(__file__).parent / "fixtures"
    source = json.loads((fixture / "cy_pin_table.json").read_text(encoding="utf-8"))
    sections = json.loads((fixture / "cy_pin_netlist.json").read_text(encoding="utf-8"))
    pins, circuits = [SectionResult.model_validate(section) for section in sections]
    prefix = pins.pins[0].evidence_ids[0].split(":")[0]
    for region in source:
        region["id"] = region["id"].replace("parse:", prefix + ":")
    inventory = source_pin_inventory(source, {"part_number": "CY7C64225", "package_name": "28-pin SSOP"})
    result = normalize_circuit_pads(circuits, pins, source_inventory=inventory,
                                    source_evidence_allowlist={prefix + ":p6:t1"})
    assert not any(row["code"] == "pin_inventory_source_conflict" for row in result.diagnostics)
    assert result.provenance[-1]["basis"] == "deterministic_selected_source_pin_table"
    assert result.provenance[-1]["evidence_ids"] == [prefix + ":p6:t1"]
    assert len([row for row in result.provenance if row["strategy"] == "complete_same_net_pin_set"]) == 4


@pytest.mark.parametrize("token", ["16(VDD)", "pad16(VDD)", "pad 16 VDD", "16 VDD", "16:VDD", "U1.16 VDD"])
def test_explicit_numeric_pad_plus_matching_source_name_normalizes(token):
    pins, circuits = sections([(16, "VDD")], [token], [net("VDD", token.removeprefix("U1."))])
    result = normalize_circuit_pads(circuits, pins)
    assert result.section.circuits[0].components[0].terminals == ["16"]
    assert result.section.circuits[0].nets[0].nodes == ["U1.16"]
    assert not result.diagnostics
    assert result.version == NORMALIZATION_VERSION
    assert result.provenance[0]["strategy"] == "explicit_pad_matching_literal_source_name"
    assert result.provenance[0]["normalization_version"] == NORMALIZATION_VERSION


@pytest.mark.parametrize("token", ["16(GND)", "pad16(NC)", "16 VCC", "16:VDD_A", "99 VDD",
                                  "16 VDD open schematic stub", "22(WAKEUP)"])
def test_wrong_explicit_pad_name_does_not_authorize_mapping(token):
    pins, circuits = sections([(16, "VDD"), (22, "WAKE")], [token], [net("N", token)])
    result = normalize_circuit_pads(circuits, pins)
    assert token in result.section.circuits[0].components[0].terminals
    assert result.section.circuits[0].nets[0].nodes == ["U1." + token]
    assert any(row["code"] == "explicit_pad_name_conflict" for row in result.diagnostics)


def test_unknown_other_component_prefix_is_never_stripped_as_ic_prefix():
    pins, circuits = sections([(16, "VDD")], ["U2.16(VDD)"], [net("VDD", "U2.16(VDD)")])
    result = normalize_circuit_pads(circuits, pins)
    assert "U2.16(VDD)" in result.section.circuits[0].components[0].terminals
    assert result.section.circuits[0].nets[0].nodes == ["U1.U2.16(VDD)"]


def test_explicit_pad_and_literal_name_can_retain_figure_alias_annotation_without_guessing_alias():
    pins, circuits = sections([(22, "WAKE")], ["pad 22 WAKE / figure alias WAKEUP", "WAKEUP"],
                             [net("W", "pad22(WAKE; schematic alias WAKEUP)")])
    result = normalize_circuit_pads(circuits, pins)
    assert result.section.circuits[0].nets[0].nodes == ["U1.22"]
    assert "WAKEUP" in result.section.circuits[0].components[0].terminals
    assert any("WAKEUP" in row["labels"][0] for row in result.provenance if row.get("labels"))
    assert any(row["code"] == "unresolved_pin_label" and "WAKEUP" in row["message"]
               for row in result.diagnostics)


def test_actual_fresh_units_normalize_decorations_but_preserve_genuine_unknowns():
    fixture = Path(__file__).parent / "fixtures"
    source = json.loads((fixture / "cy_pin_table.json").read_text(encoding="utf-8"))
    units = json.loads((fixture / "cy_decorated_figure_units.json").read_text(encoding="utf-8"))
    pin_section = SectionResult.model_validate(json.loads((fixture / "cy_pin_netlist.json").read_text(encoding="utf-8"))[0])
    for unit in units:
        section = SectionResult.model_validate(unit["section"])
        parse_id = section.circuits[0].evidence_ids[0].split(":")[0]
        for region in source:
            region["id"] = parse_id + (":p6:t1" if region["kind"] == "table" else ":p6")
        inventory = source_pin_inventory(source, {"part_number": "CY7C64225", "package_name": "28-pin SSOP"})
        before = section.model_dump()
        result = normalize_circuit_pads(section, pin_section, source_inventory=inventory,
                                        source_evidence_allowlist={parse_id + ":p6:t1"})
        assert section.model_dump() == before
        circuit = result.section.circuits[0]
        ic = next(component for component in circuit.components if component.ref == circuit.ic_ref)
        assert {str(i) for i in range(1, 29)} <= set(ic.terminals)
        assert circuit.explicitly_unconnected_pads == ["6"]
        assert not any(row["code"] == "explicit_pad_name_conflict" for row in result.diagnostics)
        assert not any("(" in terminal or ":" in terminal for terminal in ic.terminals)
        nodes = [node for net in circuit.nets for node in net.nodes]
        if unit["role"] == "blind" and unit["figure_number"] == "3":
            assert "schematic label NO CONNECT" in nodes
        if unit["role"] == "blind" and unit["figure_number"] == "4":
            assert "SUSPEND open schematic stub" in nodes
        if unit["role"] == "extract" and unit["figure_number"] == "4":
            assert any("WAKEUP schematic terminal" in terminal for terminal in ic.terminals)
            assert any("WAKEUP schematic terminal" in node for node in nodes)
            assert "22" in circuit.unobserved_pads
        else:
            assert set(ic.terminals) == {str(i) for i in range(1, 29)}
