"""Synthetic source-grounding tests; these do not measure model extraction accuracy."""

import copy
import io
import re

import pytest
from PIL import Image
from test_core import TARGET

from datasheet_parser.config import Settings
from datasheet_parser.document_index import ensure_index
from datasheet_parser.engine import Engine
from datasheet_parser.evidence_packet import EvidencePacket, build_packet
from datasheet_parser.requirements import requirements
from datasheet_parser.retrieval import Retriever
from datasheet_parser.store import MemoryStore


def source():
    store = MemoryStore()
    image = io.BytesIO()
    Image.new("RGB", (240, 320), "white").save(image, format="PNG")
    blob = store.artifact(image.getvalue(), "image/png", "fixture.png")
    texts = {
        1: "Pin configuration\n1 VCC Supply\n2 GND Ground",
        2: "Typical application schematic\nFigure 2. Capacitor circuit\nSee Table 9 for capacitor values",
        3: "PACKAGE OUTLINE\nPW0016A\nTSSOP mechanical dimensions",
        4: "",
        5: "Table 9. Capacitor values\nC1 100 nF\n(1) Only at VCC = 3.3 V",
    }
    store.put("parses", {"id": "parse", "document_id": "doc", "status": "complete", "page_count": 5})
    for n, text in texts.items():
        common = {"parse_id": "parse", "page": n, "image_artifact_id": blob}
        store.put(
            "pages",
            {
                "id": f"parse:p{n}",
                "parse_id": "parse",
                "number": n,
                "text": text,
                "requires_visual_read": n == 4,
                "image_artifact_id": blob,
                "width": 240,
                "height": 320,
            },
        )
        store.put(
            "regions", {"id": f"parse:p{n}", **common, "kind": "page", "text": text, "bbox": [0, 0, 240, 320]}
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
        if n == 5:
            store.put(
                "regions",
                {
                    "id": "parse:p5:t1",
                    **common,
                    "kind": "table",
                    "text": '[["C", "Value"], ["C1", "100 nF"]]',
                    "rows": [["C", "Value"], ["C1", "100 nF"]],
                    "bbox": [10, 38, 230, 70],
                },
            )
    return store


CONFIG = {
    "top_k": 1,
    "embedding_model": None,
    "reranker_model": None,
    "local_only": True,
    "text_budget": 60000,
}


def test_distant_reference_keeps_table_header_and_footnote(tmp_path):
    store = source()
    manifest, chunks = ensure_index(store, "parse")
    retriever = Retriever(store, manifest, chunks, CONFIG)
    seeds = {c["id"] for c in chunks if c["page"] == 2}
    selected = retriever.expand(seeds, TARGET)
    packet = build_packet(store, Settings(mongo_uri="unused", work_dir=tmp_path), "parse", chunks, selected)
    assert "100 nF" in packet.text
    assert "Only at VCC = 3.3 V" in packet.text
    assert "parse:p5:t1" in packet.allowed
    assert 3 not in packet.image_pages  # Known PW outline cannot establish selected S package dimensions.


def test_canonical_regions_are_serialized_once_and_page_text_is_not_duplicated(tmp_path):
    store = source()
    _, chunks = ensure_index(store, "parse")
    packet = build_packet(
        store, Settings(mongo_uri="unused", work_dir=tmp_path), "parse", chunks, {c["id"] for c in chunks}
    )
    assert packet.text.count("Only at VCC = 3.3 V") == 1
    assert len(packet.aliases) == len(set(packet.aliases.values()))
    assert len(packet.image_pages) == len(packet.images) == len(set(packet.image_pages))


def test_unknown_visual_pages_remain_retrievable_for_every_requirement():
    store = source()
    manifest, chunks = ensure_index(store, "parse")
    retriever = Retriever(store, manifest, chunks, CONFIG)
    for req in requirements():
        selected, _ = retriever.select(req, TARGET)
        assert any(retriever.by_id[c]["page"] == 4 for c in selected)


def test_index_is_reused_and_invalidated_by_source_change():
    store = source()
    manifest, _ = ensure_index(store, "parse")
    assert ensure_index(store, "parse")[0]["id"] == manifest["id"]
    region = store.get("regions", "parse:p2:l0")
    store.put("regions", {**region, "text": region["text"] + " changed"})
    assert ensure_index(store, "parse")[0]["id"] != manifest["id"]


def test_aliases_reject_unsupplied_source_and_round_trip(tmp_path):
    store = source()
    _, chunks = ensure_index(store, "parse")
    packet = build_packet(
        store, Settings(mongo_uri="unused", work_dir=tmp_path), "parse", chunks, {chunks[0]["id"]}
    )
    original = {"claims": [{"evidence_ids": [next(iter(packet.allowed))]}]}
    assert packet.decode(packet.encode(original)) == original
    with pytest.raises(ValueError, match="alias"):
        packet.decode({"claims": [{"evidence_ids": ["parse:p999"]}]})


def test_bracketed_citations_resolve_in_all_nested_evidence_fields():
    packet = EvidencePacket("", [], {"e0": "parse:p1", "e19": "parse:p1:l3"}, [], [], {})
    response = {
        "findings": [{"evidence_ids": ["[e0]", "e19"]}],
        "claims": [{"evidence_ids": [" [e19] "]}],
        "pins": [{"evidence_ids": ["[e0]"]}],
        "circuits": [{"components": [{"evidence_ids": ["[e19]"]}]}],
        "reviewed_region_ids": ["[e0]"],
        "reason": "Keep [e19] prose unchanged",
    }
    decoded = packet.decode(response)
    assert decoded["findings"][0]["evidence_ids"] == ["parse:p1", "parse:p1:l3"]
    assert decoded["claims"][0]["evidence_ids"] == ["parse:p1:l3"]
    assert decoded["pins"][0]["evidence_ids"] == ["parse:p1"]
    assert decoded["circuits"][0]["components"][0]["evidence_ids"] == ["parse:p1:l3"]
    assert decoded["reviewed_region_ids"] == ["parse:p1"]
    assert decoded["reason"] == response["reason"]
    assert response["findings"][0]["evidence_ids"][0] == "[e0]"


@pytest.mark.parametrize("citation", ["[e999]", "[e0,e19]", "[e0] extra", "eN", "parse:p999", None])
def test_citation_normalization_cannot_introduce_unsupplied_evidence(citation):
    packet = EvidencePacket("", [], {"e0": "parse:p1"}, [], [], {})
    with pytest.raises(ValueError, match=r"\$\.claims\[0\]\.evidence_ids\[0\]"):
        packet.decode({"claims": [{"evidence_ids": [citation]}]})


class CompactProvider:
    def __init__(self, absent=False, search_requests=()):
        self.requests = []
        self.absent = absent
        self.search_requests = list(search_requests)

    def generate(self, request):
        self.requests.append(request)
        assert "image_attached=True" not in request.prompt  # Legacy packet not used.
        if request.task.startswith("blind:"):
            assert "ORIGINAL:" not in request.prompt
            assert "UNIQUE_FIRST_ANSWER" not in request.prompt
        aliases = re.findall(r"\[(e\d+)\]", request.prompt)
        alias = next(a for a in aliases if a != "eN")
        if request.task.startswith("review:"):
            return {"supported_claim_ids": [], "issues": [], "reviewed_region_ids": [alias]}
        sid = int(request.task.split(":")[1])
        result = {
            "status": "not_found" if self.absent else "present",
            "claims": [],
            "findings": [
                {
                    "requirement_id": req,
                    "status": "not_found" if self.absent else "present",
                    "evidence_ids": [] if self.absent else [alias],
                    "reason": "Checked synthetic source" if self.absent else "",
                }
                for req in requirements(sid)
            ],
            "search_requests": self.search_requests,
        }
        if sid == 1:
            result["pins"] = []
        if sid == 2:
            result["circuits"] = []
        return result


def engine(tmp_path, provider):
    store = source()
    store.put("catalogs", {"id": "parse", "targets": [TARGET], "ambiguities": []})
    settings = Settings(mongo_uri="unused", work_dir=tmp_path, retrieval_top_k=1)
    return Engine(store, provider, settings), store


def test_resume_recovers_bracketed_response_checkpoint_without_new_inference(tmp_path, monkeypatch):
    class BracketedProvider(CompactProvider):
        def generate(self, request):
            response = super().generate(request)
            for finding in response["findings"]:
                finding["evidence_ids"] = [f"[{alias}]" for alias in finding["evidence_ids"]]
            return response

    provider = BracketedProvider()
    e, store = engine(tmp_path, provider)
    rid = e.create_run("parse", "target")

    def old_decoder(self, response):
        raise ValueError("Unknown or unsupplied evidence alias")

    with monkeypatch.context() as patch:
        patch.setattr(EvidencePacket, "decode", old_decoder)
        with pytest.raises(ValueError, match="alias"):
            e.resume(rid, [1], review=False)
    assert len(provider.requests) == 1
    assert not store.find("results", {"run_id": rid})
    e.resume(rid, [1], review=False)
    assert len(provider.requests) == 1
    assert store.find("results", {"run_id": rid})
    assert any(
        event.get("telemetry", {}).get("recovered")
        for event in store.find("events", {"run_id": rid})
    )


def test_new_workflow_all_17_sections_no_llm_mapping_and_stable_export(tmp_path):
    provider = CompactProvider()
    e, store = engine(tmp_path, provider)
    rid = e.create_run("parse", "target")
    e.resume(rid)
    exported = e.export(rid)
    assert len(exported.sections) == 17
    assert len(store.find("coverage", {"run_id": rid})) == 51
    assert len(provider.requests) == 51
    assert all(not r.task.startswith("map:") for r in provider.requests)
    assert exported.metadata["pipeline"] == "retrieval-v2"
    assert "section_id" not in provider.requests[0].schema["properties"]
    assert "circuits" not in provider.requests[0].schema["properties"]
    count = len(provider.requests)
    e.resume(rid)
    assert len(provider.requests) == count


@pytest.mark.parametrize("blind", [False, True])
def test_not_found_alone_skips_expansion_for_extractor_and_blind_reader(tmp_path, blind):
    provider = CompactProvider(absent=True)
    e, _store = engine(tmp_path, provider)
    rid = e.create_run("parse", "target")
    e.read_retrieved(e.store.get("runs", rid), 3, blind=blind)
    assert [r.task for r in provider.requests] == ["blind:3" if blind else "extract:3"]
    assert "Document-wide canonical text search: False" in provider.requests[0].prompt


@pytest.mark.parametrize("blind", [False, True])
def test_explicit_search_request_is_recorded_without_expansion(tmp_path, blind):
    provider = CompactProvider(absent=True, search_requests=["Table 9 capacitor conditions"])
    e, _store = engine(tmp_path, provider)
    rid = e.create_run("parse", "target")
    section, *_ = e.read_retrieved(e.store.get("runs", rid), 3, blind=blind)
    assert [r.task for r in provider.requests] == ["blind:3" if blind else "extract:3"]
    assert "Document-wide canonical text search: False" in provider.requests[0].prompt
    assert section.status == "ambiguous"
    assert section.notes == [
        "Evidence expansion disabled; unresolved evidence requests: Table 9 capacitor conditions"
    ]


@pytest.mark.parametrize("blind", [False, True])
def test_unreadable_is_preserved_without_expansion(tmp_path, blind):
    class UnreadableProvider(CompactProvider):
        def generate(self, request):
            response = super().generate(request)
            response["status"] = "unreadable"
            for finding in response["findings"]:
                finding["status"] = "unreadable"
            return response

    provider = UnreadableProvider()
    e, _store = engine(tmp_path, provider)
    rid = e.create_run("parse", "target")
    section, *_ = e.read_retrieved(e.store.get("runs", rid), 3, blind=blind)
    assert [r.task for r in provider.requests] == ["blind:3" if blind else "extract:3"]
    assert section.status == "unreadable"
    assert all(f.status == "unreadable" for f in section.findings)


def test_unresolved_search_continues_to_independent_blind_and_review_without_expansion(tmp_path):
    provider = CompactProvider(absent=True, search_requests=["Table 9 capacitor conditions"])
    e, store = engine(tmp_path, provider)
    rid = e.create_run("parse", "target")
    e.resume(rid, [3])
    assert [r.task for r in provider.requests] == ["extract:3", "blind:3", "review:3"]
    assert "unresolved_evidence_request" in {
        issue["code"] for issue in store.get("results", f"{rid}:3")["validation"]
    }
    assert store.get("blind_reads", f"{rid}:3:blind")["section"]["notes"]


def test_compact_schema_preserves_quantities_conditions_and_target_ids(tmp_path):
    from jsonschema import Draft202012Validator
    from test_core import fact

    from datasheet_parser.contracts import expand_section, section_contract
    from datasheet_parser.providers import strict_schema

    claim = copy.deepcopy(fact())
    claim.pop("id")
    claim.pop("applies_to")
    row = {"status": "present", "claims": [claim], "findings": [], "search_requests": []}
    Draft202012Validator(strict_schema(section_contract(3))).validate(row)
    final = expand_section(row, 3, "chosen")
    assert final.claims[0].applies_to == ["chosen"]
    assert final.claims[0].id == "3.1"
    assert final.claims[0].quantity.typical == 3
    assert final.claims[0].conditions == ["TA = 25 C"]


def test_dense_vectors_cached_and_reranker_used_without_llm_or_network(monkeypatch):
    import sys
    from types import SimpleNamespace

    batches, reranks = [], []

    class Array(list):
        def tolist(self):
            return list(self)

    class Encoder:
        def __init__(self, name, **kwargs):
            assert kwargs["local_files_only"] is True

        def encode(self, texts, **kwargs):
            if len(texts) > 1:
                batches.append(texts)
            return Array([[1.0, 0.0] if "capacitor" in t.lower() else [0.0, 1.0] for t in texts])

    class Reranker:
        def __init__(self, name, **kwargs):
            pass

        def predict(self, pairs):
            reranks.append(pairs)
            return [2 if "capacitor" in text.lower() else 1 for _, text in pairs]

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=Encoder, CrossEncoder=Reranker),
    )
    store = source()
    manifest, chunks = ensure_index(store, "parse")
    config = {**CONFIG, "embedding_model": "fixture", "reranker_model": "fixture-reranker"}
    one = Retriever(store, manifest, chunks, config)
    two = Retriever(store, manifest, chunks, config)
    assert len(batches) == 1
    assert one.rank("capacitor circuit", 2) == two.rank("capacitor circuit", 2)
    assert reranks


