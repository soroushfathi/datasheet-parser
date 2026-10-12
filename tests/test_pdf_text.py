"""Portable PDF fixtures for source-backed glyphs, not semantic replacements."""

import json
from copy import deepcopy
from hashlib import sha256
from io import BytesIO

import pdfplumber
import pytest

from datasheet_parser.ingestion import ingest
from datasheet_parser.pdf_text import TEXT_REPAIR_VERSION, prepare_page, repair_chars, repair_metadata
from datasheet_parser.store import MemoryStore


def glyph(text="<", x=473.2803, top=680.319, font="COAGEL+ArialMT"):
    return {
        "text": text,
        "fontname": font,
        "x0": x,
        "top": top,
        "x1": x + 5.256,
        "bottom": top + 9,
        "upright": True,
    }


def bar(x=473.28, top=688.08, **kwargs):
    return {
        "x0": x,
        "top": top,
        "x1": x + 5.28,
        "bottom": top + 0.66,
        "fill": True,
        "non_stroking_color": 0.0,
        **kwargs,
    }


def test_distinguishes_inclusive_and_strict_bounds_from_painted_source():
    # Coordinates captured from the audited PDF p15, including the strict 55 C
    # lower bound of ISBH. There is no underneath bar at x478.2384 in that row.
    source = [glyph(), glyph(x=493.2615), glyph(x=478.2384, top=693.3389), glyph(x=498.3018, top=693.3389)]
    untouched = deepcopy(source)
    chars, repairs, unresolved = repair_chars(
        source, [bar(), bar(x=493.26), bar(x=498.3, top=701.1)]
    )
    assert [c["text"] for c in chars] == ["\u2264", "\u2264", "<", "\u2264"]
    assert source == untouched
    assert len(repairs) == 3 and unresolved == []
    assert all(r["method"] == "operator_plus_vector_bar" and r["bar_bbox"] for r in repairs)


@pytest.mark.parametrize(
    "rectangle",
    [
        bar(x1=700),  # a table rule or an underline extending beyond the glyph
        bar(x=480),  # a neighboring glyph's mark
        bar(top=681),  # the top of the glyph, not its lower quarter
        bar(top=690),  # below the glyph
        bar(non_stroking_color=1.0),  # white backgrounds do not paint a bar
        bar(fill=False),
        bar(bottom=688.085),  # essentially zero-height geometry
        bar(bottom=690),  # a filled cell or large block
        bar(non_stroking_color=(1, 0, 0)),
    ],
)
def test_never_promotes_ordinary_less_than_without_exact_vector_evidence(rectangle):
    chars, repairs, unresolved = repair_chars([glyph()], [rectangle])
    assert chars[0]["text"] == "<"
    assert repairs == unresolved == []


def test_ambiguous_multiple_bars_and_rotated_operators_are_not_repaired():
    rotated = {**glyph(), "upright": False}
    for char, rectangles in [(glyph(), [bar(), bar(top=688.12)]), (rotated, [bar()])]:
        assert repair_chars([char], rectangles)[1] == []


def test_greater_than_and_existing_unicode_are_preserved_correctly():
    chars, repairs, _ = repair_chars([glyph(">"), glyph("\u2264", x=510)], [bar()])
    assert [c["text"] for c in chars] == ["\u2265", "\u2264"]
    assert len(repairs) == 1


def test_symbol_private_use_requires_font_identity_and_preserves_minus_signal():
    symbol = "COAIGC+SymbolMT"
    source = [glyph(c, font=symbol) for c in ["\uf057", "\uf06d", "\uf071"]]
    source += [glyph("\uf06d", font="CustomFont"), glyph("\ufffd"), glyph("D\u2013")]
    chars, repairs, unresolved = repair_chars(source, [], symbol_fonts=[symbol])
    assert [c["text"] for c in chars] == ["\u03a9", "\u03bc", "\u03b8", "\uf06d", "\ufffd", "D\u2013"]
    assert len(repairs) == 3 and len(unresolved) == 2
    assert all(r["source_codepoint"].startswith("U+F0") for r in repairs)


def test_region_repair_metadata_contains_only_local_glyphs():
    _, repairs, unresolved = repair_chars([glyph(), glyph("\ufffd", x=100)], [bar()])
    metadata = {"version": TEXT_REPAIR_VERSION, "repairs": repairs, "unresolved_glyphs": unresolved}
    local = repair_metadata(metadata, [470, 675, 510, 695])
    assert local["status"] == "repaired" and len(local["repairs"]) == 1
    assert local["unresolved_glyphs"] == []
    unknown = repair_metadata(metadata, [90, 675, 110, 695])
    assert unknown["status"] == "unresolved" and len(unknown["unresolved_glyphs"]) == 1


