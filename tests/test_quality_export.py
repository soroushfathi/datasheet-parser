"""Export proof authorization, linked diagnostics and preserved quality audit."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from datasheet_parser.config import Settings
from datasheet_parser.engine import Engine
from datasheet_parser.models import Review, SectionResult
from datasheet_parser.requirements import requirements
from datasheet_parser.review_context import reconcile_review
from datasheet_parser.store import MemoryStore
from datasheet_parser.validation import validate_section

PIN_SOURCE = "parse:p1:t1"
CIRCUIT_SOURCE = "parse:p2"
UNSUPPLIED_SOURCE = "parse:p3"


class ForbiddenProvider:
    def generate(self, *args, **kwargs):
        raise AssertionError("Export and structural validation must not invoke a model")


def empty_section(sid):
    return {
        "section_id": sid, "status": "not_found", "claims": [], "pins": [], "circuits": [], "notes": [],
        "findings": [{"requirement_id": req, "status": "not_found", "evidence_ids": [],
                      "reason": "This source fixture did not state the requirement."} for req in requirements(sid)],
    }


def exported_fixture(tmp_path, *, quality="quality-v1"):
    store = MemoryStore()
    store.put("parses", {"id": "parse", "document_id": "document", "status": "complete", "page_count": 3})
    target = {"id": "selected", "manufacturer": "Fixture", "part_number": "TEST",
              "ordering_code": "TEST-D", "package_name": "4-pin SOIC", "package_code": "D",
              "temperature_grade": "C", "document_revision": "1", "evidence_ids": [PIN_SOURCE],
              "status": "supported", "reason": "Explicit selected ordering code in the source fixture."}
    store.put("catalogs", {"id": "parse", "targets": [target], "ambiguities": []})
    for page, rid in enumerate([PIN_SOURCE, CIRCUIT_SOURCE, UNSUPPLIED_SOURCE], 1):
        store.put("regions", {"id": rid, "parse_id": "parse", "page": page, "kind": "table" if page == 1 else "page",
                              "text": "Selected-package pin table" if page == 1 else "Source schematic"})
    settings = Settings(mongo_uri="unused", work_dir=tmp_path, quality=quality)
    parser = Engine(store, ForbiddenProvider(), settings)
    run_id = parser.create_run("parse", target["id"])
    for sid in range(1, 18):
        section = empty_section(sid)
        source = PIN_SOURCE if sid == 1 else CIRCUIT_SOURCE
        if sid in {1, 2}:
            section["status"] = "present"
            for finding in section["findings"]:
                finding.update(status="present", evidence_ids=[source], reason="Explicit source fixture data.")
        if sid == 1:
            section["pins"] = [
                {"pad": str(pad), "name": name, "electrical_type": kind, "functions": [], "conditions": [],
                 "connection_requirements": [], "evidence_ids": [PIN_SOURCE]}
                for pad, name, kind in [(1, "TxD", "output"), (2, "GND", "ground"),
                                        (3, "VDD", "power_in"), (4, "NC", "nc")]
            ]
        if sid == 2:
            section["circuits"] = [{
                "id": "figure1", "figure": "Figure 1", "conditions": [], "ic_ref": "U1",
                "components": [{"ref": "U1", "ref_origin": "generated", "kind": "IC", "value": "TEST",
                                "terminals": ["TxD", "GND", "VDD"], "evidence_ids": [CIRCUIT_SOURCE]}],
                "nets": [{"name": "SERIAL_TX", "nodes": ["U1.TxD"], "evidence_ids": [CIRCUIT_SOURCE],
                          "status": "supported"}],
                "explicitly_unconnected_pads": [], "unobserved_pads": ["U1.GND", "U1.VDD"],
                "evidence_ids": [CIRCUIT_SOURCE], "ambiguities": [],
            }]
        store.put("results", {"id": f"{run_id}:{sid}", "run_id": run_id, "section_id": sid,
                              "section": section, "validation": [], "supplied_region_ids": [source],
                              "visual_pages": [], "packet_metrics": {}})
        store.put("reviews", {"id": f"{run_id}:{sid}", "run_id": run_id, "section_id": sid,
                              "supported_claim_ids": [], "issues": [], "reviewed_region_ids": [source]})
    return parser, store, run_id


def fact(cid, requirement_id, *, status="present", evidence_ids=(CIRCUIT_SOURCE,)):
    return {"id": cid, "requirement_id": requirement_id, "parameter": "Voltage",
            "kind": "text", "value": "-0.3 V ≤ VIN ≤ 5 V", "raw_value": "-0.3 V ≤ VIN ≤ 5 V",
            "quantity": None, "conditions": ["0°C ≤ TA ≤ 55°C"], "applies_to": ["selected"],
            "evidence_ids": list(evidence_ids), "status": status, "explanation": "Explicit source fixture fact."}


def test_quality_export_maps_physical_pads_with_only_valid_pin_source_proofs(tmp_path):
    parser, store, run_id = exported_fixture(tmp_path)
    original = store.get("results", f"{run_id}:2")
    result = parser.export(run_id)
    circuit = result.sections[1].circuits[0]
    assert circuit.nets[0].nodes == ["U1.1"]
    assert circuit.components[0].terminals == ["1", "2", "3", "4"]
    assert circuit.unobserved_pads == ["2", "3", "4"]
    assert circuit.explicitly_unconnected_pads == []  # Package NC did not invent a schematic NC marker.
    assert PIN_SOURCE in circuit.components[0].evidence_ids
    assert not any(issue.get("code") in {"unseen_evidence", "invalid_evidence", "package_pin_mismatch",
                                          "unknown_pad_disposition"} for issue in result.issues)
    audit = result.metadata["quality_audit"]
    assert result.metadata["quality_version"] == audit["version"] == "quality-v1"
    assert any(row["labels"] == ["TxD"] and row["pads"] == ["1"]
               and PIN_SOURCE in row["evidence_ids"] for row in audit["normalization"]["provenance"]
               if row["strategy"] == "cited_pin_name")
    assert audit["semantic_coverage"]
    assert all(row["basis"] == "extracted_claims_not_verified_source_completeness"
               for row in audit["semantic_coverage"])
    assert store.get("results", f"{run_id}:2") == original  # Export preserves original extraction for audit.
    assert store.get("runs", run_id)["status"] == result.status


@pytest.mark.parametrize("owner", ["ic", "net", "external_component"])
def test_existing_unsupplied_citation_is_not_authorized_by_normalization_provenance(tmp_path, owner):
    parser, store, run_id = exported_fixture(tmp_path)
    row = store.get("results", f"{run_id}:2")
    circuit = row["section"]["circuits"][0]
    if owner == "ic":
        circuit["components"][0]["evidence_ids"].append(UNSUPPLIED_SOURCE)
        owner_id = "U1"
    elif owner == "net":
        circuit["nets"][0]["evidence_ids"].append(UNSUPPLIED_SOURCE)
        owner_id = "SERIAL_TX"
    else:
        circuit["components"].append({"ref": "R1", "ref_origin": "source", "kind": "resistor", "value": "1 kΩ",
                                      "terminals": ["1", "2"], "evidence_ids": [UNSUPPLIED_SOURCE]})
        owner_id = "R1"
    store.put("results", row)
    result = parser.export(run_id)
    assert any(issue.get("code") == "unseen_evidence" and issue.get("claim_id") == owner_id
               for issue in result.issues)
    assert result.status == "needs_review"


def test_pin_proof_not_supplied_to_pin_reader_cannot_authorize_a_circuit_citation(tmp_path):
    parser, store, run_id = exported_fixture(tmp_path)
    row = store.get("results", f"{run_id}:1")
    row["supplied_region_ids"] = [CIRCUIT_SOURCE]
    store.put("results", row)
    result = parser.export(run_id)
    assert any(issue.get("code") == "unseen_evidence" and issue.get("section_id") == 1
               for issue in result.issues)
    assert any(issue.get("code") == "unseen_evidence" and issue.get("section_id") == 2
               and issue.get("claim_id") == "U1" for issue in result.issues)


def test_quality_export_links_unverified_warning_without_losing_detailed_issue_or_raw_audit(tmp_path):
    parser, store, run_id = exported_fixture(tmp_path)
    row = store.get("results", f"{run_id}:3")
    row["section"]["status"] = "present"
    row["section"]["claims"] = [fact("3.1", "3.voltages"), fact("3.2", "3.currents_power")]
    for finding in row["section"]["findings"]:
        if finding["requirement_id"] in {"3.voltages", "3.currents_power"}:
            finding.update(status="present", evidence_ids=[CIRCUIT_SOURCE])
    store.put("results", row)
    negative = {"category": "incorrect", "claim_id": "3.1", "requirement_id": "3.voltages",
                "severity": "error", "evidence_ids": [CIRCUIT_SOURCE],
                "explanation": "Value is from a different electrical condition."}
    false_absence = {"category": "missing_condition", "claim_id": "3.1", "requirement_id": "3.voltages",
                     "severity": "warning", "evidence_ids": [CIRCUIT_SOURCE],
                     "explanation": "Missing condition: 0°C ≤ TA ≤ 55°C"}
    raw_review = Review.model_validate({"supported_claim_ids": [], "issues": [negative, false_absence],
                                       "reviewed_region_ids": [CIRCUIT_SOURCE]})
    effective, reconciliation = reconcile_review(raw_review, row["section"])
    review_row = {"id": f"{run_id}:3", "run_id": run_id, "section_id": 3,
                  **effective.model_dump(mode="json"), "review_reconciliation": reconciliation}
    store.put("reviews", review_row)
    result = parser.export(run_id)
    assert any(issue.get("category") == "incorrect" and issue.get("claim_id") == "3.1"
               and issue["explanation"] == negative["explanation"] for issue in result.issues)
    assert not any(issue.get("code") == "unverified_claim" and issue.get("claim_id") == "3.1"
                   for issue in result.issues)
    assert any(issue.get("code") == "unverified_claim" and issue.get("claim_id") == "3.2"
               for issue in result.issues)  # Claims without a detailed explanation still need review.
    linked = result.metadata["quality_audit"]["linked_review_diagnostics"]
    assert linked == [{"code": "unverified_claim", "claim_id": "3.1", "section_id": 3,
                       "linked_to": "detailed_review_issue"}]
    exported_audit = result.metadata["review_reconciliation"]["3"]
    assert exported_audit == reconciliation
    assert exported_audit["raw_review"]["issues"] == [negative, false_absence]
    assert exported_audit["decisions"][0]["matches"][0]["field"] == "conditions[0]"
    assert store.get("reviews", f"{run_id}:3") == review_row
    assert result.status == "needs_review"


def test_quality_semantic_guard_records_misplacement_without_rewriting_owning_fact(tmp_path):
    parser, store, run_id = exported_fixture(tmp_path)
    row = store.get("results", f"{run_id}:7")
    thermal = fact("7.1", "7.dimensions")
    thermal.update(parameter="θJA", value="90 °C/W", raw_value="90 °C/W")
    row["section"].update(status="present", claims=[thermal])
    row["section"]["findings"][0].update(status="present", evidence_ids=[CIRCUIT_SOURCE])
    store.put("results", row)
    result = parser.export(run_id)
    assert result.sections[6].claims[0].model_dump(mode="json") == thermal
    audit = result.metadata["quality_audit"]
    assert any(issue["code"] == "semantic_requirement_mismatch" and issue["claim_id"] == "7.1"
               and issue["suggested_requirement_id"] == "11.metrics" for issue in audit["semantic_diagnostics"])
    assert any(issue.get("code") == "semantic_requirement_mismatch" for issue in result.issues)


def test_baseline_version_export_preserves_symbolic_netlist_and_duplicate_warning_behavior(tmp_path):
    parser, store, run_id = exported_fixture(tmp_path, quality="baseline")
    row = store.get("results", f"{run_id}:3")
    row["section"].update(status="present", claims=[fact("3.1", "3.voltages")])
    row["section"]["findings"][0].update(status="present", evidence_ids=[CIRCUIT_SOURCE])
    store.put("results", row)
    review_row = store.get("reviews", f"{run_id}:3")
    review_row["issues"] = [{"category": "incorrect", "claim_id": "3.1", "requirement_id": "3.voltages",
                             "severity": "error", "evidence_ids": [CIRCUIT_SOURCE], "explanation": "Fixture error"}]
    store.put("reviews", review_row)
    before = deepcopy(store.find("results", {"run_id": run_id}))
    result = parser.export(run_id)
    assert result.sections[1].circuits[0].nets[0].nodes == ["U1.TxD"]
    assert result.sections[1].circuits[0].components[0].evidence_ids == [CIRCUIT_SOURCE]
    assert any(issue.get("code") == "package_pin_mismatch" for issue in result.issues)
    assert any(issue.get("code") == "unverified_claim" and issue.get("claim_id") == "3.1"
               for issue in result.issues)
    assert any(issue.get("category") == "incorrect" and issue.get("claim_id") == "3.1"
               for issue in result.issues)
    assert "quality_audit" not in result.metadata
    assert "review_reconciliation" not in result.metadata
    assert store.find("results", {"run_id": run_id}) == before


def uncertainty_fixture():
    data = empty_section(3)
    req = "3.voltages"
    data["status"] = "ambiguous"
    data["findings"][0].update(status="ambiguous", evidence_ids=[],
                               reason="An omitted candidate page was not inspected; absence is not certified.")
    uncertainty = {req: {"reported_status": "not_found", "effective_status": "ambiguous",
                         "uninspected_candidate_pages": [2], "supplied_region_ids": []}}
    return data, uncertainty


def test_derived_absence_uncertainty_permits_empty_finding_evidence_and_stays_unresolved():
    data, uncertainty = uncertainty_fixture()
    section = SectionResult.model_validate(data)
    issues = validate_section(section, "selected", {CIRCUIT_SOURCE}, coverage_uncertainty=uncertainty)
    assert not any(issue["code"] == "missing_evidence" for issue in issues)
    assert any(issue["code"] == "unresolved_requirement" and issue["claim_id"] == "3.voltages"
               for issue in issues)
    without_provenance = validate_section(section, "selected", {CIRCUIT_SOURCE})
    assert any(issue["code"] == "missing_evidence" and issue["claim_id"] == "3.voltages"
               for issue in without_provenance)


@pytest.mark.parametrize("status", ["present", "ambiguous"])
def test_absence_coverage_metadata_does_not_excuse_unsupported_positive_claims(status):
    data, uncertainty = uncertainty_fixture()
    data["claims"] = [fact("3.1", "3.voltages", status=status, evidence_ids=())]
    issues = validate_section(SectionResult.model_validate(data), "selected", {CIRCUIT_SOURCE},
                              coverage_uncertainty=uncertainty)
    assert any(issue["code"] == "missing_evidence" and issue["claim_id"] == "3.1"
               for issue in issues)


def test_coverage_uncertainty_export_preserves_ambiguity_without_false_missing_evidence(tmp_path):
    parser, store, run_id = exported_fixture(tmp_path)
    data, uncertainty = uncertainty_fixture()
    row = store.get("results", f"{run_id}:3")
    row.update(section=data, packet_metrics={"absence_coverage_uncertainty": uncertainty})
    store.put("results", row)
    result = parser.export(run_id)
    assert result.sections[2].findings[0].status == "ambiguous"
    assert not any(issue.get("code") == "missing_evidence" and issue.get("section_id") == 3
                   for issue in result.issues)
    assert any(issue.get("code") == "unresolved_requirement" and issue.get("section_id") == 3
               for issue in result.issues)
    assert result.status == "needs_review"


def attach_crop_uncertainty(parser, store, run_id, monkeypatch, *, include_real_graphic=False):
    table_id = "parse:p15:t1"
    store.put("regions", {"id": table_id, "parse_id": "parse", "page": 15, "kind": "table",
                          "text": "Supplied timing table without a propagation-delay value"})
    row = store.get("results", f"{run_id}:12")
    row["section"]["status"] = "present"
    finding = next(f for f in row["section"]["findings"] if f["requirement_id"] == "12.delays")
    reported = deepcopy(finding)
    reported["reason"] = "The supplied timing table states no propagation-delay value."
    finding.update(status="ambiguous", reason="Graphical page 15 was not inspected; absence is not certified.")
    metrics = {
        "image_pages": [], "image_source_pages": [15],
        "images": [{"kind": "table_crop", "page": 15, "source_region_ids": [table_id]}],
        "omitted_chunk_ids": [], "omitted_candidate_ids": [], "omitted_bundles": [],
        "unresolved_evidence_requests": [],
        "absence_coverage_uncertainty": {"12.delays": {
            "reported_status": "not_found", "effective_status": "ambiguous", "reported_finding": reported,
            "uninspected_candidate_pages": [15], "omitted_candidate_ids": [],
            "visual_candidate_ids": ["timing-table"], "supplied_region_ids": [table_id],
        }},
    }
    by_id = {"timing-table": {"id": "timing-table", "kind": "table", "page": 15,
                              "visual": True, "unknown_visual": False, "region_ids": [table_id]}}
    selected = {"12.delays": {"timing-table"}}
    unresolved = ["12.delays"]
    if include_real_graphic:
        before = next(f for f in row["section"]["findings"] if f["requirement_id"] == "12.startup_reset")
        raw_frame = deepcopy(before)
        before.update(status="ambiguous", reason="The frame waveform was not supplied.")
        by_id["frame-graphic"] = {"id": "frame-graphic", "kind": "visual", "page": 15,
                                  "visual": True, "unknown_visual": False, "region_ids": ["parse:p15"]}
        metrics["absence_coverage_uncertainty"]["12.startup_reset"] = {
            "reported_status": "not_found", "effective_status": "ambiguous", "reported_finding": raw_frame,
            "visual_candidate_ids": ["frame-graphic"], "uninspected_candidate_pages": [15],
            "omitted_candidate_ids": [], "supplied_region_ids": [table_id],
        }
        selected["12.startup_reset"] = {"frame-graphic"}
        unresolved.append("12.startup_reset")

    def select(req, target):
        assert target["id"] == "selected"
        return selected[req], {}

    monkeypatch.setattr(parser, "retrieval_context", lambda run: (
        {"id": "frozen-fixture-index"}, SimpleNamespace(by_id=by_id, select=select),
    ))
    row.update(packet_metrics=metrics, supplied_region_ids=[table_id], validation=[{
        "code": "incomplete_visual_search", "section_id": 12, "severity": "warning", "requirements": unresolved,
    }])
    store.put("results", row)
    return row


def test_export_corrects_only_false_crop_uncertainty_with_preserved_raw_finding_and_proofs(tmp_path, monkeypatch):
    parser, store, run_id = exported_fixture(tmp_path)
    before = attach_crop_uncertainty(parser, store, run_id, monkeypatch)
    result = parser.export(run_id)
    finding = next(f for f in result.sections[11].findings if f.requirement_id == "12.delays")
    assert finding.status == "not_found"
    assert finding.reason.startswith("The supplied timing table states no propagation-delay value.")
    assert "not_found is limited to supplied evidence; document-wide absence is not certified" in finding.reason
    assert finding.evidence_ids == []  # Exact raw disposition retained; table image is separate coverage proof.
    assert result.sections[11].claims == []
    assert store.get("reviews", f"{run_id}:12")["supported_claim_ids"] == []
    assert not any(issue.get("code") == "incomplete_visual_search" and issue.get("section_id") == 12
                   for issue in result.issues)
    assert not any(issue.get("code") == "unresolved_requirement" and issue.get("claim_id") == "12.delays"
                   for issue in result.issues)
    audit = result.metadata["quality_audit"]["absence_reconciliation"]
    assert len(audit) == 1 and audit[0]["section_id"] == 12
    decision = audit[0]["decisions"][0]
    raw_finding = next(f for f in before["section"]["findings"] if f["requirement_id"] == "12.delays")
    assert decision["before_finding"] == raw_finding
    assert decision["after_finding"] == finding.model_dump(mode="json")
    assert decision["candidate_image_coverage"]["proofs"] == [{
        "chunk_id": "timing-table", "page": 15, "kind": "table", "basis": "table_crop",
        "source_region_ids": ["parse:p15:t1"],
    }]
    assert result.metadata["reviewed_is_not_certified_correct"] is True
    assert store.get("results", f"{run_id}:12") == before  # Stored model status and old validation stay auditable.


def test_export_keeps_real_visual_uncertainty_and_only_filters_the_restored_requirement(tmp_path, monkeypatch):
    parser, store, run_id = exported_fixture(tmp_path)
    before = attach_crop_uncertainty(parser, store, run_id, monkeypatch, include_real_graphic=True)
    result = parser.export(run_id)
    findings = {f.requirement_id: f for f in result.sections[11].findings}
    assert findings["12.delays"].status == "not_found"
    assert findings["12.startup_reset"].status == "ambiguous"
    incomplete = [issue for issue in result.issues
                  if issue.get("code") == "incomplete_visual_search" and issue.get("section_id") == 12]
    assert incomplete == [{"code": "incomplete_visual_search", "section_id": 12, "severity": "warning",
                           "requirements": ["12.startup_reset"]}]
    assert any(issue.get("code") == "unresolved_requirement" and issue.get("claim_id") == "12.startup_reset"
               for issue in result.issues)
    audit = result.metadata["quality_audit"]["absence_reconciliation"]
    assert [decision["requirement_id"] for decision in audit[0]["decisions"]] == ["12.delays"]
    assert store.get("results", f"{run_id}:12") == before
    assert result.status == "needs_review"


@pytest.mark.parametrize("veto", ["omitted_bundle", "unresolved_request", "present_claim", "ambiguous_claim"])
def test_export_restoration_does_not_hide_missing_source_or_unsupported_claim(veto, tmp_path, monkeypatch):
    parser, store, run_id = exported_fixture(tmp_path)
    before = attach_crop_uncertainty(parser, store, run_id, monkeypatch)
    if veto == "omitted_bundle":
        before["packet_metrics"]["omitted_bundles"] = [{"root_chunk_id": "omitted-source"}]
    elif veto == "unresolved_request":
        before["packet_metrics"]["unresolved_evidence_requests"] = ["Need the missing waveform"]
    else:
        before["section"]["claims"] = [fact("12.1", "12.delays",
                                           status="present" if veto == "present_claim" else "ambiguous", evidence_ids=())]
    store.put("results", before)
    result = parser.export(run_id)
    finding = next(f for f in result.sections[11].findings if f.requirement_id == "12.delays")
    assert finding.status == "ambiguous"
    assert result.metadata["quality_audit"]["absence_reconciliation"] == []
    assert any(issue.get("code") == "incomplete_visual_search" and issue.get("section_id") == 12
               for issue in result.issues)
    if veto in {"present_claim", "ambiguous_claim"}:
        assert any(issue.get("code") == "missing_evidence" and issue.get("claim_id") == "12.1"
                   for issue in result.issues)
    assert store.get("results", f"{run_id}:12") == before
    assert result.status == "needs_review"


def test_baseline_export_does_not_rewrite_stored_crop_uncertainty(tmp_path, monkeypatch):
    parser, store, run_id = exported_fixture(tmp_path, quality="baseline")
    before = attach_crop_uncertainty(parser, store, run_id, monkeypatch)
    result = parser.export(run_id)
    finding = next(f for f in result.sections[11].findings if f.requirement_id == "12.delays")
    assert finding.status == "ambiguous"
    assert "quality_audit" not in result.metadata
    assert any(issue.get("code") == "incomplete_visual_search" and issue.get("section_id") == 12
               for issue in result.issues)
    assert store.get("results", f"{run_id}:12") == before
