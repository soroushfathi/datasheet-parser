"""Reusable structural index. Context is retrieval metadata, never a replacement for source."""

import json
import re
from collections import defaultdict
from hashlib import sha256

INDEX_VERSION = "structural-8"
BOUNDED_INDEX_REVISION = "typed-local-headings-8"
QUALITY_INDEX_REVISION = "source-headings-lists-1"
BOUNDED_VERSIONS = {"structural-9", "structural-10"}
REFERENCE = re.compile(r"\b(figure|fig\.?|table|section)\s+(\d+(?:[.-]\d+)*)", re.IGNORECASE)
HEADING = re.compile(r"^\d+(?:\.\d+)*\s+[A-Z][A-Za-z ]{3,100}$")
LOCAL_HEADING = re.compile(r"^\d+(?:\.\d+)*\s+[A-Z][A-Za-z]{2,}[ A-Za-z0-9/()–—-]{0,100}$")


def _heading(text, version):
    if version in BOUNDED_VERSIONS and re.match(r"^\d+\s+(?:PINS?|MAX|MIN|TYP|REF)\b", text.strip()):
        return None
    numeric = (LOCAL_HEADING if version in BOUNDED_VERSIONS else HEADING).match(text.strip())
    if numeric or version != "structural-10":
        return numeric
    title = text.strip()
    words = title.split()
    connectors = {"and", "or", "to", "of", "the", "for", "with", "using", "by", "in"}
    single = {"Applications", "Introduction", "Protection", "Regulator", "Oscillator", "Timing", "Overview"}
    if (2 <= len(words) <= 12 or title in single) and len(title) <= 100 and all(
        w.lower() in connectors or re.fullmatch(r"[A-Z][A-Za-z0-9/()–-]*", w) for w in words
    ):
        return re.match(r".+", title)
    return None


def _section_key(text):
    numeric = re.match(r"\d+(?:\.\d+)*", text)
    return numeric.group() if numeric else "title:" + text


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


