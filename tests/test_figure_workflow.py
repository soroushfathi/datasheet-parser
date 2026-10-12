"""Offline figure-unit coverage, frozen source, independence and resume checks."""

import io
import json
import re

import pytest
from PIL import Image
from test_optimized_workflow import TARGET, no_network_provider_or_tokenizer, source  # noqa: F401

from datasheet_parser.config import Settings
from datasheet_parser.engine import Engine
from datasheet_parser.figure_workflow import read_figures, review_figures, source_app_figures
from datasheet_parser.providers import ProviderError, RecordedProvider
from datasheet_parser.requirements import requirements
from datasheet_parser.token_budget import request_input_metrics


class FigureProvider(RecordedProvider):
    name = "offline-figure-units"

    def __init__(self, settings):
        super().__init__(settings)
        self.requests = []
        self.fail_on = None
        self.omit_circuit = None
        self.bad_review_id = False

    def execute(self, request, folder, images):
        self.requests.append(request)
        assert request.task.count(":") == 3
        role, _, _, number = request.task.split(":")
        assert request_input_metrics(request.prompt, request.schema, request.images)["input_tokens_estimate"] <= request.metrics["request_token_budget"]
        if request.task == self.fail_on:
            raise ProviderError("Synthetic unit failure", code="synthetic_failure")
        aliases = list(dict.fromkeys(re.findall(r"^\[(e\d+)\]", request.prompt, re.MULTILINE)))
        assert aliases and images
        alias = aliases[-1]
        assert "RAW_PIN_INVENTORY:" in request.prompt
        assert "SOURCE_FIGURE_INVENTORY:" in request.prompt
        if role != "review":
            assert "SUBFIELD CHECKLIST:" in request.prompt
        if role == "review":
            original = json.loads(request.prompt.split("ORIGINAL: ", 1)[1].split("\nINDEPENDENT:", 1)[0])
            claims = original.get("claims", [])
            if "claim_tables" in original:
                claims = [dict(zip(table["columns"], row, strict=True))
                          for table in original["claim_tables"] for row in table["rows"]]
            ids = [c["id"] for c in claims]
            if self.bad_review_id:
                ids.append("2.bf999.1")
            return {"supported_claim_ids": ids, "issues": [], "reviewed_region_ids": aliases}, {"usage": None}
        if role == "blind":
            assert "ORIGINAL:" not in request.prompt and "EXTRACT_ONLY" not in request.prompt
        value = f"{'EXTRACT_ONLY' if role == 'extract' else 'INDEPENDENT'}_{number}"
        circuit = {
            "id": "application", "figure": f"Figure {number}", "conditions": [], "ic_ref": "U1",
            "components": [{"ref": "U1", "ref_origin": "generated", "kind": "IC", "value": "TEST1C",
                            "terminals": ["1", "2"], "evidence_ids": [alias]},
                           {"ref": "C1", "ref_origin": "source", "kind": "capacitor", "value": "1 uF",
                            "terminals": ["1", "2"], "evidence_ids": [alias]}],
            "nets": [{"name": "VCC", "nodes": ["U1.1", "C1.1"], "evidence_ids": [alias], "status": "supported"},
                     {"name": "GND", "nodes": ["U1.2", "C1.2"], "evidence_ids": [alias], "status": "supported"}],
            "explicitly_unconnected_pads": [], "unobserved_pads": [], "evidence_ids": [alias], "ambiguities": [],
        }
        return {
            "status": "present",
            "findings": [{"requirement_id": req, "status": "present", "evidence_ids": [alias], "reason": ""}
                         for req in requirements(2)],
            "claims": [{"requirement_id": req, "kind": "text", "parameter": req, "value": value,
                        "conditions": [], "evidence_ids": [alias], "status": "present", "reason": ""}
                       for req in requirements(2)],
            "circuits": [] if self.omit_circuit == number else [circuit], "search_requests": [],
        }, {"usage": None}


