"""Source boundaries must preserve complete lists and ownership headings."""

from test_core import TARGET
from test_optimized_workflow import source
from test_retrieval import CONFIG

from datasheet_parser.document_index import ensure_index
from datasheet_parser.retrieval import Retriever


def test_quality_list_keeps_wrapped_tail_and_does_not_change_previous_index():
    first = "supported baud rates: " + "9600, " * 215
    second = "19200, " * 150
    tail = "115200 and 230400 baud."
    store = source(["UART Protocol\n" + first + "\n" + second + "\n" + tail + "\nElectrical Characteristics"])
    previous, old = ensure_index(store, "parse", version="structural-9")
    quality, chunks = ensure_index(store, "parse", version="structural-10")
    listing = next(c for c in chunks if first in c["text"])
    assert second in listing["text"] and tail in listing["text"]
    assert listing["heading_candidate"] == "UART Protocol"
    assert "Electrical Characteristics" not in listing["text"]
    assert quality["id"] != previous["id"]
    saved, unchanged = ensure_index(store, "parse", version="structural-9")
    assert saved == previous and unchanged == old
    assert not any(first in c["text"] and tail in c["text"] for c in old)


def test_unnumbered_heading_routes_to_complete_source_table_bidirectionally():
    store = source(["Absolute Maximum Ratings\nVDD 6.0 V\nRecommended Operating Conditions"])
    store.put("regions", {"id": "parse:p1:t1", "parse_id": "parse", "page": 1,
        "kind": "table", "text": "VDD 6.0 V", "rows": [["VDD", "6.0 V"]],
        "bbox": [10, 40, 230, 60]})
    _, chunks = ensure_index(store, "parse", version="structural-10")
    table = next(c for c in chunks if c["kind"] == "table")
    heading = next(c for c in chunks if c["text"] == "Absolute Maximum Ratings")
    assert table["heading_candidate"] == heading["text"]
    assert any(e["chunk_id"] == heading["id"] and e["required"] and
        e["relation"] == "source_heading" for e in table["typed_links"])
    assert any(e["chunk_id"] == table["id"] and e["required"] and
        e["relation"] == "heading_source" for e in heading["typed_links"])


def test_complete_source_rate_list_has_priority_over_unrelated_generic_headings():
    rates = "supports baud rates of 1200, 2400, 4800, 9600 and 19200 with parity."
    store = source(["UART\n" + rates, "Device Overview\nProtocols and clock or bus speed with applicable pins."])
    manifest, chunks = ensure_index(store, "parse", version="structural-10")
    retriever = Retriever(store, manifest, chunks, {**CONFIG, "packet_version": "budget-v1", "index_version": "structural-10"})
    for req in ("10.protocols", "12.clocks"):
        selected, _ = retriever.select(req, TARGET)
        assert any(rates in retriever.by_id[cid]["text"] for cid in selected)


def test_driver_loading_paragraph_is_available_to_interface_levels_reader():
    text = "The output drivers sink a maximum current of 12 mA each."
    store = source(["Activity Indicators\n" + text, "Electrical Characteristics\nVIH VIL VOH VOL"])
    manifest, chunks = ensure_index(store, "parse", version="structural-10")
    retriever = Retriever(store, manifest, chunks, {**CONFIG, "packet_version": "budget-v1", "index_version": "structural-10"})
    selected, _ = retriever.select("10.levels", TARGET)
    assert any(text in retriever.by_id[cid]["text"] for cid in selected)