def ensure_index(store, parse_id, *, version=INDEX_VERSION):
    if version not in {INDEX_VERSION, *BOUNDED_VERSIONS}:
        raise ValueError(f"Unsupported structural index version: {version}")
    pages = sorted(store.find("pages", {"parse_id": parse_id}), key=lambda p: p["number"])
    regions = store.find("regions", {"parse_id": parse_id})
    source = sorted([(r["id"], r.get("text", ""), r.get("bbox")) for r in regions])
    digest_source = [version, source]
    if version in BOUNDED_VERSIONS:
        digest_source.append(QUALITY_INDEX_REVISION if version == "structural-10" else BOUNDED_INDEX_REVISION)
    digest = sha256(json.dumps(digest_source, sort_keys=True).encode()).hexdigest()[:24]
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
        local_heading, local_section = heading, section_key
        if version in BOUNDED_VERSIONS and kind != "visual":
            boxes = [r["bbox"] for r in items if r.get("bbox")]
            top = min((b[1] for b in boxes), default=0)
            preceding = [
                r
                for r in by_page[page["number"]]
                if r["kind"] == "line"
                and _heading(r["text"], version)
                and r.get("bbox")
                and r["bbox"][1] <= top + 1
            ]
            if preceding:
                local_heading = max(preceding, key=lambda r: r["bbox"][1])["text"]
                local_section = _section_key(local_heading)
            context = f"PDF page {page['number']}; heading candidate: {local_heading or 'unknown'}"
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
                "section_candidate": local_section,
                "package_hint": (
                    re.search(r"\b([A-Z]{1,3})0\d{3}[A-Z]\b", page["text"]).group(1)
                    if re.search(r"\b([A-Z]{1,3})0\d{3}[A-Z]\b", page["text"])
                    else None
                ),
            }
        )
        if version in BOUNDED_VERSIONS:
            chunks[-1]["heading_candidate"] = local_heading
            if not chunks[-1]["package_hint"] and re.search(
                r"PACKAGE VIEW|PACKAGE OUTLINE|BOARD LAYOUT|STENCIL DESIGN", page["text"]
            ):
                label = re.search(
                    r"^\s*([A-Z]{1,3})\s+\d{1,3}\s+(?:SOIC|SSOP|TSSOP|QFN|DFN|BGA|DIP)\b",
                    page["text"],
                    re.MULTILINE,
                )
                if label:
                    chunks[-1]["package_hint"] = label.group(1)

    for page in pages:
        rows = by_page[page["number"]]
        tables = [r for r in rows if r["kind"] == "table"]
        lines = sorted(
            [r for r in rows if r["kind"] == "line"], key=lambda r: (r.get("bbox", [0, 0])[1], r["id"])
        )
        headings = [r["text"] for r in lines if _heading(r["text"], version)]
        if headings:
            heading = headings[0]
            section_key = _section_key(heading)
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
            boundary = bool(_heading(line["text"], version))
            if version in BOUNDED_VERSIONS:
                # Separate source notes/captions from unrelated prose. The source line itself
                # remains intact; no word/row is cut to achieve a token limit.
                boundary |= bool(
                    re.match(r"^\s*(?:\(\d+\)|Notes?\b|Table\s+\d|Figure\s+\d)", line["text"], re.IGNORECASE)
                )
            if group and (
                (size + len(line["text"]) > (1200 if version == "structural-9" else 2400)
                 and not (version == "structural-10" and group[-1]["text"].rstrip().endswith(","))) or boundary
            ):
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
        graphic_pattern = (
            r"^\s*(?:figure|fig\.?)\s+\d|block diagram|package\s+outline|example board layout|land pattern"
            if version in BOUNDED_VERSIONS
            else r"\bfigure\s+\d|block diagram|package\s+outline|example board layout|land pattern"
        )
        graphical = page.get("requires_visual_read", False) or bool(
            re.search(graphic_pattern, page["text"], re.IGNORECASE | re.MULTILINE)
        )
        if version in BOUNDED_VERSIONS and "table of contents" in page["text"].lower():
            graphical = page.get("requires_visual_read", False) or bool(
                re.search(r"^\s*(?:figure|fig\.?)\s+\d", page["text"], re.IGNORECASE | re.MULTILINE)
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
            if _heading(line, version):
                number = _section_key(line.strip())
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
        if version in BOUNDED_VERSIONS:
            # The typed pass below owns closure in this index version.
            continue
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
    if version in BOUNDED_VERSIONS:
        _typed_links(chunks, by_page, per_page, destinations, page_meta, version=version)
    manifest = {
        "id": key,
        "parse_id": parse_id,
        "version": version,
        "status": "complete",
        "chunk_count": len(chunks),
        "page_count": len(pages),
        "unresolved_references": unresolved,
        "source_digest": digest,
    }
    if version in BOUNDED_VERSIONS:
        manifest["algorithm_revision"] = QUALITY_INDEX_REVISION if version == "structural-10" else BOUNDED_INDEX_REVISION
        manifest["unresolved_unknown_visual_pages"] = [
            p["number"] for p in pages if p.get("requires_visual_read", False)
        ]
    store.put_many("chunks", chunks)
    store.put("document_indexes", manifest)
    return manifest, chunks


def _typed_links(chunks, regions_by_page, per_page, destinations, pages, *, version="structural-9"):
    """Source-scoped closure: condition/notes stay attached, references remain budgetable.

    These edges are heuristics with an explicit type, not semantic proof. Unlike the old
    page closure, a table does not inherit every other table or paragraph on its page.
    """
    note = re.compile(r"^\s*(?:\(\d+\)|Notes?\b|Test\s+conditions?\b)", re.IGNORECASE | re.MULTILINE)
    condition = re.compile(
        r"\b(?:unless otherwise|over (?:the )?recommended|test conditions?|TA\s*=|VCC\s*=)", re.IGNORECASE
    )
    source_note = re.compile(r"^\s*(?:\(\d+\)|NOTES?\s*[:.]|[A-Z]\.\s+)", re.IGNORECASE)
    named = defaultdict(set)
    by_id = {c["id"]: c for c in chunks}
    for chunk in chunks:
        if chunk["kind"] not in {"text", "visual"}:
            continue
        for line in chunk["text"].splitlines():
            caption = re.match(
                r"^\s*(?:figure|fig\.?|table)\s+\d+(?:[.-]\d+)*[.:]?\s+(.+)$", line, re.IGNORECASE
            )
            if caption:
                title = tuple(re.findall(r"[a-z0-9]+", caption.group(1).lower()))
                if len(title) >= 4:
                    named[title].add(chunk["id"])
    for chunk in chunks:
        typed = {}

        def link(other, relation, required, chunk=chunk, typed=typed):
            own_package, other_package = chunk["package_hint"], other["package_hint"]
            if own_package and other_package and own_package != other_package:
                return
            # An image-only drawing cannot establish a note association with the
            # following drawing; unknown-to-known package transitions stay gaps.
            if (
                relation in {"footnote", "continuation"}
                and chunk["page"] != other["page"]
                and (
                    chunk["unknown_visual"]
                    or ("package" in chunk["context"].lower() and own_package != other_package)
                )
            ):
                return
            if other["id"] != chunk["id"]:
                old = typed.get(other["id"])
                if not old or required:
                    typed[other["id"]] = {"chunk_id": other["id"], "relation": relation, "required": required}

        for ref in references(chunk["text"]):
            for cid in destinations[ref]:
                link(by_id[cid], "reference", False)
        # Datasheets also refer to figure/table titles without printing their numbers.
        # Match source-derived titles in order, tolerating a short PDF subscript token
        # between wrapped words. Only one optional hop is followed by the retriever.
        words = re.findall(r"[a-z0-9]+", chunk["text"].lower())
        for title, ids in named.items():
            for start, word in enumerate(words):
                if word != title[0]:
                    continue
                cursor = start + 1
                for expected in title[1:]:
                    location = next(
                        (i for i in range(cursor, min(len(words), cursor + 3)) if words[i] == expected), None
                    )
                    if location is None:
                        break
                    cursor = location + 1
                else:
                    for cid in ids:
                        link(by_id[cid], "named_reference", False)
                    break
        if chunk["kind"] == "table" and chunk.get("heading_candidate"):
            for other in per_page[chunk["page"]]:
                if other["kind"] == "text" and any(
                    line.strip() == chunk["heading_candidate"].strip() for line in other["text"].splitlines()
                ):
                    link(other, "source_heading", True)
        if chunk["kind"] == "text":
            if source_note.match(chunk["text"]):
                for other in per_page[chunk["page"]]:
                    if other["kind"] == "text" and source_note.match(other["text"]):
                        link(other, "note_sibling", True)
            own_headings = {
                line.strip() for line in chunk["text"].splitlines()
                if (_heading(line, version) if version == "structural-10" else LOCAL_HEADING.match(line.strip()))
            }
            for other in per_page[chunk["page"]]:
                if other["kind"] == "table" and other.get("heading_candidate", "").strip() in own_headings:
                    link(other, "heading_source", True)
        # A reference often resolves to the caption text, rather than the numeric table
        # chunk. The caption and its nearest source table are one atomic object.
        if chunk["kind"] == "text":
            caption_types = {
                kind
                for line in chunk["text"].splitlines()
                if REFERENCE.match(line.strip())
                for kind, _ in references(line)
            }
            if "figure" in caption_types:
                for other in per_page[chunk["page"]]:
                    if other["kind"] == "visual":
                        link(other, "caption_source", True)
            if "table" in caption_types:
                caption_boxes = [
                    r["bbox"]
                    for r in regions_by_page[chunk["page"]]
                    if r["id"] in chunk["region_ids"] and r.get("bbox")
                ]
                candidates = []
                for other in per_page[chunk["page"]]:
                    if other["kind"] != "table":
                        continue
                    boxes = [
                        r["bbox"]
                        for r in regions_by_page[chunk["page"]]
                        if r["id"] in other["region_ids"] and r.get("bbox")
                    ]
                    if boxes and caption_boxes:
                        distance = min(
                            min(abs(a[3] - b[1]), abs(b[3] - a[1])) for a in caption_boxes for b in boxes
                        )
                        candidates.append((distance, other["ordinal"], other))
                if candidates:
                    link(min(candidates, key=lambda row: (row[0], row[1]))[2], "caption_source", True)
        if chunk["kind"] in {"table", "visual"}:
            source = [r for r in regions_by_page[chunk["page"]] if r["id"] in chunk["region_ids"]]
            boxes = [r["bbox"] for r in source if r.get("bbox")]
            top = min((b[1] for b in boxes), default=0)
            bottom = max((b[3] for b in boxes), default=pages[chunk["page"]].get("height", 1000))
            for other in per_page[chunk["page"]]:
                if other["kind"] != "text":
                    continue
                rows = [r for r in regions_by_page[chunk["page"]] if r["id"] in other["region_ids"]]
                near = any(
                    r.get("bbox") and r["bbox"][1] <= bottom + 120 and r["bbox"][3] >= top - 100 for r in rows
                )
                # Footnotes are conservatively shared on a page. Captions/test conditions are
                # mandatory when geometrically adjacent; geometry-free sources fall back wide.
                if note.search(other["text"]):
                    link(other, "footnote", True)
                elif condition.search(other["text"]) and (near or not boxes):
                    link(other, "condition", True)
                elif references(other["text"]) and (near or chunk["kind"] == "visual"):
                    link(other, "caption", True)
            if chunk["kind"] == "table" and bottom > pages[chunk["page"]].get("height", 1000) * 0.6:
                for other in per_page[chunk["page"] + 1]:
                    if note.search(other["text"]):
                        link(other, "footnote", True)
        if re.search(r"\bcontinued\b", chunk["text"], re.IGNORECASE):
            for n in (chunk["page"] - 1, chunk["page"] + 1):
                for other in per_page[n]:
                    if other["kind"] == "table" or note.search(other["text"]):
                        link(other, "continuation", True)
        # An explicit application cross-reference can join pages; a shared section number
        # alone is not enough reason to include a complete multi-page application chapter.
        chunk["typed_links"] = sorted(typed.values(), key=lambda e: (not e["required"], e["chunk_id"]))
        chunk["links"] = sorted(typed)
