"""Offline end-to-end regressions for the optimized contracts and source review path.

These fixtures exercise the actual scheduler, packet builder, recorded request guard,
durable outputs and public export. They are not model-accuracy or latency benchmarks.
"""

import copy
import io
import json

import pytest
from PIL import Image

from datasheet_parser.config import Settings
from datasheet_parser.contracts import cited_regions
from datasheet_parser.engine import Engine
from datasheet_parser.errors import ReviewBudgetError
from datasheet_parser.models import Review
from datasheet_parser.providers import ProviderError, RecordedProvider
from datasheet_parser.requirements import SECTIONS, requirements
from datasheet_parser.store import MemoryStore
from datasheet_parser.token_budget import request_input_metrics

TARGET = {
    "id": "selected-part",
    "manufacturer": "Fixture",
    "part_number": "TEST1C",
    "ordering_code": "TEST1CD",
    "package_name": "SOIC-16",
    "package_code": "D",
    "temperature_grade": "C",
    "document_revision": "A",
    "evidence_ids": ["parse:p1"],
    "status": "supported",
    "reason": "Synthetic source explicitly identifies the selected ordering code",
}


def source(texts):
    store = MemoryStore()
    store.put(
        "parses", {"id": "parse", "document_id": "doc", "status": "complete", "page_count": len(texts)}
    )
    for n, text in enumerate(texts, 1):
        bitmap = io.BytesIO()
        Image.new("RGB", (240, 320), (255, 255, 250 - n)).save(bitmap, format="PNG")
        artifact = store.artifact(bitmap.getvalue(), "image/png", f"fixture-{n}.png")
        common = {"parse_id": "parse", "page": n, "image_artifact_id": artifact}
        store.put(
            "pages",
            {
                "id": f"parse:p{n}",
                "parse_id": "parse",
                "number": n,
                "text": text,
                "requires_visual_read": not bool(text),
                "image_artifact_id": artifact,
                "width": 240,
                "height": 320,
            },
        )
        store.put(
            "regions",
            {"id": f"parse:p{n}", **common, "kind": "page", "text": text, "bbox": [0, 0, 240, 320]},
        )
        for i, line in enumerate(text.splitlines()):
            store.put(
                "regions",
                {
                    "id": f"parse:p{n}:l{i}",
                    **common,
                    "kind": "line",
                    "text": line,
                    "bbox": [10, 10 + i * 30, 230, 30 + i * 30],
                },
            )
    store.put("catalogs", {"id": "parse", "targets": [TARGET], "ambiguities": []})
    return store


@pytest.fixture(autouse=True)
def no_network_provider_or_tokenizer(monkeypatch):
    def forbidden_factory(settings):
        raise AssertionError("This integration fixture must never create an external provider")

    monkeypatch.setattr("datasheet_parser.engine.make_provider", forbidden_factory)
    # Use the documented offline upper bound and avoid any tokenizer-data download.
    monkeypatch.setattr("datasheet_parser.token_budget._encoding", lambda: None)
    from datasheet_parser.token_budget import estimate_text_tokens

    estimate_text_tokens.cache_clear()
    yield
    estimate_text_tokens.cache_clear()