def pdf_fixture(content: bytes) -> bytes:
    """Minimal self-contained fixture; no PDF-generation dependency is needed."""
    cmap = (
        b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap "
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def "
        b"/CMapName /Fixture def /CMapType 2 def 1 begincodespacerange <00> <FF> endcodespacerange "
        b"2 beginbfchar <57> <F057> <6D> <F06D> endbfchar endcmap "
        b"CMapName currentdict /CMap defineresource pop end end"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
            b"/Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 7 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Symbol /ToUnicode 6 0 R >>",
        b"<< /Length " + str(len(cmap)).encode() + b" >>\nstream\n" + cmap + b"\nendstream",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(data)
    data += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        data += f"{offset:010d} 00000 n \n".encode()
    data += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return bytes(data)


def painted_fixture() -> bytes:
    # Rows with two inclusive bounds followed by a strict and an inclusive bound.
    content = (
        b"0 G .5 w 10 100 250 0 re S 10 125 250 0 re S 10 150 250 0 re S "
        b"10 100 0 50 re S 260 100 0 50 re S "
        b"BT /F1 10 Tf 20 135 Td (0 C < TA < 55 C) Tj ET "
        b"BT /F1 10 Tf 20 110 Td (55 C < TA < 70 C) Tj ET "
        b"BT /F2 10 Tf 180 135 Td (m) Tj ET "
        b"BT /F2 10 Tf 195 135 Td (W) Tj ET "
        b"BT /F1 10 Tf 20 10 Td (footer < 70) Tj ET"
    )
    # Independently read raw char boxes to paint a realistic composite glyph.
    with pdfplumber.open(BytesIO(pdf_fixture(content))) as pdf:
        operators = [c for c in pdf.pages[0].chars if c["text"] == "<"]
        for i in [0, 1, 3, 4]:
            c = operators[i]
            height = c["bottom"] - c["top"]
            rect_top = c["top"] + height * 0.86
            thickness = height * 0.07
            bottom_pdf = 200 - rect_top - thickness
            content += (
                f" 0 g {c['x0']} {bottom_pdf} {c['x1'] - c['x0']} {thickness} re f"
            ).encode()
    return pdf_fixture(content)


def test_font_resource_gating_and_consistent_page_text_lines_tables_footer(tmp_path):
    source = painted_fixture()
    with pdfplumber.open(BytesIO(source)) as pdf:
        prepared, metadata = prepare_page(pdf.pages[0])
        assert "0 C < TA < 55 C" in pdf.pages[0].extract_text()
        assert "0 C \u2264 TA \u2264 55 C" in prepared.extract_text()
        assert "55 C < TA \u2264 70 C" in prepared.extract_text()
        assert len(metadata["repairs"]) == 6  # 4 operator bars, 2 Symbol units
        assert metadata["unresolved_glyphs"] == []
    path = tmp_path / "glyph-fixture.pdf"
    path.write_bytes(source)
    store = MemoryStore()
    parse_id = ingest(path, store)
    page = store.get("pages", f"{parse_id}:p1")
    assert page["raw_text"] != page["text"]
    assert page["raw_printed_label_candidate"] == "footer < 70"
    assert page["printed_label_candidate"] == "footer \u2264 70"
    assert page["preparation_status"] == "ready"
    assert page["text_repair"]["version"] == TEXT_REPAIR_VERSION
    lines = [store.get("regions", rid) for rid in page["line_ids"]]
    assert any("0 C \u2264 TA \u2264 55 C" in r["text"] for r in lines)
    tables = [store.get("regions", rid) for rid in page["table_ids"]]
    assert tables
    assert any("55 C < TA \u2264 70 C" in json.dumps(r["rows"], ensure_ascii=False) for r in tables)
    assert any("0 C < TA < 55 C" in json.dumps(r["raw_rows"]) for r in tables)
    config = store.get("parses", parse_id)["config"]
    assert config["contract"] == "regions-2" and config["text_repair"] == TEXT_REPAIR_VERSION
    assert ingest(path, store) == parse_id
    old_config = {k: v for k, v in config.items() if k != "text_repair"}
    old_config["contract"] = "regions-1"
    document_id = sha256(source).hexdigest()
    old_parse_id = sha256((document_id + json.dumps(old_config, sort_keys=True)).encode()).hexdigest()[:24]
    old_record = {"id": old_parse_id, "config": old_config, "status": "complete", "legacy": True}
    store.put("parses", old_record)
    assert parse_id != old_parse_id
    assert ingest(path, store) == parse_id
    assert store.get("parses", old_parse_id) == old_record