def figure_fixture(tmp_path):
    store = source([
        "TEST1C\nPin Configuration\n2-pin SOIC\nTable 1. Pin description",
        "TEST1C\nApplication Circuits\nFigure 13. USB bus powered design\nC1 1 uF across VCC and GND",
        "TEST1C\nBus Powered Design\nFigure 27. USB to UART converter\nC1 1 uF across VCC and GND",
        "TEST1C\nDC Electrical Characteristics\nFigure 28. Internal block diagram",
        "TEST1C\nPackage outline\nFigure 29. Mechanical package dimensions",
    ])
    target = {**TARGET, "package_name": "2-pin SOIC"}
    store.put("catalogs", {"id": "parse", "targets": [target], "ambiguities": []})
    store.put("regions", {
        "id": "parse:p1:t1", "parse_id": "parse", "page": 1, "kind": "table",
        "bbox": [10, 120, 230, 200],
        "rows": [["Pin No.", "Name", "I/O", "Description"], ["1", "VCC", "Power", "Supply"], ["2", "GND", "Power", "Ground"]],
        "text": "Pin No. Name I/O Description\n1 VCC Power Supply\n2 GND Power Ground",
        "cells": [],
    })
    settings = Settings(mongo_uri="unused", work_dir=tmp_path, workers=1, llm_attempts=1,
                        quality="quality-v1", optimization="budget-v1", request_token_budget=20000)
    provider = FigureProvider(settings)
    engine = Engine(store, provider, settings)
    run_id = engine.create_run("parse", target["id"])
    run = store.get("runs", run_id)
    engine.map_retrieval(run)
    return engine, provider, store, run


def test_source_captions_define_units_without_part_specific_page_or_figure_ids(tmp_path):
    engine, _, _, run = figure_fixture(tmp_path)
    figures = source_app_figures(engine, run)
    assert [(u["number"], u["pages"]) for u in figures] == [("13", [2]), ("27", [3])]


def test_contents_and_caption_references_are_not_actual_application_figures(tmp_path):
    engine, _, store, run = figure_fixture(tmp_path)
    page = store.get("pages", "parse:p4")
    store.put("pages", {**page, "text": "Contents\nApplication Circuits\nFigure 80. Typical application ... 13"})
    for i, text in enumerate(["Contents", "Application Circuits", "Figure 80. Typical application ... 13"]):
        region = store.get("regions", f"parse:p4:l{i}")
        store.put("regions", {**region, "text": text})
    store.put("regions", {"id": "parse:p2:reference", "parse_id": "parse", "page": 2,
                           "kind": "line", "bbox": [10, 100, 230, 110],
                           "text": "Figure 98 illustrates the typical application configuration"})
    assert [u["number"] for u in source_app_figures(engine, run)] == ["13", "27"]


def test_complete_source_units_and_raw_pin_context_for_two_independent_readers(tmp_path):
    engine, provider, store, run = figure_fixture(tmp_path)
    original, allowed, visual, metrics, unresolved = read_figures(engine, run)
    independent, *_ = read_figures(engine, run, blind=True)
    assert len(original.circuits) == len(independent.circuits) == 2
    assert {c.id for c in original.claims} == {f"2.f{n}.{i}" for n in [13, 27] for i in range(1, 4)}
    assert {c.id for c in independent.claims} == {f"2.bf{n}.{i}" for n in [13, 27] for i in range(1, 4)}
    assert len({c.id for c in original.circuits}) == 2 and all(c.id.startswith("fig") for c in original.circuits)
    assert visual == [1, 2, 3] and unresolved == []
    assert metrics["figure_workflow_version"] and metrics["figure_count"] == 2
    assert "parse:p1:t1" in allowed
    assert len(provider.requests) == 4
    for request in provider.requests:
        number = request.task.rsplit(":", 1)[1]
        assert request.metrics["image_pages"] == [1, 2 if number == "13" else 3]
        assert "1 VCC Power Supply" in request.prompt
        assert "2 GND Power Ground" in request.prompt
        assert request.metrics["mandatory_overflow"] == {}
    assert len(store.find("figure_reads", {"run_id": run["id"]})) == 4