class FixtureProvider(RecordedProvider):
    name = "offline-compact-v2"

    def __init__(self, settings, store, *, status="present", searches=()):
        super().__init__(settings)
        self.store = store
        self.requests = []
        self.status = status
        self.searches = list(searches)
        self.citations = {}
        self.run_id = None

    def execute(self, request, folder, images):
        self.requests.append(request)
        role, section_text = request.task.split(":")[:2]
        sid = int(section_text)
        packet = self.store.get("evidence_packets", f"{self.run_id}:{sid}:{role}:0")
        aliases = packet["aliases"]
        assert aliases, "Fixture must supply source evidence rather than manufacture citations"
        if role == "blind":
            assert "ORIGINAL:" not in request.prompt
            assert "EXTRACT_ONLY_VALUE" not in request.prompt
        if role == "review":
            original = json.loads(request.prompt.split("ORIGINAL: ", 1)[1].split("\nINDEPENDENT: ", 1)[0])
            claims = original.get("claims", [])
            if "claim_tables" in original:
                claims = [dict(zip(table["columns"], row, strict=True))
                          for table in original["claim_tables"] for row in table["rows"]]
            return {
                "supported_claim_ids": [claim["id"] for claim in claims],
                "issues": [],
                "reviewed_region_ids": list(aliases),
            }, {"usage": None, "synthetic_fixture": True}

        region = self.citations.get(role)
        citation = next(alias for alias, rid in aliases.items() if rid == region) if region else next(iter(aliases))
        claims = []
        if self.status == "present":
            for req in requirements(sid):
                if req == "4.supply":
                    claim = {
                        "kind": "quantity",
                        "raw_value": "3.3 V",
                        "quantity": {
                            "bounds": [{"role": "typical", "value": 3.3}],
                            "unit": "V",
                            "expression": None,
                        },
                    }
                else:
                    claim = {
                        "kind": "text",
                        "value": "EXTRACT_ONLY_VALUE" if role == "extract" else "INDEPENDENT_VALUE",
                    }
                claims.append(
                    {
                        **claim,
                        "requirement_id": req,
                        "parameter": requirements(sid)[req],
                        "conditions": ["Selected TEST1CD / package D / grade C"],
                        "evidence_ids": [citation],
                        "status": "present",
                        "reason": "",
                    }
                )
        result = {
            "status": self.status,
            "claims": claims,
            "findings": [
                {
                    "requirement_id": req,
                    "status": self.status,
                    "evidence_ids": [] if self.status == "not_found" else [citation],
                    "reason": "Source fixture checked" if self.status != "present" else "",
                }
                for req in requirements(sid)
            ],
            "search_requests": self.searches,
        }
        if sid == 1:
            result["pins"] = []
        if sid == 2:
            result["circuits"] = []
        return result, {"usage": None, "synthetic_fixture": True}


def engine(tmp_path, store, **provider_options):
    settings = Settings(
        mongo_uri="unused",
        work_dir=tmp_path,
        workers=1,
        llm_attempts=1,
        retrieval_top_k=1,
        quality="baseline",
        optimization="budget-v1",
        packet_token_budget=6000,
        request_token_budget=20000,
    )
    provider = FixtureProvider(settings, store, **provider_options)
    parser = Engine(store, provider, settings)
    run_id = parser.create_run("parse", TARGET["id"])
    provider.run_id = run_id
    return parser, provider, run_id


def test_optimized_run_all_17_sections_and_51_findings_export_and_resume(tmp_path):
    texts = [
        "\n".join(
            [SECTIONS[sid][0].replace("_", " ")]
            + [f"{req}: {description}; TEST1CD package D grade C" for req, description in requirements(sid).items()]
            + (["VCC typical = 3.3 V"] if sid == 4 else [])
        )
        for sid in range(1, 18)
    ]
    store = source(texts)
    parser, provider, run_id = engine(tmp_path, store)
    parser.resume(run_id)
    exported = parser.export(run_id)

    assert exported.schema_version == "1.0"
    assert [section.section_id for section in exported.sections] == list(range(1, 18))
    assert exported.metadata["contract_version"] == "compact-v2"
    assert exported.metadata["packet_version"] == "budget-v1"
    assert exported.metadata["scheduler_version"] == "dag-v1"
    assert len(provider.requests) == 51
    assert {request.task for request in provider.requests} == {
        f"{role}:{sid}" for role in ("extract", "blind", "review") for sid in range(1, 18)
    }
    coverage = store.find("coverage", {"run_id": run_id})
    assert len(coverage) == 51
    assert {row["requirement_id"] for row in coverage} == set(requirements())
    assert all(row["extraction_status"] == "present" and row["review_status"] == "reviewed" for row in coverage)
    for row in store.find("blind_reads", {"run_id": run_id}):
        assert all(claim["value"] != "EXTRACT_ONLY_VALUE" for claim in row["section"]["claims"])
    supply = next(claim for claim in exported.sections[3].claims if claim.requirement_id == "4.supply")
    assert supply.raw_value == "3.3 V"
    assert supply.quantity.typical == 3.3
    assert supply.quantity.minimum is None
    assert supply.quantity.maximum is None
    assert supply.applies_to == [TARGET["id"]]

    assert len(store.find("evidence_packets", {"run_id": run_id})) == 51
    for sid in range(1, 18):
        original = store.get("results", f"{run_id}:{sid}")
        blind = store.get("blind_reads", f"{run_id}:{sid}:blind")
        packet = store.get("evidence_packets", f"{run_id}:{sid}:review:0")
        assert cited_regions(original["section"]) | cited_regions(blind["section"]) <= set(packet["aliases"].values())

    completed_calls = [
        event for event in store.find("events", {"run_id": run_id})
        if event.get("kind") == "llm" and event["status"] == "completed"
    ]
    assert len(completed_calls) == 51
    for event in completed_calls:
        request = next(request for request in provider.requests if request.task == event["stage"])
        logged = event["telemetry"]["request_metrics"]
        actual = request_input_metrics(request.prompt, request.schema, request.images)
        assert logged["input_tokens_estimate"] == actual["input_tokens_estimate"]
        assert logged["schema_tokens_estimate"] > 0
        assert logged["input_tokens_estimate"] >= logged["request_text_tokens_estimate"]
        assert logged["input_tokens_estimate"] <= 20000

    parser.resume(run_id)
    assert len(provider.requests) == 51  # Reuse durable stages, never replay inference.


