import copy
import json

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from datasheet_parser.config import Settings
from datasheet_parser.engine import Engine
from datasheet_parser.evaluation import claim_key, connections, evaluate, metrics
from datasheet_parser.models import Claim, ExtractionResult, SectionResult, Target
from datasheet_parser.providers import ExternalWorkerProvider, LLMRequest, PendingResponse, strict_schema
from datasheet_parser.requirements import requirements
from datasheet_parser.store import MemoryStore
from datasheet_parser.validation import validate_cross_sections, validate_section

TARGET = {
    "id": "target",
    "manufacturer": "Fixture",
    "part_number": "TEST1",
    "ordering_code": None,
    "package_name": "SOIC-8",
    "package_code": "S",
    "temperature_grade": None,
    "document_revision": "A",
    "evidence_ids": ["parse:p1"],
    "status": "supported",
    "reason": "Synthetic test fixture",
}


def empty_section(sid):
    return {
        "section_id": sid,
        "status": "not_found",
        "claims": [],
        "pins": [],
        "circuits": [],
        "notes": [],
        "findings": [
            {"requirement_id": r, "status": "not_found", "evidence_ids": [], "reason": "Checked source"}
            for r in requirements(sid)
        ],
    }


def fact(**overrides):
    return {
        "id": "3-voltage",
        "requirement_id": "3.voltages",
        "parameter": "Supply voltage",
        "kind": "quantity",
        "raw_value": "3 V",
        "value": None,
        "quantity": {"minimum": None, "typical": 3.0, "maximum": None, "unit": "V", "expression": None},
        "conditions": ["TA = 25 C"],
        "applies_to": ["target"],
        "evidence_ids": ["parse:p1"],
        "status": "present",
        "explanation": "",
        **overrides,
    }


def test_strict_schema_requires_nullable_fields():
    schema = strict_schema(SectionResult)
    Draft202012Validator.check_schema(schema)
    for value in [schema, *schema["$defs"].values()]:
        if value.get("type") == "object":
            assert set(value["required"]) == set(value["properties"])
            assert value["additionalProperties"] is False


def test_schema_does_not_admit_extra_properties():
    schema = strict_schema(SectionResult)
    row = empty_section(3)
    Draft202012Validator(schema).validate(row)
    row["invented"] = True
    assert list(Draft202012Validator(schema).iter_errors(row))


def test_nonfinite_numbers_rejected():
    row = fact()
    row["quantity"]["typical"] = float("nan")
    with pytest.raises(ValidationError):
        Claim.model_validate(row)


@pytest.mark.parametrize(
    "change,code",
    [
        ({"evidence_ids": []}, "missing_evidence"),
        ({"evidence_ids": ["other:p8"]}, "invalid_evidence"),
        ({"applies_to": ["other-package"]}, "wrong_target"),
        ({"requirement_id": "4.supply"}, "wrong_requirement"),
    ],
)
def test_invalid_claims_are_flagged(change, code):
    row = empty_section(3)
    row["claims"] = [fact(**change)]
    issues = validate_section(SectionResult.model_validate(row), "target", {"parse:p1"})
    assert code in {i["code"] for i in issues}


def test_bounds_order_is_checked():
    row = empty_section(3)
    item = fact()
    item["quantity"].update(minimum=5, maximum=4)
    row["claims"] = [item]
    assert "inverted_bounds" in {
        i["code"] for i in validate_section(SectionResult.model_validate(row), "target", {"parse:p1"})
    }


def test_missing_requirement_is_not_silently_absent():
    row = empty_section(3)
    row["findings"].pop()
    assert "requirement_coverage" in {
        i["code"] for i in validate_section(SectionResult.model_validate(row), "target", {"parse:p1"})
    }


def circuit():
    return {
        "id": "c1",
        "figure": "Figure 1",
        "conditions": [],
        "ic_ref": "U1",
        "components": [
            {
                "ref": "U1",
                "ref_origin": "source",
                "kind": "IC",
                "value": "TEST1",
                "terminals": ["1", "2"],
                "evidence_ids": ["parse:p1"],
            }
        ],
        "nets": [{"name": "VCC", "nodes": ["U1.1"], "evidence_ids": ["parse:p1"], "status": "supported"}],
        "explicitly_unconnected_pads": ["2"],
        "unobserved_pads": [],
        "evidence_ids": ["parse:p1"],
        "ambiguities": [],
    }


def test_nc_and_double_net_membership():
    row = empty_section(2)
    c = circuit()
    c["nets"].append(
        {
            "name": "GND",
            "nodes": ["U1.1", "U1.2", "U1.99"],
            "evidence_ids": ["parse:p1"],
            "status": "supported",
        }
    )
    row["circuits"] = [c]
    codes = {i["code"] for i in validate_section(SectionResult.model_validate(row), "target", {"parse:p1"})}
    assert {"nc_connected", "terminal_in_two_nets", "unknown_terminal"} <= codes


