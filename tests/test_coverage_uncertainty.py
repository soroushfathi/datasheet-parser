"""Table crops prove their own regions, never arbitrary graphics on the page."""

from copy import deepcopy

import pytest
from test_optimized_workflow import engine, no_network_provider_or_tokenizer, source  # noqa: F401

from datasheet_parser.coverage_uncertainty import reconcile_absence_coverage, visual_candidate_coverage
from datasheet_parser.models import SectionResult
from datasheet_parser.requirements import requirements


def table(cid="table-1", page=15, regions=("parse:p15:t1",), *, unknown=False):
    return {"id": cid, "page": page, "kind": "table", "visual": True,
            "unknown_visual": unknown, "region_ids": list(regions)}


def crop_metrics():
    return {"image_pages": [], "image_source_pages": [15],
            "images": [{"page": 15, "kind": "table_crop", "source_region_ids": ["parse:p15:t1", "parse:p15:note"]}],
            "omitted_chunk_ids": [], "omitted_candidate_ids": [], "omitted_bundles": [],
            "unresolved_evidence_requests": []}


def test_complete_table_crop_proves_table_without_claiming_whole_page_coverage():
    metrics = crop_metrics()
    result = visual_candidate_coverage([table()], metrics)
    assert result["uninspected_pages"] == []
    assert result["uninspected_chunk_ids"] == []
    assert result["proofs"] == [{"chunk_id": "table-1", "page": 15, "kind": "table",
                                 "basis": "table_crop", "source_region_ids": ["parse:p15:t1"]}]
    assert metrics["image_pages"] == []


@pytest.mark.parametrize("candidate", [
    table("different-table", regions=("parse:p15:t2",)),
    table("partial-table", regions=("parse:p15:t1", "parse:p15:t2")),
    table("different-page", page=16),
    table("unknown-page-table", unknown=True),
    {"id": "real-graphic", "page": 15, "kind": "visual", "visual": True,
     "unknown_visual": False, "region_ids": ["parse:p15"]},
    {"id": "unclassified-text", "page": 15, "kind": "text", "visual": False,
     "unknown_visual": True, "region_ids": ["parse:p15:l1"]},
])
def test_a_crop_does_not_prove_other_or_unknown_graphical_candidates(candidate):
    result = visual_candidate_coverage([candidate], crop_metrics())
    assert result["uninspected_chunk_ids"] == [candidate["id"]]
    assert result["uninspected_pages"] == [candidate["page"]]


def test_page_number_without_image_descriptor_is_not_table_crop_proof():
    result = visual_candidate_coverage([table()], {"image_pages": [], "image_source_pages": [15]})
    assert result["uninspected_chunk_ids"] == ["table-1"]


def test_whole_page_covers_table_and_true_graphical_anchor():
    graphic = {"id": "graphic", "page": 15, "kind": "visual", "visual": True,
               "unknown_visual": True, "region_ids": ["parse:p15"]}
    result = visual_candidate_coverage([table(), graphic], {
        "image_pages": [15], "images": [{"page": 15, "kind": "whole_page", "source_region_ids": ["parse:p15"]}],
    })
    assert not result["uninspected_chunk_ids"]
    assert {row["basis"] for row in result["proofs"]} == {"whole_page"}


def restoration_fixture(*, retained_reason=True):
    data = {"section_id": 12, "status": "present", "claims": [], "pins": [], "circuits": [], "notes": [],
            "findings": [{"requirement_id": req, "status": "not_found", "evidence_ids": [],
                          "reason": "Not stated in the supplied evidence."} for req in requirements(12)]}
    finding = next(row for row in data["findings"] if row["requirement_id"] == "12.delays")
    reported = deepcopy(finding)
    finding.update(status="ambiguous", reason="Graphical page 15 was not inspected. Absence is not certified.")
    metrics = crop_metrics()
    uncertainty = {"reported_status": "not_found", "effective_status": "ambiguous",
                   "uninspected_candidate_pages": [15], "omitted_candidate_ids": [],
                   "supplied_region_ids": ["parse:p15:t1", "parse:p15:note"]}
    if retained_reason:
        uncertainty.update(reported_finding=reported, visual_candidate_ids=["table-1"])
    metrics["absence_coverage_uncertainty"] = {"12.delays": uncertainty}
    return SectionResult.model_validate(data), metrics, {"12.delays": [table()]}


