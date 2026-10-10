"""Source preservation and budget enforcement; no inference accuracy claims."""

import pytest
from PIL import Image
from test_core import TARGET
from test_retrieval import CONFIG, source

from datasheet_parser.config import Settings
from datasheet_parser.document_index import ensure_index
from datasheet_parser.evidence_packet import build_packet
from datasheet_parser.retrieval import Retriever
from datasheet_parser.token_budget import estimate_text_tokens

CONFIG_BOUNDED = {**CONFIG, "packet_version": "budget-v1"}


def fixture():
    store = source()
    manifest, chunks = ensure_index(store, "parse", version="structural-9")
    return store, manifest, chunks


def packet(store, chunks, tmp_path, selected, **kwargs):
    return build_packet(
        store,
        Settings(mongo_uri="unused", work_dir=tmp_path),
        "parse",
        chunks,
        selected,
        budget={"text_tokens": 12000, "image_tokens": 15000, "max_images": 4},
        **kwargs,
    )


def test_new_index_has_scoped_typed_links_and_separate_legacy_identity():
    store, manifest, chunks = fixture()
    table = next(c for c in chunks if c["kind"] == "table")
    assert manifest["version"] == "structural-9"
    assert manifest["unresolved_unknown_visual_pages"] == [4]
    assert any(e["relation"] == "footnote" and e["required"] for e in table["typed_links"])
    assert not any(c["id"] in table["links"] for c in chunks if c["page"] == 4)
    old, legacy = ensure_index(store, "parse")
    assert old["version"] == "structural-8"
    assert old["id"] != manifest["id"]
    assert all("typed_links" not in c for c in legacy)


def test_unknown_empty_visual_is_gap_not_universal_per_requirement_seed(tmp_path):
    store, manifest, chunks = fixture()
    retriever = Retriever(store, manifest, chunks, CONFIG_BOUNDED)
    selected, _ = retriever.select("4.supply", TARGET)
    assert not any(retriever.by_id[c]["page"] == 4 for c in selected)
    p = packet(store, chunks, tmp_path, selected)
    assert p.metrics["unclassified_visual_pages"] == [4]
    assert "Unselected or unclassified evidence cannot establish source absence" in p.text


def test_whole_table_header_conditions_and_aliases_survive_atomic_budgeting(tmp_path):
    store, _, chunks = fixture()
    table = next(c for c in chunks if c["kind"] == "table")
    p = packet(store, chunks, tmp_path, {table["id"]})
    assert '["C", "Value"]' in p.text
    assert '["C1", "100 nF"]' in p.text
    assert "Only at VCC = 3.3 V" in p.text
    assert "parse:p5:t1" in p.allowed
    assert p.metrics["text_tokens"] == estimate_text_tokens(p.text)
    assert p.decode(p.encode({"evidence_ids": ["parse:p5:t1"]})) == {"evidence_ids": ["parse:p5:t1"]}
    with pytest.raises(ValueError, match="unsupplied"):
        p.decode({"evidence_ids": ["parse:p999"]})


def test_oversized_required_bundle_stays_intact_and_reports_overflow(tmp_path):
    store, _, chunks = fixture()
    table = next(c for c in chunks if c["kind"] == "table")
    p = build_packet(
        store,
        Settings(mongo_uri="unused", work_dir=tmp_path),
        "parse",
        chunks,
        {table["id"]},
        required_regions=["parse:p5:t1"],
        budget={"text_tokens": 5, "max_images": 0},
        role="review",
    )
    assert "100 nF" in p.text and "Only at VCC = 3.3 V" in p.text
    assert p.metrics["role"] == "review"
    assert p.metrics["mandatory_overflow"]["text_tokens"]["limit"] == 5
    assert p.metrics["mandatory_overflow"]["max_images"]["limit"] == 0
    assert table["id"] in p.metrics["required_chunk_ids"]