def test_cross_section_package_mismatch():
    a, b = empty_section(1), empty_section(2)
    b["circuits"] = [circuit()]
    assert validate_cross_sections([SectionResult.model_validate(a), SectionResult.model_validate(b)])


def test_equivalent_units_and_conditions():
    a, b = fact(), fact()
    b["quantity"].update(typical=3000, unit="mV")
    assert claim_key(a) == claim_key(b)
    b["conditions"] = ["TA = 85 C"]
    assert claim_key(a) != claim_key(b)


def test_net_names_do_not_affect_connections():
    a = {"circuits": [circuit()]}
    a["circuits"][0]["nets"][0]["nodes"].append("C1.1")
    b = copy.deepcopy(a)
    b["circuits"][0]["nets"][0]["name"] = "arbitrary_generated_name"
    assert connections(a) == connections(b)
    b["circuits"][0]["nets"][0]["nodes"].append("U1.2")
    assert metrics(connections(b), connections(a))["false_positive"] == 2


def test_empty_predictions_do_not_get_perfect_precision():
    assert metrics(set(), {1})["precision"] is None
    assert metrics(set(), {1})["recall"] == 0


def test_final_contract_requires_all_17_sections():
    with pytest.raises(ValidationError):
        ExtractionResult(
            document_id="doc",
            parse_id="parse",
            target=Target.model_validate(TARGET),
            sections=[],
            run_id="run",
            status="needs_review",
            issues=[],
            evidence=[],
            metadata={},
        )


def test_evaluation_rejects_wrong_package():
    prediction = {"document_id": "doc", "target": TARGET, "sections": []}
    reference = copy.deepcopy(prediction)
    reference["target"]["package_code"] = "OTHER"
    with pytest.raises(ValueError, match="package_code"):
        evaluate(prediction, reference)


class ScriptedProvider:
    """Explicit synthetic responses to exercise workflow mechanics, not extraction accuracy."""

    def __init__(self, fail_once=None):
        self.calls = []
        self.fail_once = fail_once

    def generate(self, request):
        self.calls.append(request.task)
        if request.task == self.fail_once:
            self.fail_once = None
            raise RuntimeError("simulated failure")
        if request.task.startswith("map:"):
            return {
                "mappings": [
                    {"requirement_id": r, "region_ids": ["parse:p1"], "reason": "fixture"}
                    for r in requirements()
                ]
            }
        sid = int(request.task.split(":")[1])
        if request.task.startswith(("extract:", "blind:")):
            return empty_section(sid)
        return {"supported_claim_ids": [], "issues": [], "reviewed_region_ids": ["parse:p1"]}


def setup_engine(tmp_path, provider):
    store = MemoryStore()
    store.put("parses", {"id": "parse", "document_id": "doc", "status": "complete", "page_count": 1})
    store.put(
        "pages",
        {
            "id": "parse:p1",
            "parse_id": "parse",
            "number": 1,
            "text": "Test document",
            "requires_visual_read": False,
            "image_artifact_id": store.artifact(b"fixture", "image/png", "p.png"),
        },
    )
    store.put(
        "regions", {"id": "parse:p1", "parse_id": "parse", "page": 1, "kind": "page", "text": "Test document"}
    )
    store.put("catalogs", {"id": "parse", "targets": [TARGET], "ambiguities": []})
    settings = Settings(mongo_uri="unused", work_dir=tmp_path)
    return Engine(store, provider, settings), store


def test_full_workflow_has_17_sections_and_coverage(tmp_path):
    engine, store = setup_engine(tmp_path, ScriptedProvider())
    run_id = engine.create_run("parse", "target")
    engine.resume(run_id)
    result = engine.export(run_id)
    assert len(result.sections) == 17
    assert len(store.find("coverage", {"run_id": run_id})) == len(requirements())
    assert all(c["review_status"] == "reviewed" for c in store.find("coverage", {"run_id": run_id}))


def test_failure_is_resumable_without_repeating_completed_stage(tmp_path):
    provider = ScriptedProvider("blind:2")
    engine, store = setup_engine(tmp_path, provider)
    run_id = engine.create_run("parse", "target")
    with pytest.raises(RuntimeError):
        engine.resume(run_id, [1, 2])
    assert store.get("runs", run_id)["status"] == "failed"
    engine.resume(run_id, [1, 2])
    assert provider.calls.count("extract:1") == 1
    assert provider.calls.count("extract:2") == 1
    assert provider.calls.count("blind:2") == 2
    assert store.get("runs", run_id)["status"] == "partial"