@pytest.mark.parametrize("retained_reason", [True, False])
def test_restoration_preserves_before_provenance_and_limited_source_disclaimer(retained_reason):
    section, metrics, candidates = restoration_fixture(retained_reason=retained_reason)
    before = section.model_dump(mode="json")
    before_metrics = deepcopy(metrics)
    corrected, audit = reconcile_absence_coverage(section, metrics, candidates)
    finding = next(row for row in corrected.findings if row.requirement_id == "12.delays")
    assert finding.status == "not_found"
    assert finding.evidence_ids == []  # Crop proof does not invent the reader's absence citations.
    assert "not_found is limited to supplied evidence" in finding.reason
    assert "document-wide absence is not certified" in finding.reason
    assert ("original reason was not retained" in finding.reason) is not retained_reason
    decision = audit["decisions"][0]
    assert decision["before_finding"]["status"] == "ambiguous"
    assert decision["after_finding"] == finding.model_dump(mode="json")
    assert decision["coverage_provenance"] == metrics["absence_coverage_uncertainty"]["12.delays"]
    assert decision["candidate_image_coverage"]["proofs"][0]["source_region_ids"] == ["parse:p15:t1"]
    assert section.model_dump(mode="json") == before
    assert metrics == before_metrics


@pytest.mark.parametrize("field,value", [
    ("unresolved_evidence_requests", ["Please inspect missing Table 8"]),
    ("omitted_chunk_ids", ["missing-source"]),
    ("omitted_candidate_ids", ["missing-source"]),
    ("omitted_bundles", [{"root_chunk_id": "missing-source"}]),
    ("mandatory_overflow", {"text_tokens": {"used": 21, "limit": 20}}),
])
def test_any_unresolved_or_omitted_packet_source_vetoes_restoration(field, value):
    section, metrics, candidates = restoration_fixture()
    metrics[field] = value
    corrected, audit = reconcile_absence_coverage(section, metrics, candidates)
    assert corrected == section
    assert not audit["decisions"]


@pytest.mark.parametrize("change", ["missing_candidates", "different_visual_set", "real_graphic", "figure_unresolved", "req_omitted"])
def test_unproved_or_changed_requirement_context_stays_uncertain(change):
    section, metrics, candidates = restoration_fixture()
    uncertainty = metrics["absence_coverage_uncertainty"]["12.delays"]
    if change == "missing_candidates":
        candidates = {}
    elif change == "different_visual_set":
        uncertainty["visual_candidate_ids"] = ["older-source-table"]
    elif change == "real_graphic":
        candidates["12.delays"].append({"id": "graphic", "page": 15, "kind": "visual", "visual": True,
                                       "unknown_visual": False, "region_ids": ["parse:p15"]})
    elif change == "figure_unresolved":
        uncertainty["source_figures_unresolved"] = ["3"]
    else:
        uncertainty["omitted_candidate_ids"] = ["table-2"]
    corrected, audit = reconcile_absence_coverage(section, metrics, candidates)
    assert corrected == section
    assert not audit["decisions"]


@pytest.mark.parametrize("status", ["present", "ambiguous", "conflicting", "unreadable"])
def test_positive_or_unresolved_claim_vetoes_absence_restoration(status):
    section, metrics, candidates = restoration_fixture()
    data = section.model_dump(mode="json")
    data["claims"] = [{"id": "12.1", "requirement_id": "12.delays", "parameter": "Delay", "kind": "text",
                       "value": "5 ms", "raw_value": "5 ms", "quantity": None, "conditions": [],
                       "applies_to": ["target"], "evidence_ids": ["parse:p15:t1"], "status": status,
                       "explanation": "Must inspect source interpretation."}]
    section = SectionResult.model_validate(data)
    assert reconcile_absence_coverage(section, metrics, candidates)[1]["decisions"] == []


