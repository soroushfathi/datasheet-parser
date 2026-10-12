"""A named NC singleton is retained, while actual connected contradictions stay errors."""

from copy import deepcopy

import pytest

from datasheet_parser.models import SectionResult
from datasheet_parser.requirements import requirements
from datasheet_parser.validation import validate_section


def section(name="RESET_NO_CONNECT", *, nodes=("U1.2",), status="supported", evidence=("source",), other_nets=()):
    return SectionResult.model_validate({
        "section_id": 2, "status": "present", "claims": [], "pins": [], "notes": [],
        "findings": [{"requirement_id": req, "status": "present", "evidence_ids": ["source"], "reason": ""}
                     for req in requirements(2)],
        "circuits": [{"id": "figure3", "figure": "Figure 3", "conditions": [], "ic_ref": "U1",
                      "components": [{"ref": "U1", "ref_origin": "source", "kind": "IC", "value": "TEST",
                                      "terminals": ["1", "2", "3"], "evidence_ids": ["source"]},
                                     {"ref": "R1", "ref_origin": "source", "kind": "resistor", "value": "1 kΩ",
                                      "terminals": ["1", "2"], "evidence_ids": ["source"]}],
                      "nets": [{"name": name, "nodes": list(nodes), "status": status, "evidence_ids": list(evidence)},
                               *other_nets],
                      "explicitly_unconnected_pads": ["2"], "unobserved_pads": ["1", "3"],
                      "evidence_ids": ["source"], "ambiguities": []}],
    })


def codes(data, *, allow=True):
    return {issue["code"] for issue in validate_section(data, "target", {"source"}, {"source"},
                                                       allow_explicit_nc_stub=allow)}


@pytest.mark.parametrize("name", ["RESET_NO_CONNECT", "RESET_NC", "UNCONNECTED_RESET", "RESET_OPEN", "RESET-No Connect"])
def test_quality_explicit_singleton_marker_is_not_a_connection_and_preserves_raw_net(name):
    data = section(name)
    original = deepcopy(data.model_dump(mode="json"))
    assert "nc_connected" not in codes(data)
    assert data.model_dump(mode="json") == original
    assert "nc_connected" in codes(data, allow=False)  # Existing runs retain the old structural rule.


@pytest.mark.parametrize("name", [
    "RESET", "NCMODE", "RESET_FLOATING", "GND", "VCC", "VDD_NC", "NC_AGND", "POWER_OPEN",
    "NC_VBUS", "OPEN_DRAIN", "RESET_OPEN_DRAIN", "OPEN_COLLECTOR", "NC_SUPPLY",
])
def test_unsupported_or_power_output_labels_cannot_certify_no_connect(name):
    assert "nc_connected" in codes(section(name))


@pytest.mark.parametrize("nodes", [("U1.2", "R1.1"), ("U1.2", "U1.3"), ("U1.2", "J1.unknown")])
def test_any_additional_terminal_is_a_real_nc_connection_contradiction(nodes):
    assert "nc_connected" in codes(section(nodes=nodes))


def test_second_real_net_invalidates_an_otherwise_explicit_nc_stub():
    second = {"name": "VDD", "nodes": ["U1.2", "R1.1"], "evidence_ids": ["source"], "status": "supported"}
    observed = codes(section(other_nets=[second]))
    assert {"nc_connected", "terminal_in_two_nets"} <= observed


def test_two_nc_named_nets_do_not_hide_double_membership():
    second = {"name": "NC_RESET", "nodes": ["U1.2"], "evidence_ids": ["source"], "status": "supported"}
    assert {"nc_connected", "terminal_in_two_nets"} <= codes(section(other_nets=[second]))


@pytest.mark.parametrize("status,evidence,additional_code", [
    ("ambiguous", ("source",), "ambiguous_connection"),
    ("supported", (), "missing_evidence"),
])
def test_uncertain_or_uncited_stub_is_not_a_supported_no_connect(status, evidence, additional_code):
    assert {"nc_connected", additional_code} <= codes(section(status=status, evidence=evidence))


def test_unobserved_disposition_is_not_covered_by_explicit_nc_exception():
    data = section()
    data.circuits[0].explicitly_unconnected_pads = []
    data.circuits[0].unobserved_pads.append("2")
    assert "unobserved_connected" in codes(data)