def test_engine_integration_reviews_each_figure_and_keeps_three_logical_stages(tmp_path):
    engine, provider, store, run = figure_fixture(tmp_path)
    record = engine.extract_retrieved(run, 2)
    engine.blind_retrieved(run, 2)
    reviewed = review_figures(engine, run, record)
    assert len(provider.requests) == 6
    assert reviewed["partition_count"] == 2
    assert reviewed["review_reconciliation"]["raw_review"]["supported_claim_ids"]
    assert set(reviewed["supported_claim_ids"]) == {c["id"] for c in record["section"]["claims"]}
    assert len(store.find("review_figure_parts", {"run_id": run["id"]})) == 2
    logical = [e for e in store.find("events", {"run_id": run["id"]}) if e["stage"] in {"extract:2", "blind:2", "review:2"}]
    assert {(e["stage"], e["status"]) for e in logical} == {
        (f"{role}:2", status) for role in ["extract", "blind", "review"] for status in ["started", "completed"]}
    for row in store.find("coverage", {"run_id": run["id"]}):
        if row["requirement_id"] in requirements(2):
            assert row["review_status"] == "reviewed"
    assert review_figures(engine, run, record) == reviewed
    assert len(provider.requests) == 6


def test_all_unit_preflights_precede_any_paid_call(tmp_path):
    engine, provider, store, run = figure_fixture(tmp_path)
    region = store.get("regions", "parse:p3:l3")
    store.put("regions", {**region, "text": "Source line that cannot be clipped " * 2000})
    # A new index is legitimate before mapping; remove the synthetic mapping guard
    # so the fixture tests the request preflight rather than source-mutability guard.
    engine._retrievers.clear()
    record = store.get("mapping_batches", f"{run['id']}:retrieval")
    from datasheet_parser.document_index import ensure_index

    index, _ = ensure_index(store, "parse", version=run["retrieval_config"]["index_version"])
    store.put("mapping_batches", {**record, "index_id": index["id"]})
    with pytest.raises(ProviderError, match="Atomic figure"):
        read_figures(engine, run)
    assert provider.requests == [] and store.find("figure_reads", {"run_id": run["id"]}) == []


def test_failed_second_unit_resumes_without_rereading_first(tmp_path):
    engine, provider, store, run = figure_fixture(tmp_path)
    provider.fail_on = "extract:2:figure:27"
    with pytest.raises(ProviderError, match="Synthetic"):
        read_figures(engine, run)
    assert len(store.find("figure_reads", {"run_id": run["id"]})) == 1
    provider.fail_on = None
    section, *_ = read_figures(engine, run)
    assert len(section.circuits) == 2
    assert [r.task for r in provider.requests] == ["extract:2:figure:13", "extract:2:figure:27", "extract:2:figure:27"]


def test_changed_source_image_does_not_reuse_old_plan_unit_records(tmp_path):
    engine, provider, store, run = figure_fixture(tmp_path)
    _, _, _, first, _ = read_figures(engine, run)
    bitmap = io.BytesIO()
    Image.new("RGB", (240, 320), "black").save(bitmap, format="PNG")
    page = store.get("pages", "parse:p3")
    store.put("pages", {**page, "image_artifact_id": store.artifact(bitmap.getvalue(), "image/png", "changed.png")})
    _, _, _, second, _ = read_figures(engine, run)
    assert first["figure_plan_hash"] != second["figure_plan_hash"]
    assert not set(first["figure_read_ids"]) & set(second["figure_read_ids"])
    # Identical first-unit requests may reuse Engine.call's exact fingerprint
    # checkpoint; the changed second image must trigger a new inference request.
    assert len(provider.requests) == 3


def test_unreadable_existing_figure_remains_ambiguous_and_never_expands(tmp_path):
    engine, provider, _, run = figure_fixture(tmp_path)
    provider.omit_circuit = "27"
    section, _, _, _, unresolved = read_figures(engine, run)
    assert len(section.circuits) == 1 and section.status == "ambiguous"
    assert set(unresolved) == {"2.circuits", "2.connections"}
    assert len(provider.requests) == 2
    assert any("Figure 27" in note for note in section.notes)
    record = engine.extract_retrieved(run, 2)
    assert "unresolved_evidence_request" not in {issue["code"] for issue in record["validation"]}


def test_review_rejects_nonoriginal_ids_without_persisting_a_part(tmp_path):
    engine, provider, store, run = figure_fixture(tmp_path)
    record = engine.extract_retrieved(run, 2)
    provider.bad_review_id = True
    with pytest.raises(ValueError, match="unknown claims"):
        review_figures(engine, run, record)
    assert store.find("review_figure_parts", {"run_id": run["id"]}) == []
