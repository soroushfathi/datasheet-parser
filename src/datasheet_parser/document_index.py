"""Reusable structural index. Context is retrieval metadata, never a replacement for source."""

import json
import re
from collections import defaultdict
from hashlib import sha256

INDEX_VERSION = "structural-8"
REFERENCE = re.compile(r"\b(figure|fig\.?|table|section)\s+(\d+(?:[.-]\d+)*)", re.IGNORECASE)
HEADING = re.compile(r"^\d+(?:\.\d+)*\s+[A-Z][A-Za-z ]{3,100}$")


def references(text):
    return {
        (("figure" if kind.lower().startswith("fig") else kind.lower()), n)
        for kind, n in REFERENCE.findall(text)
    }


def _inside(line, table):
    if not line.get("bbox") or not table.get("bbox"):
        return False
    x0, y0, x1, y1 = line["bbox"]
    a, b, c, d = table["bbox"]
    overlap = max(0, min(x1, c) - max(x0, a)) * max(0, min(y1, d) - max(y0, b))
    return overlap / max(1, (x1 - x0) * (y1 - y0)) > 0.75


def ensure_index(store, parse_id):
    pages = sorted(store.find("pages", {"parse_id": parse_id}), key=lambda p: p["number"])
    regions = store.find("regions", {"parse_id": parse_id})
    source = sorted([(r["id"], r.get("text", ""), r.get("bbox")) for r in regions])
    digest = sha256(json.dumps([INDEX_VERSION, source], sort_keys=True).encode()).hexdigest()[:24]
    key = f"{parse_id}:{digest}"
    try:
        manifest = store.get("document_indexes", key)
        if manifest["status"] == "complete":
            return manifest, sorted(store.find("chunks", {"index_id": key}), key=lambda c: c["ordinal"])
    except KeyError:
        pass
    by_page = defaultdict(list)
    for region in regions:
        by_page[region["page"]].append(region)
    chunks = []
    heading = ""
    section_key = ""

    def append(page, items, kind, context, visual=False):
        ordinal = len(chunks)
        chunks.append(
            {
                "id": f"{key}:c{ordinal}",
                "index_id": key,
                "parse_id": parse_id,
                "ordinal": ordinal,
                "page": page["number"],
                "kind": kind,
                "context": context,
                "region_ids": [r["id"] for r in items],
                "covered_region_ids": [r["id"] for r in items]
                + [
                    line["id"]
                    for table in items
                    if table.get("kind") == "table"
                    for line in by_page[page["number"]]
                    if line["kind"] == "line" and _inside(line, table)
                ],
                "text": "\n".join(r.get("text", "") for r in items),
                "visual": visual,
                "links": [],
                "unknown_visual": page.get("requires_visual_read", False),
                "section_candidate": section_key,
                "package_hint": (
                    re.search(r"\b([A-Z]{1,3})0\d{3}[A-Z]\b", page["text"]).group(1)
                    if re.search(r"\b([A-Z]{1,3})0\d{3}[A-Z]\b", page["text"])
                    else None
                ),
            }
        )

    for page in pages:
        rows = by_page[page["number"]]
        tables = [r for r in rows if r["kind"] == "table"]
        lines = sorted(
            [r for r in rows if r["kind"] == "line"], key=lambda r: (r.get("bbox", [0, 0])[1], r["id"])
        )
        headings = [r["text"] for r in lines if HEADING.match(r["text"].strip())]
        if headings:
            heading = headings[0]
            section_key = re.match(r"\d+", heading).group()
        if re.search(
            r"PACKAGE OPTION ADDENDUM|PACKAGE MATERIALS INFORMATION|PACKAGE OUTLINE|EXAMPLE BOARD LAYOUT",
            page["text"],
        ):
            heading, section_key = "package appendix", ""
        context = f"PDF page {page['number']}; heading candidate: {heading or 'unknown'}"
        for table in tables:
            # Keep a source table whole: no row/column/condition truncation to meet a budget.
            append(page, [table], "table", context, visual=True)
        group = []
        size = 0
        for line in lines:
            if any(_inside(line, table) for table in tables):
                continue
            if group and (size + len(line["text"]) > 2400 or HEADING.match(line["text"].strip())):
                append(page, group, "text", context)
                group, size = [], 0
            group.append(line)
            size += len(line["text"])
        if group:
            append(page, group, "text", context)
        if not lines and not tables:
            page_region = next((r for r in rows if r["kind"] == "page"), None)
            if page_region:
                append(page, [page_region], "page", context, page.get("requires_visual_read", False))
        # A visual page anchor is essential for schematics/dimensions absent from text extraction.
        graphical = page.get("requires_visual_read", False) or bool(
            re.search(
                r"\bfigure\s+\d|block diagram|package\s+outline|example board layout|land pattern",
                page["text"],
                re.IGNORECASE,
            )
        )
        if graphical and (lines or tables):
            anchor = next((r for r in rows if r["kind"] == "page"), None)
            if anchor:
                captions = [
                    r["text"]
                    for r in lines
                    if references(r["text"])
                    or re.search(r"outline|land pattern|schematic", r["text"], re.IGNORECASE)
                ]
                append(
                    page,
                    [{**anchor, "text": "\n".join(captions) or page["text"][:1600]}],
                    "visual",
                    context,
                    True,
                )
    destinations = defaultdict(set)
    for chunk in chunks:
        for line in chunk["text"].splitlines():
            if HEADING.match(line.strip()):
                number = re.match(r"\d+(?:\.\d+)*", line.strip()).group()
                destinations[("section", number)].add(chunk["id"])
            if REFERENCE.match(line.strip()):
                for ref in references(line):
                    destinations[ref].add(chunk["id"])
        if chunk["kind"] == "visual":
            for ref in references(chunk["text"]):
                destinations[ref].add(chunk["id"])
    per_page = defaultdict(list)
    page_meta = {p["number"]: p for p in pages}
    per_section = defaultdict(list)
    for chunk in chunks:
        per_page[chunk["page"]].append(chunk)
        if chunk["section_candidate"]:
            per_section[chunk["section_candidate"]].append(chunk)
    unresolved = []
    for chunk in chunks:
        links = set()
        for ref in references(chunk["text"]):
            if destinations[ref]:
                links.update(destinations[ref])
            else:
                unresolved.append({"chunk_id": chunk["id"], "reference": list(ref)})
        # Conservative source-page closure: retain conditions, footnotes and captions with tables/figures.
        if chunk["kind"] in {"table", "visual"} or any(
            REFERENCE.match(line.strip()) for line in chunk["text"].splitlines()
        ):
            links.update(c["id"] for c in per_page[chunk["page"]])
        # Keep a numbered datasheet section across page boundaries, including design requirements
        # and footnotes on a later page. Metadata is a heuristic candidate, not a target-scope claim.
        if chunk["section_candidate"] and any(
            "application" in c["context"].lower() for c in per_section[chunk["section_candidate"]]
        ):
            links.update(c["id"] for c in per_section[chunk["section_candidate"]])
        if re.search(r"\bcontinued\b", chunk["text"], re.IGNORECASE):
            links.update(c["id"] for n in [chunk["page"] - 1, chunk["page"] + 1] for c in per_page[n])
        if chunk["kind"] == "table":
            source_tables = [r for r in by_page[chunk["page"]] if r["id"] in chunk["region_ids"]]
            if any(
                r.get("bbox", [0, 0, 0, 0])[3] > page_meta[chunk["page"]].get("height", 1000) * 0.6
                for r in source_tables
            ):
                links.update(
                    c["id"]
                    for c in per_page[chunk["page"] + 1]
                    if re.search(r"^\s*\(\d+\)|^\s*Notes?\b", c["text"], re.MULTILINE | re.IGNORECASE)
                )
        chunk["links"] = sorted(links - {chunk["id"]})
    manifest = {
        "id": key,
        "parse_id": parse_id,
        "version": INDEX_VERSION,
        "status": "complete",
        "chunk_count": len(chunks),
        "page_count": len(pages),
        "unresolved_references": unresolved,
        "source_digest": digest,
    }
    store.put_many("chunks", chunks)
    store.put("document_indexes", manifest)
    return manifest, chunks