def test_parallel_sections_use_separate_providers_and_remain_resumable(tmp_path, monkeypatch):
    import threading

    providers = []
    lock = threading.Lock()

    def provider_factory(settings):
        p = CompactProvider()
        with lock:
            providers.append(p)
        return p

    monkeypatch.setattr("datasheet_parser.engine.make_provider", provider_factory)
    e, store = engine(tmp_path, CompactProvider())
    e.settings.workers = 2
    rid = e.create_run("parse", "target")
    e.resume(rid, [1, 2, 3])
    assert len(store.find("results", {"run_id": rid})) == 3
    assert len(store.find("reviews", {"run_id": rid})) == 3
    assert 1 <= len(providers) <= 2
    assert len({id(p) for p in providers}) == len(providers)
    e.resume(rid, [1, 2, 3])
    assert sum(len(p.requests) for p in providers) == 9


def test_unseen_diagram_cannot_be_reported_as_confirmed_absence(tmp_path):
    provider = CompactProvider(absent=True)
    e, store = engine(tmp_path, provider)
    row = store.get("regions", "parse:p3:l0")
    store.put("regions", {**row, "text": "Figure 99. Unknown waveform"})
    page = store.get("pages", "parse:p3")
    store.put("pages", {**page, "text": "Figure 99. Unknown waveform"})
    # Source selection intentionally misses this diagram to exercise the guard.
    original_packet = e.retrieval_packet

    def packet(*args, **kwargs):
        p = original_packet(*args, **kwargs)
        p.image_pages = [n for n in p.image_pages if n != 3]
        return p

    e.retrieval_packet = packet
    rid = e.create_run("parse", "target")
    e.resume(rid, [3], review=False)
    saved = store.get("results", f"{rid}:3")
    assert saved["section"]["status"] == "ambiguous"
    assert all(f["status"] == "ambiguous" for f in saved["section"]["findings"])
    assert "incomplete_visual_search" in {i["code"] for i in saved["validation"]}