def test_review_keeps_both_cited_sources_without_carrying_unused_supplied_image(tmp_path, monkeypatch):
    store = source([
        "Recommended operating conditions\nVCC minimum 3 V",
        "Recommended operating conditions\nVCC maximum 5.5 V",
        "",  # An unclassified image deliberately included in both source-read fixtures.
    ])
    parser, provider, run_id = engine(tmp_path, store)
    run = store.get("runs", run_id)
    _, retriever = parser.retrieval_context(run)
    first = next(c["id"] for c in retriever.chunks if "parse:p1:l1" in c["region_ids"])
    second = next(c["id"] for c in retriever.chunks if "parse:p2:l1" in c["region_ids"])
    unused_image = next(c["id"] for c in retriever.chunks if c["page"] == 3 and c["visual"])
    active = {"role": "extract"}

    def fixture_selection(req, target, **options):
        selected = {
            "extract": {first, unused_image},
            "blind": {second, unused_image},
            "review": {first},
        }[active["role"]]
        return selected, {cid: {"route": "seed", "rank_score": 10} for cid in selected}

    original_packet = parser.retrieval_packet

    def packet_for_role(run, sid, **options):
        active["role"] = options.get("role") or ("blind" if options.get("complement") else "extract")
        return original_packet(run, sid, **options)

    monkeypatch.setattr(retriever, "select", fixture_selection)
    monkeypatch.setattr(parser, "retrieval_packet", packet_for_role)
    provider.citations = {"extract": "parse:p1:l1", "blind": "parse:p2:l1"}
    parser.resume(run_id, [4])

    record = store.get("results", f"{run_id}:4")
    blind = store.get("blind_reads", f"{run_id}:4:blind")
    packet = store.get("evidence_packets", f"{run_id}:4:review:0")
    assert "parse:p3" in record["supplied_region_ids"]
    assert "parse:p3" in blind["supplied_region_ids"]
    assert set(packet["aliases"].values()) >= {"parse:p1:l1", "parse:p2:l1"}
    assert "parse:p3" not in packet["aliases"].values()
    assert len(next(request for request in provider.requests if request.task == "extract:4").images) == 1
    assert len(next(request for request in provider.requests if request.task == "blind:4").images) == 1
    review_request = next(request for request in provider.requests if request.task == "review:4")
    assert review_request.images == []
    original_extra = json.loads(
        review_request.prompt.split("ORIGINAL_ADDITIONAL_SOURCE: ", 1)[1].split("\n", 1)[0]
    )
    independent_extra = json.loads(
        review_request.prompt.split("INDEPENDENT_ADDITIONAL_SOURCE: ", 1)[1].split("\n", 1)[0]
    )
    original_extra_regions = {packet["aliases"][alias] for alias in original_extra}
    independent_extra_regions = {packet["aliases"][alias] for alias in independent_extra}
    assert "parse:p2:l1" in original_extra_regions
    assert "parse:p1:l1" not in original_extra_regions
    assert "parse:p1:l1" in independent_extra_regions
    assert "parse:p2:l1" not in independent_extra_regions
    assert "parse:p3" not in original_extra_regions | independent_extra_regions
    assert packet["metrics"]["role"] == "review"
    assert store.get("evidence_packets", f"{run_id}:4:blind:0")["metrics"]["role"] == "blind"


@pytest.mark.parametrize("status", ["not_found", "ambiguous"])
def test_uncertain_findings_and_search_requests_do_not_trigger_expansion(tmp_path, status):
    store = source(["Absolute maximum ratings\nVCC limit not specified in this fixture"])
    parser, provider, run_id = engine(tmp_path, store, status=status, searches=["Missing Table 9"])
    parser.resume(run_id, [3])
    assert [request.task for request in provider.requests] == ["extract:3", "blind:3", "review:3"]
    packets = store.find("evidence_packets", {"run_id": run_id})
    assert len(packets) == 3
    assert all(row["id"].endswith(":0") for row in packets)
    assert not any(row["metrics"]["broad_text"] for row in packets)
    assert store.get("results", f"{run_id}:3")["section"]["notes"]
    assert store.get("blind_reads", f"{run_id}:3:blind")["section"]["notes"]