def test_final_export_refuses_partial_run(tmp_path):
    engine, _ = setup_engine(tmp_path, ScriptedProvider())
    run_id = engine.create_run("parse", "target")
    engine.resume(run_id, [3], review=False)
    with pytest.raises(ValueError, match="1/17"):
        engine.export(run_id)


def test_blind_reader_never_receives_first_extraction(tmp_path):
    class Inspector(ScriptedProvider):
        def generate(self, request):
            if request.task.startswith("blind:"):
                assert "ORIGINAL:" not in request.prompt
                assert "UNIQUE_FIRST_ANSWER" not in request.prompt
            result = super().generate(request)
            if request.task.startswith("extract:"):
                result["notes"] = ["UNIQUE_FIRST_ANSWER"]
            return result

    engine, _ = setup_engine(tmp_path, Inspector())
    engine.resume(engine.create_run("parse", "target"), [3])


def test_external_provider_requires_schema_valid_response(tmp_path):
    settings = Settings(mongo_uri="unused", work_dir=tmp_path)
    provider = ExternalWorkerProvider(settings)
    request = LLMRequest("test", "Extract", strict_schema(SectionResult), [])
    with pytest.raises(PendingResponse):
        provider.generate(request)
    folders = list((tmp_path / "llm" / "external").iterdir())
    (folders[0] / "submitted.json").write_text(json.dumps(empty_section(3)))
    assert provider.generate(request)["section_id"] == 3


def test_artifact_content_addressing():
    store = MemoryStore()
    a = store.artifact(b"same", "text/plain", "a")
    b = store.artifact(b"same", "text/plain", "b")
    assert a == b
    assert store.read_artifact(a) == b"same"


def test_packet_does_not_claim_missing_images_were_attached(tmp_path):
    engine, _ = setup_engine(tmp_path, ScriptedProvider())
    text, images, _ = engine.packet("parse", [1], image_pages=[])
    assert "image_attached=False" in text
    assert images == []


def test_netlist_requires_explicit_disposition_for_every_known_pad():
    from datasheet_parser.models import Circuit, Pin

    a, b = SectionResult.model_validate(empty_section(1)), SectionResult.model_validate(empty_section(2))
    a.pins = [
        Pin(
            pad=str(n),
            name="P",
            electrical_type="bidirectional",
            functions=[],
            conditions=[],
            connection_requirements=[],
            evidence_ids=["parse:p1"],
        )
        for n in range(1, 4)
    ]
    b.circuits = [Circuit.model_validate(circuit())]
    assert "unaccounted_ic_pads" in {i["code"] for i in validate_cross_sections([a, b])}
    b.circuits[0].unobserved_pads = ["3"]
    assert not validate_cross_sections([a, b])


def test_netlist_variants_preserve_conditions():
    a = {"circuits": [circuit()]}
    a["circuits"][0]["nets"][0]["nodes"].append("C1.1")
    b = copy.deepcopy(a)
    b["circuits"][0]["conditions"] = ["Alternative connection"]
    assert connections(a).isdisjoint(connections(b))


def test_absent_section_cannot_contain_present_facts():
    row = empty_section(3)
    row["claims"] = [fact()]
    codes = {i["code"] for i in validate_section(SectionResult.model_validate(row), "target", {"parse:p1"})}
    assert {"contradictory_disposition", "contradictory_section_status"} <= codes


def test_unknown_usage_is_not_zero():
    from datasheet_parser.telemetry import summarize_events

    result = summarize_events(
        [
            {
                "stage": "extract:1",
                "status": "completed",
                "time": "2026-01-01",
                "telemetry": {"usage": None, "elapsed_seconds": 1},
            }
        ]
    )
    assert result["completed_calls_with_usage"] == 0
    assert result["reported_usage_totals"] == {}
    assert result["billed_cost"] is None


def test_empty_review_cannot_pass_as_source_checked(tmp_path):
    class EmptyReview(ScriptedProvider):
        def generate(self, request):
            result = super().generate(request)
            if request.task.startswith("review:"):
                result["reviewed_region_ids"] = []
            return result

    engine, store = setup_engine(tmp_path, EmptyReview())
    run_id = engine.create_run("parse", "target")
    with pytest.raises(ValueError, match="Review contains"):
        engine.resume(run_id, [3])
    assert not store.find("reviews", {"run_id": run_id})


def test_raw_only_text_facts_are_not_collapsed():
    a = fact(kind="text", quantity=None, value=None, raw_value="Mode A")
    b = {**a, "raw_value": "Mode B"}
    assert claim_key(a) != claim_key(b)