def test_mapping_is_code_not_an_inference_in_telemetry():
    from datasheet_parser.telemetry import summarize_events

    report = summarize_events(
        [
            {
                "stage": "retrieve:map",
                "status": "completed",
                "kind": "code",
                "time": "2026-01-01",
                "telemetry": {"elapsed_seconds": 2},
            }
        ]
    )
    assert report["completed_calls"] == 0
    assert report["code_stage_seconds"] == 2


def test_prepare_only_stops_before_inference_and_keeps_interactive_selection(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from datasheet_parser import cli

    p = CompactProvider()
    e, store = engine(tmp_path, p)
    monkeypatch.setattr(cli, "context", lambda *args: (e.settings, store, e))
    result = CliRunner().invoke(cli.app, ["extract", "parse", "--prepare-only"], input="1\n")
    assert result.exit_code == 0, result.output
    assert "Select target number" in result.output
    assert p.requests == []
    assert len(store.find("coverage")) == 51


def test_compact_response_survives_database_failure_without_another_call(tmp_path):
    p = CompactProvider()
    e, store = engine(tmp_path, p)
    rid = e.create_run("parse", "target")
    put = store.put
    fail = True

    def fail_once(collection, record):
        nonlocal fail
        if (
            fail
            and collection == "events"
            and record.get("stage") == "extract:3"
            and record["status"] == "completed"
        ):
            fail = False
            raise RuntimeError("database disconnected after compact inference")
        return put(collection, record)

    store.put = fail_once
    with pytest.raises(RuntimeError, match="database disconnected"):
        e.resume(rid, [3], review=False)
    e.resume(rid, [3], review=False)
    assert [r.task for r in p.requests] == ["extract:3"]
    assert store.get("results", f"{rid}:3")["section"]["section_id"] == 3


def test_text_only_page_coverage_never_stands_in_for_unseen_graphics(tmp_path):
    store = source()
    _, chunks = ensure_index(store, "parse")
    selected = {c["id"] for c in chunks if c["kind"] != "visual" and not c["unknown_visual"]}
    packet = build_packet(store, Settings(mongo_uri="unused", work_dir=tmp_path), "parse", chunks, selected)
    assert "parse:p1" in packet.covered_regions
    assert "parse:p2" not in packet.covered_regions  # Figure remains unseen despite its caption text.


def test_numbered_section_reference_resolves_across_pages():
    store = source()
    region = store.get("regions", "parse:p1:l0")
    store.put("regions", {**region, "text": "See Section 9.2 for constraints"})
    region = store.get("regions", "parse:p5:l0")
    store.put("regions", {**region, "text": "9.2 Application Constraints"})
    manifest, chunks = ensure_index(store, "parse")
    r = Retriever(store, manifest, chunks, CONFIG)
    selected = r.expand({c["id"] for c in chunks if c["page"] == 1}, TARGET)
    assert any(r.by_id[c]["page"] == 5 for c in selected)