def test_retained_blank_absence_reason_is_not_replaced_with_a_generated_justification():
    section, metrics, candidates = restoration_fixture()
    metrics["absence_coverage_uncertainty"]["12.delays"]["reported_finding"]["reason"] = ""
    corrected, audit = reconcile_absence_coverage(section, metrics, candidates)
    assert corrected == section
    assert audit["decisions"] == []


def quality_read_fixture(tmp_path):
    store = source(["Timing Characteristics\nDelays not specified in this fixture"])
    store.put("regions", {"id": "parse:p1:t1", "parse_id": "parse", "page": 1, "kind": "table",
                          "text": "Delay Symbol Value\nPropagation delay not stated", "bbox": [20, 150, 230, 220],
                          "rows": [["Delay", "Value"], ["Propagation", "Not specified"]], "cells": []})
    parser, provider, run_id = engine(tmp_path, store, status="not_found")
    run = store.get("runs", run_id)
    run.update(quality_version="quality-v1")
    run["retrieval_config"]["index_version"] = "structural-10"
    store.put("runs", run)
    _, retriever = parser.retrieval_context(run)
    table_id = next(chunk["id"] for chunk in retriever.chunks if chunk["kind"] == "table")
    retriever.select = lambda *args, **kwargs: ({table_id}, {table_id: {"route": "seed", "rank_score": 10}})
    parser.map_retrieval(run)
    return parser, provider, run, retriever, table_id


def test_actual_quality_read_does_not_convert_supplied_table_crop_to_false_ambiguity(tmp_path):
    parser, provider, run, _, _ = quality_read_fixture(tmp_path)
    section, _, whole_pages, metrics, unresolved = parser.read_retrieved(run, 12)
    assert len(provider.requests) == 1
    assert whole_pages == []
    assert metrics["images"][0]["kind"] == "table_crop"
    assert metrics["images"][0]["source_region_ids"]
    assert all(finding.status == "not_found" and finding.reason == "Source fixture checked" for finding in section.findings)
    assert metrics["absence_coverage_uncertainty"] == {}
    assert unresolved == []


def test_actual_quality_read_preserves_raw_reason_when_a_real_visual_anchor_is_unsupplied(tmp_path, monkeypatch):
    parser, _, run, retriever, table_id = quality_read_fixture(tmp_path)
    packet = parser.retrieval_packet(run, 12)
    assert packet.metrics["images"][0]["kind"] == "table_crop"
    # A same-page graphical anchor is a different candidate from the cropped table.
    graphic = {**retriever.by_id[table_id], "id": "fixture-real-visual", "kind": "visual", "visual": True,
               "unknown_visual": False, "region_ids": ["parse:p1"]}
    retriever.by_id[graphic["id"]] = graphic
    retriever.select = lambda *args, **kwargs: ({table_id, graphic["id"]}, {})
    monkeypatch.setattr(parser, "retrieval_packet", lambda *args, **kwargs: packet)
    section, _, _, metrics, unresolved = parser.read_retrieved(run, 12)
    finding = next(finding for finding in section.findings if finding.requirement_id == "12.delays")
    assert finding.status == "ambiguous" and "12.delays" in unresolved
    provenance = metrics["absence_coverage_uncertainty"]["12.delays"]
    assert provenance["reported_finding"]["reason"] == "Source fixture checked"
    assert provenance["reported_finding"]["status"] == "not_found"
    assert provenance["reported_section_status"] == "not_found"
    assert provenance["candidate_image_coverage"]["uninspected_chunk_ids"] == [graphic["id"]]
    assert provenance["candidate_image_coverage"]["proofs"][0]["basis"] == "table_crop"