def test_optional_bundle_omitted_whole_instead_of_chopping_rows(tmp_path):
    store, _, chunks = fixture()
    table = next(c for c in chunks if c["kind"] == "table")
    p = build_packet(
        store,
        Settings(mongo_uri="unused", work_dir=tmp_path),
        "parse",
        chunks,
        {table["id"]},
        budget={"text_tokens": 1000, "max_images": 0},
    )
    assert table["id"] in p.metrics["omitted_candidate_ids"]
    assert "100 nF" not in p.text and "Only at VCC = 3.3 V" not in p.text
    assert not p.images
    assert not p.metrics["mandatory_overflow"]
    assert p.metrics["omitted_bundles"][0]["exceeded"]["max_images"] == {"used": 1, "limit": 0}


def test_table_crop_covers_table_and_notes_without_claiming_whole_page(tmp_path):
    store, _, chunks = fixture()
    table = next(c for c in chunks if c["kind"] == "table")
    p = packet(store, chunks, tmp_path, {table["id"]})
    assert len(p.images) == 1
    d = p.metrics["images"][0]
    assert d["kind"] == "table_crop"
    assert d["bbox"][1] <= 38 and d["bbox"][3] >= 90
    assert "parse:p5:t1" in d["source_region_ids"]
    assert "parse:p5:l2" in d["source_region_ids"]
    assert p.image_pages == []
    assert "parse:p5" not in p.allowed
    # A complete text serialization can cover a text-only page in covered_regions;
    # it never creates a cropped-image page citation alias.
    with Image.open(p.images[0]) as image:
        assert image.height < 320


def test_unsafe_crop_falls_back_to_whole_page_with_truthful_citation(tmp_path):
    store = source()
    table = store.get("regions", "parse:p5:t1")
    store.put("regions", {**table, "bbox": None})
    _, chunks = ensure_index(store, "parse", version="structural-9")
    table_chunk = next(c for c in chunks if c["kind"] == "table")
    p = packet(store, chunks, tmp_path, {table_chunk["id"]})
    assert p.metrics["images"][0]["kind"] == "whole_page"
    assert p.metrics["images"][0]["fallback_reason"] == "unsafe_or_missing_geometry"
    assert p.image_pages == [5]
    assert "parse:p5" in p.allowed


def test_schematic_and_unknown_page_visuals_remain_whole(tmp_path):
    store, _, chunks = fixture()
    selected = {c["id"] for c in chunks if c["page"] in {2, 4}}
    p = packet(store, chunks, tmp_path, selected)
    assert set(p.image_pages) >= {2, 4}
    assert all(d["kind"] == "whole_page" for d in p.metrics["images"] if d["page"] in {2, 4})
    assert "parse:p2" in p.allowed and "parse:p4" in p.allowed


def test_seed_rank_controls_atomic_selection_under_image_limit(tmp_path):
    store, _, chunks = fixture()
    table = next(c for c in chunks if c["kind"] == "table")
    unknown = next(c for c in chunks if c["page"] == 4)
    p = build_packet(
        store,
        Settings(mongo_uri="unused", work_dir=tmp_path),
        "parse",
        chunks,
        {table["id"], unknown["id"]},
        budget={"text_tokens": 12000, "max_images": 1},
        priority={table["id"]: {"route": "seed", "rank_score": 0.2}, unknown["id"]: 0},
    )
    assert table["id"] in p.chunk_ids
    assert unknown["id"] in p.metrics["omitted_candidate_ids"]
    assert p.metrics["image_source_pages"] == [5]


def test_long_table_not_clipped_and_next_page_footnote_remains_in_bundle(tmp_path):
    store = source()
    table = store.get("regions", "parse:p5:t1")
    rows = [[f"parameter-{i}", f"{i} mA", "TA = 25 C"] for i in range(300)]
    import json

    store.put("regions", {**table, "bbox": [10, 180, 230, 300], "rows": rows, "text": json.dumps(rows)})
    page = store.get("pages", "parse:p5")
    store.put(
        "pages", {**page, "id": "parse:p6", "number": 6, "text": "(2) Applies to selected package only"}
    )
    store.put(
        "regions",
        {
            "id": "parse:p6:l1",
            "parse_id": "parse",
            "page": 6,
            "kind": "line",
            "text": "(2) Applies to selected package only",
            "bbox": [10, 10, 230, 30],
        },
    )
    _, chunks = ensure_index(store, "parse", version="structural-9")
    table_chunk = next(c for c in chunks if c["kind"] == "table")
    p = build_packet(
        store,
        Settings(mongo_uri="unused", work_dir=tmp_path),
        "parse",
        chunks,
        {table_chunk["id"]},
        required_ids=[table_chunk["id"]],
        budget={"text_tokens": 500, "max_images": 3},
    )
    assert "parameter-0" in p.text and "parameter-299" in p.text
    assert "TA = 25 C" in p.text
    assert "Applies to selected package only" in p.text
    assert "parse:p6:l1" in p.allowed
    assert p.metrics["mandatory_overflow"]["text_tokens"]["limit"] == 500