def test_serialized_review_overflow_fails_before_fake_or_real_inference(tmp_path):
    store = source(["Recommended operating conditions\nVCC typical 3.3 V"])
    parser, provider, run_id = engine(tmp_path, store)
    run = store.get("runs", run_id)
    parser.map_retrieval(run)
    record = parser.extract_retrieved(run, 4)
    parser.blind_retrieved(run, 4)
    oversized = copy.deepcopy(record)
    oversized["section"]["claims"][0]["explanation"] = "Preserved source condition: " + "A" * 22000
    before = len(provider.requests)
    with pytest.raises(ReviewBudgetError) as failure:
        parser.review_retrieved(run, 4, oversized)
    assert failure.value.estimate > failure.value.limit == 20000
    assert len(provider.requests) == before
    assert not store.find("reviews", {"run_id": run_id})
    assert not any("expanded" in request.task for request in provider.requests)
    packet = store.get("evidence_packets", f"{run_id}:4:review:0")
    assert packet["metrics"]["mandatory_overflow"]
    assert packet["metrics"]["budget"]["total_tokens"] == 0


def test_draft_compaction_fits_existing_cap_without_changing_review_sources(tmp_path):
    store = source(["Recommended operating conditions\nVCC typical 3.3 V"])
    parser, provider, run_id = engine(tmp_path, store)
    run = store.get("runs", run_id)
    run["retrieval_config"]["request_token_budget"] = 100000
    parser.map_retrieval(run)
    record = copy.deepcopy(parser.extract_retrieved(run, 4))
    parser.blind_retrieved(run, 4)
    template = record["section"]["claims"][0]
    record["section"]["claims"] = []
    for i in range(100):
        claim = copy.deepcopy(template)
        claim["id"] = f"4-large-{i}"
        claim["conditions"].append("Printed fixture qualifier: preserve all units and test conditions. " * 3)
        record["section"]["claims"].append(claim)
    parser.review_retrieved(run, 4, record)
    ordinary = provider.requests[-1]
    previous = store.get("evidence_packets", f"{run_id}:4:review:0")
    before = request_input_metrics(ordinary.prompt, ordinary.schema, ordinary.images)["input_tokens_estimate"]
    del store.records["reviews"][f"{run_id}:4"]
    run["retrieval_config"]["request_token_budget"] = before - 300
    parser.review_retrieved(run, 4, record)
    packed = provider.requests[-1]
    packet = store.get("evidence_packets", f"{run_id}:4:review:0")
    actual = request_input_metrics(packed.prompt, packed.schema, packed.images)["input_tokens_estimate"]
    assert actual <= run["retrieval_config"]["request_token_budget"] < before
    assert packet["metrics"]["draft_encoding"].startswith("columnar-v1")
    assert packet["chunk_ids"] == previous["chunk_ids"] and packet["aliases"] == previous["aliases"]
    assert packed.images == ordinary.images
    assert set(store.get("reviews", f"{run_id}:4")["supported_claim_ids"]) == {
        c["id"] for c in record["section"]["claims"]}


def test_provider_guard_counts_final_prompt_and_schema_before_execute(tmp_path):
    store = source(["Recommended operating conditions\nVCC typical 3.3 V"])
    parser, provider, run_id = engine(tmp_path, store)
    with pytest.raises(ProviderError) as failure:
        parser.call(
            run_id, "review:4", Review, "Original draft: " + "A" * 22000, [],
            request_metrics={"request_token_budget": 20000},
        )
    assert failure.value.code == "request_budget_exceeded"
    assert provider.requests == []
    failure_event = next(
        event for event in reversed(store.find("events", {"run_id": run_id}))
        if event["stage"] == "review:4" and event["status"] == "failed"
    )
    metrics = failure_event["telemetry"]["request_metrics"]
    assert metrics["prompt_text_tokens_estimate"] > 22000
    assert metrics["input_tokens_estimate"] > metrics["request_token_budget"]
    assert metrics["schema_tokens_estimate"] > 0