def test_wrong_component_value_fails_even_when_connectivity_matches():
    reference = {"document_id": "doc", "target": TARGET, "sections": [empty_section(2)]}
    reference["sections"][0]["circuits"] = [circuit()]
    prediction = copy.deepcopy(reference)
    prediction["sections"][0]["circuits"][0]["components"][0]["value"] = "WRONG-PART"
    result = evaluate(prediction, reference)
    assert result["component_values_and_terminals"]["false_positive"] == 1


def test_explained_absence_does_not_require_fabricated_evidence():
    row = empty_section(3)
    row["claims"] = [
        fact(
            kind="text",
            quantity=None,
            raw_value=None,
            value=None,
            status="not_found",
            evidence_ids=[],
            explanation="Checked source; no value stated",
        )
    ]
    assert not validate_section(SectionResult.model_validate(row), "target", {"parse:p1"})


def test_provider_failure_has_telemetry_and_durable_artifacts(tmp_path):
    from datasheet_parser.providers import ProviderError, RecordedProvider

    class BrokenProvider(RecordedProvider):
        def execute(self, request, folder, images):
            raise RuntimeError("simulated transport failure")

    provider = BrokenProvider(Settings(mongo_uri="unused", work_dir=tmp_path))
    engine, store = setup_engine(tmp_path, provider)
    with pytest.raises(ProviderError):
        engine.call("run", "extract:1", SectionResult, "Extract from the fixture", [])
    event = next(e for e in store.find("events") if e["status"] == "failed")
    assert event["telemetry"]["status"] == "failed"
    assert event["telemetry"]["usage"] is None
    assert "failure.json" in event["artifacts"]


def test_successful_model_call_survives_database_failure_without_new_inference(tmp_path):
    provider = ScriptedProvider()
    engine, store = setup_engine(tmp_path, provider)
    original_put = store.put
    fail = True

    def flaky_put(collection, record):
        nonlocal fail
        if fail and collection == "events" and record.get("status") == "completed":
            fail = False
            raise RuntimeError("database connection dropped after inference")
        original_put(collection, record)

    store.put = flaky_put
    with pytest.raises(RuntimeError, match="database connection"):
        engine.call("run", "extract:3", SectionResult, "Fixture source", [])
    result = engine.call("run", "extract:3", SectionResult, "Fixture source", [])
    assert result.section_id == 3
    assert provider.calls == ["extract:3"]
    engine.call("other-run", "extract:3", SectionResult, "Fixture source", [])
    assert provider.calls == ["extract:3", "extract:3"]  # No hidden cache across evaluation runs.


def test_run_command_interactively_selects_and_exports_all_17_sections(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from datasheet_parser import cli

    engine, store = setup_engine(tmp_path, ScriptedProvider())
    monkeypatch.setattr(cli, "context", lambda *args: (engine.settings, store, engine))
    monkeypatch.setattr(cli, "ingest_pdf", lambda *args: "parse")
    pdf = tmp_path / "fixture.pdf"
    pdf.write_bytes(b"synthetic fixture; ingestion is tested separately")
    output = tmp_path / "result.json"
    result = CliRunner().invoke(cli.app, ["run", str(pdf), "--output", str(output)], input="1\n")
    assert result.exit_code == 0, result.output
    exported = ExtractionResult.model_validate_json(output.read_text(encoding="utf-8"))
    assert len(exported.sections) == 17
    assert len(store.find("reviews")) == 17
    assert "run_id=" in result.output


def test_run_failure_keeps_partial_work_and_resume_can_export(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from datasheet_parser import cli

    provider = ScriptedProvider("blind:2")
    engine, store = setup_engine(tmp_path, provider)
    monkeypatch.setattr(cli, "context", lambda *args: (engine.settings, store, engine))
    monkeypatch.setattr(cli, "ingest_pdf", lambda *args: "parse")
    monkeypatch.setattr(cli, "make_provider", lambda settings: provider)
    pdf, output = tmp_path / "fixture.pdf", tmp_path / "result.json"
    pdf.write_bytes(b"synthetic fixture")
    runner = CliRunner()
    failed = runner.invoke(cli.app, ["run", str(pdf), "-o", str(output), "--target", "target"])
    assert failed.exit_code == 1 and not output.exists()
    run_id = store.find("runs")[0]["id"]
    resumed = runner.invoke(cli.app, ["resume", run_id, "--output", str(output)])
    assert resumed.exit_code == 0, resumed.output
    assert len(ExtractionResult.model_validate_json(output.read_text()).sections) == 17
    assert provider.calls.count("extract:1") == 1


def test_run_cannot_overwrite_source_pdf(tmp_path):
    from typer.testing import CliRunner

    from datasheet_parser.cli import app

    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"original")
    result = CliRunner().invoke(app, ["run", str(pdf), "--output", str(pdf)])
    assert result.exit_code == 1
    assert pdf.read_bytes() == b"original"