def test_atomic_table_budget_keeps_required_closure_even_when_only_root_selected(tmp_path):
    store, manifest, chunks = fixture()
    retriever = Retriever(store, manifest, chunks, CONFIG_BOUNDED)
    seeds = {c["id"] for c in chunks if c["page"] == 2}
    selected = retriever.expand(seeds, TARGET)
    p = packet(store, chunks, tmp_path, selected)
    assert "100 nF" in p.text
    assert "Only at VCC = 3.3 V" in p.text
    assert "parse:p5:t1" in p.allowed


def test_combined_source_limit_accounts_for_actual_text_and_images_atomically(tmp_path):
    store, _, chunks = fixture()
    table = next(c for c in chunks if c["kind"] == "table")
    before = packet(store, chunks, tmp_path, {table["id"]})
    total = before.metrics["text_tokens"] + before.metrics["estimated_image_tokens"]
    p = build_packet(
        store,
        Settings(mongo_uri="unused", work_dir=tmp_path),
        "parse",
        chunks,
        {table["id"]},
        budget={"text_tokens": 12000, "image_tokens": 12000, "total_tokens": total - 1},
    )
    assert table["id"] not in p.chunk_ids
    assert p.metrics["omitted_bundles"][0]["exceeded"]["total_tokens"] == {"used": total, "limit": total - 1}
    mandatory = build_packet(
        store,
        Settings(mongo_uri="unused", work_dir=tmp_path),
        "parse",
        chunks,
        {table["id"]},
        required_ids=[table["id"]],
        budget={"total_tokens": total - 1},
    )
    assert mandatory.metrics["mandatory_overflow"]["total_tokens"]["used"] == total
    assert "100 nF" in mandatory.text


@pytest.mark.parametrize("complement", [False, True])
def test_actual_nearest_heading_routes_recommended_table_ahead_of_common_words(complement):
    store = source()
    page = store.get("pages", "parse:p5")
    content = [
        ("6.1 Absolute Maximum Ratings", [10, 10, 230, 30]),
        ("Supply voltage input temperature voltage conditions", [10, 45, 230, 65]),
        ("6.3 Recommended Operating Conditions", [10, 160, 230, 180]),
        ("VCC = 3.3 V; TA = 25 C", [10, 190, 230, 210]),
    ]
    store.put("pages", {**page, "text": "\n".join(text for text, _ in content)})
    for rid in list(store.records["regions"]):
        if store.records["regions"][rid]["page"] == 5 and rid != "parse:p5":
            del store.records["regions"][rid]
    for i, (text, bbox) in enumerate(content):
        store.put(
            "regions",
            {
                "id": f"parse:p5:l{i}",
                "parse_id": "parse",
                "page": 5,
                "kind": "line",
                "text": text,
                "bbox": bbox,
            },
        )
    for i, (text, bbox) in enumerate(
        [
            (
                '[["Supply voltage", "Input voltage", "Temperature", "MAX"], ["VCC", "6 V", "150 C", "6"]]',
                [10, 40, 230, 100],
            ),
            ('[["MIN", "NOM", "MAX", "UNIT"], ["3", "3.3", "3.6", "V"]]', [10, 225, 230, 270]),
        ]
    ):
        store.put(
            "regions",
            {
                "id": f"parse:p5:t{i}",
                "parse_id": "parse",
                "page": 5,
                "kind": "table",
                "text": text,
                "bbox": bbox,
            },
        )
    manifest, chunks = ensure_index(store, "parse", version="structural-9")
    recommended = next(c for c in chunks if "parse:p5:t1" in c["region_ids"])
    assert recommended["heading_candidate"] == "6.3 Recommended Operating Conditions"
    retriever = Retriever(store, manifest, chunks, CONFIG_BOUNDED)
    selected, _ = retriever.select("4.supply", TARGET, complement=complement)
    assert recommended["id"] in selected