def test_review_reservation_overflow_uses_complete_request_preflight_without_dropping_sources(
    tmp_path, monkeypatch,
):
    store = source(["Recommended operating conditions\nVCC typical 3.3 V"])
    parser, provider, run_id = engine(tmp_path, store)
    run = store.get("runs", run_id)
    parser.map_retrieval(run)
    original = parser.extract_retrieved(run, 4)
    independent = parser.blind_retrieved(run, 4)
    before = len(provider.requests)
    ordinary_reservation = parser.request_reserved_tokens

    def conservative_reservation(run, sid, model, answers=()):
        if model is Review:
            # Estimation leaves one token for source, although the actual full request fits.
            return run["retrieval_config"]["request_token_budget"] - 1
        return ordinary_reservation(run, sid, model, answers)

    monkeypatch.setattr(parser, "request_reserved_tokens", conservative_reservation)
    parser.review_retrieved(run, 4, original)

    assert len(provider.requests) == before + 1
    request = provider.requests[-1]
    assert request.task == "review:4"
    actual = request_input_metrics(request.prompt, request.schema, request.images)
    assert actual["input_tokens_estimate"] <= run["retrieval_config"]["request_token_budget"] == 20000
    assert actual["schema_tokens_estimate"] > 0
    assert "Output token target:" in request.prompt

    packet = store.get("evidence_packets", f"{run_id}:4:review:0")
    required = cited_regions(original["section"]) | cited_regions(independent["section"])
    assert required <= set(packet["aliases"].values())
    assert packet["metrics"]["budget"]["total_tokens"] == 1
    assert packet["metrics"]["reservation_warning"]
    assert not packet["metrics"]["mandatory_overflow"]
    assert store.get("reviews", f"{run_id}:4")
    assert len(store.find("evidence_packets", {"run_id": run_id})) == 3


def test_review_retains_union_of_seven_cited_images_from_bounded_first_readers(tmp_path, monkeypatch):
    store = source([""] * 7)
    parser, provider, run_id = engine(tmp_path, store)
    run = store.get("runs", run_id)
    _, retriever = parser.retrieval_context(run)
    image_chunks = {c["page"]: c["id"] for c in retriever.chunks if c["visual"]}
    assert set(image_chunks) == set(range(1, 8))
    active = {"role": "extract"}

    def fixture_selection(req, target, **options):
        pages = {"extract": range(1, 5), "blind": range(5, 8), "review": [1]}[active["role"]]
        selected = {image_chunks[page] for page in pages}
        return selected, {cid: {"route": "seed", "rank_score": 10} for cid in selected}

    ordinary_packet = parser.retrieval_packet

    def packet_for_role(run, sid, **options):
        active["role"] = options.get("role") or ("blind" if options.get("complement") else "extract")
        return ordinary_packet(run, sid, **options)

    monkeypatch.setattr(retriever, "select", fixture_selection)
    monkeypatch.setattr(parser, "retrieval_packet", packet_for_role)
    parser.map_retrieval(run)
    original = parser.extract_retrieved(run, 3)
    independent = parser.blind_retrieved(run, 3)

    # Record facts supported across each reader's actual supplied pages, not extra source.
    original["section"]["claims"][0]["evidence_ids"] = [f"parse:p{n}" for n in range(1, 5)]
    independent["section"]["claims"][0]["evidence_ids"] = [f"parse:p{n}" for n in range(5, 8)]
    store.put("results", original)
    store.put("blind_reads", independent)
    parser.review_retrieved(run, 3, original)

    assert [request.task for request in provider.requests] == ["extract:3", "blind:3", "review:3"]
    assert [len(request.images) for request in provider.requests] == [4, 3, 7]
    packets = store.find("evidence_packets", {"run_id": run_id})
    assert len(packets) == 3
    assert all(packet["id"].endswith(":0") and not packet["metrics"]["broad_text"] for packet in packets)
    for role in ["extract", "blind"]:
        packet = store.get("evidence_packets", f"{run_id}:3:{role}:0")
        assert packet["metrics"]["image_count"] <= packet["metrics"]["budget"]["max_images"] == 6

    packet = store.get("evidence_packets", f"{run_id}:3:review:0")
    expected = {f"parse:p{n}" for n in range(1, 8)}
    assert cited_regions(original["section"]) | cited_regions(independent["section"]) == expected
    assert expected <= set(packet["aliases"].values())
    assert packet["metrics"]["image_count"] == 7
    assert packet["metrics"]["budget"]["max_images"] == 12
    assert not packet["metrics"]["mandatory_overflow"]
    review_request = provider.requests[-1]
    actual = request_input_metrics(review_request.prompt, review_request.schema, review_request.images)
    assert actual["input_tokens_estimate"] <= run["retrieval_config"]["request_token_budget"] == 20000
    assert store.get("reviews", f"{run_id}:3")