def test_package_specific_drawing_filter_precedes_top_k_and_does_not_confuse_d_dw():
    store = source()
    original = store.get("pages", "parse:p3")
    for number, code in [(3, "DW"), (6, "D")]:
        text = f"PACKAGE OUTLINE\n{code}0016A SOIC mechanical dimensions"
        store.put("pages", {**original, "id": f"parse:p{number}", "number": number, "text": text})
        store.put(
            "regions",
            {
                "id": f"parse:p{number}",
                "parse_id": "parse",
                "page": number,
                "kind": "page",
                "text": text,
                "bbox": [0, 0, 240, 320],
            },
        )
        store.put(
            "regions",
            {
                "id": f"parse:p{number}:l0",
                "parse_id": "parse",
                "page": number,
                "kind": "line",
                "text": text,
                "bbox": [10, 10, 230, 50],
            },
        )
    manifest, chunks = ensure_index(store, "parse", version="structural-9")
    retriever = Retriever(store, manifest, chunks, CONFIG_BOUNDED)
    selected, _ = retriever.select("7.dimensions", {**TARGET, "package_code": "D"})
    assert any(retriever.by_id[c]["page"] == 6 for c in selected)
    assert not any(retriever.by_id[c]["page"] == 3 for c in selected)


def test_source_caption_title_reference_preserves_distant_table_without_number(tmp_path):
    store = source()
    caption = store.get("regions", "parse:p5:l0")
    store.put("regions", {**caption, "text": "Table 9. Capacitor values for recommended supply levels"})
    line = store.get("regions", "parse:p2:l2")
    store.put(
        "regions", {**line, "text": "Use Capacitor values for recommended supply levels to select parts."}
    )
    manifest, chunks = ensure_index(store, "parse", version="structural-9")
    retriever = Retriever(store, manifest, chunks, CONFIG_BOUNDED)
    seeds = {c["id"] for c in chunks if c["page"] == 2}
    assert any(
        e["relation"] == "named_reference" for cid in seeds for e in retriever.by_id[cid]["typed_links"]
    )
    p = packet(store, chunks, tmp_path, retriever.expand(seeds, TARGET))
    assert "100 nF" in p.text and "Only at VCC = 3.3 V" in p.text


def test_bounded_index_invalidates_when_algorithm_revision_changes(monkeypatch):
    store, manifest, _ = fixture()
    monkeypatch.setattr("datasheet_parser.document_index.BOUNDED_INDEX_REVISION", "next-revision")
    updated, _ = ensure_index(store, "parse", version="structural-9")
    assert updated["id"] != manifest["id"]
    assert updated["algorithm_revision"] == "next-revision"


def test_requirement_topics_and_heading_diversity_keep_distinct_source_domains():
    store, manifest, _ = fixture()
    headings = [
        "5 Pin Configuration and Functions",
        "5 Pin Configuration and Functions",
        "9.2.2 Detailed Design Procedure",
        "6.4 Thermal Information",
        "11.1 Layout Guidelines",
        "6.7 Electrical Characteristics — Receiver",
        "6.3 Recommended Operating Conditions",
        "8.4 Device Functional Modes",
        "6.9 Typical Characteristics",
        "7 Parameter Measurement Information",
    ]
    chunks = []
    for i, heading in enumerate(headings):
        chunks.append(
            {
                "id": f"test:c{i}",
                "ordinal": i,
                "page": i + 1,
                "kind": "text",
                "context": heading,
                "heading_candidate": heading,
                "text": heading,
                "region_ids": [],
                "covered_region_ids": [],
                "links": [],
                "typed_links": [],
                "visual": False,
                "unknown_visual": False,
                "package_hint": None,
            }
        )
    retriever = Retriever(store, manifest, chunks, {**CONFIG_BOUNDED, "top_k": 2})
    connections, _ = retriever.select("1.connections", TARGET)
    assert "test:c2" in connections
    assert sum(cid in connections for cid in ["test:c0", "test:c1"]) == 1
    layout, _ = retriever.select("11.layout", TARGET)
    assert "test:c4" in layout
    levels, _ = retriever.select("10.levels", TARGET)
    assert {"test:c5", "test:c6"} <= levels
    graphs, _ = retriever.select("16.inventory", TARGET)
    assert {"test:c8", "test:c9"} <= graphs


def test_protection_content_and_actual_footnotes_outrank_generic_headers():
    store, manifest, _ = fixture()
    entries = [
        (1, "1 Features", "ESD protection at terminals."),
        (2, "8.1 Overview", "Outputs are protected against shorts to ground."),
        (
            3,
            "6.6 Electrical Characteristics — Driver",
            "(2) Short-circuit durations must be limited; only one output shorted at once.",
        ),
        (4, "4 Revision History", "Specification notes were revised."),
        (5, "7 Parameter Measurement Information", "A. Input pulse timing conditions apply."),
        (6, "6.3 Recommended Operating Conditions", "(1) Test conditions: TA = 25 C."),
        (7, "package appendix", "(1) Package grade applicability is defined here."),
    ]
    chunks = [
        {
            "id": f"routing:c{i}",
            "ordinal": i,
            "page": n,
            "kind": "text",
            "context": heading,
            "heading_candidate": heading,
            "text": text,
            "region_ids": [],
            "covered_region_ids": [],
            "links": [],
            "typed_links": [],
            "visual": False,
            "unknown_visual": False,
            "package_hint": None,
        }
        for i, (n, heading, text) in enumerate(entries)
    ]
    retriever = Retriever(store, manifest, chunks, {**CONFIG_BOUNDED, "top_k": 2})
    protected, _ = retriever.select("13.behavior", TARGET)
    assert {"routing:c1", "routing:c2"} <= protected
    retriever.config["top_k"] = 3
    footnotes, _ = retriever.select("15.footnotes", TARGET)
    assert "routing:c3" not in footnotes
    assert "routing:c4" in footnotes and "routing:c5" in footnotes


def test_unknown_drawing_does_not_inherit_next_package_notes_and_generic_view_has_hint():
    store = source()
    page = store.get("pages", "parse:p4")
    note_text = "PACKAGE OUTLINE\nDW 16 SOIC\nNOTES: Drawing dimensions apply to DW only."
    store.put(
        "pages", {**page, "id": "parse:p6", "number": 6, "text": note_text, "requires_visual_read": False}
    )
    store.put(
        "regions",
        {
            "id": "parse:p6:l0",
            "parse_id": "parse",
            "page": 6,
            "kind": "line",
            "text": note_text,
            "bbox": [10, 10, 230, 100],
        },
    )
    unknown = store.get("pages", "parse:p4")
    store.put("pages", {**unknown, "id": "parse:p5", "number": 5, "text": "", "requires_visual_read": True})
    table = store.get("regions", "parse:p5:t1")
    store.put("regions", {**table, "text": '[[""], [""]]', "bbox": [10, 200, 230, 300]})
    _, chunks = ensure_index(store, "parse", version="structural-9")
    foreign = [c for c in chunks if c["page"] == 6]
    assert all(c["package_hint"] == "DW" for c in foreign)
    table_chunk = next(c for c in chunks if c["kind"] == "table" and c["page"] == 5)
    assert not any(e["chunk_id"] in {c["id"] for c in foreign} for e in table_chunk["typed_links"])


def test_toc_reference_words_do_not_create_whole_page_visual_anchor():
    store = source()
    page = store.get("pages", "parse:p2")
    text = "Table of Contents\n8.2 Functional Block Diagram........9\n4 Revision History\nChanged Figure 9."
    store.put("pages", {**page, "text": text})
    _, chunks = ensure_index(store, "parse", version="structural-9")
    assert not any(c["kind"] == "visual" and c["page"] == 2 for c in chunks)
