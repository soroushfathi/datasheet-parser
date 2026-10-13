"""Serialize each source region once and use reversible, packet-local citation aliases."""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .store import materialize


@dataclass
class EvidencePacket:
    text: str
    images: list[Path]
    aliases: dict[str, str]
    chunk_ids: list[str]
    image_pages: list[int]
    metrics: dict
    covered_regions: set[str] = field(default_factory=set)

    @property
    def allowed(self):
        return set(self.aliases.values())

    def decode(self, response):
        def resolve(value, path):
            # Source labels are rendered as [eN], so either literal representation is valid.
            # Never guess an alias or accept a region that was absent from this packet.
            alias = value.strip() if isinstance(value, str) else value
            if isinstance(alias, str) and re.fullmatch(r"\[e\d+\]", alias):
                alias = alias[1:-1]
            # Exact canonical compatibility is limited to this supplied packet,
            # never the document's global region inventory or target provenance.
            if isinstance(alias, str) and alias in self.allowed:
                return alias
            if not isinstance(alias, str) or alias not in self.aliases:
                raise ValueError(f"Unknown or unsupplied evidence alias at {path}: {value!r}")
            return self.aliases[alias]

        def visit(value, key="", path="$"):
            if isinstance(value, dict):
                return {k: visit(v, k, f"{path}.{k}") for k, v in value.items()}
            if isinstance(value, list):
                if key in {"evidence_ids", "reviewed_region_ids"}:
                    return [resolve(v, f"{path}[{i}]") for i, v in enumerate(value)]
                return [visit(v, path=f"{path}[{i}]") for i, v in enumerate(value)]
            return value

        return visit(response)

    def encode(self, response):
        reverse = {v: k for k, v in self.aliases.items()}

        def visit(value, key=""):
            if isinstance(value, dict):
                return {k: visit(v, k) for k, v in value.items()}
            if isinstance(value, list):
                if key in {"evidence_ids", "reviewed_region_ids"}:
                    return [reverse[v] for v in value]
                return [visit(v) for v in value]
            return value

        return visit(response)


def build_packet(
    store,
    settings,
    parse_id,
    chunks,
    selected,
    *,
    broad_text=False,
    required_regions=(),
    budget=None,
    priority=None,
    required_ids=(),
    role="extract",
):
    if budget is not None:
        from .bounded_packet import build_bounded_packet

        return build_bounded_packet(
            store,
            settings,
            parse_id,
            chunks,
            selected,
            budget=budget,
            priority=priority,
            required_regions=required_regions,
            required_ids=required_ids,
            role=role,
        )
    chosen = [c for c in chunks if c["id"] in selected]
    if broad_text:
        # A bounded retry widens to canonical document text; unrelated images are not added automatically.
        chosen = chunks
    regions = {r["id"]: r for r in store.find("regions", {"parse_id": parse_id})}
    pages = {p["number"]: p for p in store.find("pages", {"parse_id": parse_id})}
    source_ids = {rid for c in chosen if c["kind"] not in {"visual"} for rid in c["region_ids"]}
    source_ids.update(required_regions)
    image_pages = sorted({c["page"] for c in chunks if c["id"] in selected and c["visual"]})
    image_pages = sorted(
        set(image_pages)
        | {regions[rid]["page"] for rid in required_regions if regions[rid]["kind"] == "page"}
    )
    # One page image per page, even when several tables/figures share it. A safe whole-page fallback
    # preserves captions, pin numbering and schematic connectivity until reliable figure crops exist.
    images = [
        materialize(store, pages[n]["image_artifact_id"], settings.work_dir / "cache", ".png")
        for n in image_pages
    ]
    source_ids.update(f"{parse_id}:p{n}" for n in image_pages)

    def source_order(rid):
        region = regions[rid]
        if region["kind"] == "page":
            y = -1
        elif region.get("bbox"):
            y = region["bbox"][1]
        else:
            suffix = re.search(r"[lt](\d+)$", rid)
            y = int(suffix.group(1)) if suffix else 0
        return region["page"], y, rid

    ordered = sorted(source_ids, key=source_order)
    aliases = {f"e{i}": rid for i, rid in enumerate(ordered)}
    context_by_region = {rid: c["context"] for c in chosen for rid in c["region_ids"]}
    parts = [
        "Cite only [eN] identifiers. Context labels are retrieval metadata; read source evidence.",
        "Attached images in order: "
        + json.dumps(
            [
                {"page": n, "evidence": next(k for k, v in aliases.items() if v == f"{parse_id}:p{n}")}
                for n in image_pages
            ]
        ),
    ]
    previous = None
    for alias, rid in aliases.items():
        r = regions[rid]
        context = (r["page"], context_by_region.get(rid, ""))
        if context != previous:
            parts.append(f"PDF page {context[0]}; context: {context[1]}")
            previous = context
        # Page evidence is a visual anchor. Never duplicate its full text beside line/table evidence.
        body = (
            "PAGE IMAGE; read attached source"
            if r["kind"] == "page" and r["page"] in image_pages
            else r["text"]
        )
        parts.append(f"[{alias}] {r['kind'] if r['kind'] != 'line' else ''}\n{body}")
    text = "\n".join(parts)
    manifest = [
        {"id": c["id"], "page": c["page"], "kind": c["kind"], "context": c["context"]}
        for c in chunks
        if c["id"] not in selected
    ]
    covered = {rid for c in chosen for rid in c["covered_region_ids"]}
    chosen_ids = {c["id"] for c in chosen}
    for n in pages:
        page_chunks = [c for c in chunks if c["page"] == n]
        # A complete canonical serialization covers text-only page citations. It does not stand
        # in for a page image when the index identifies graphics or unknown visual content.
        if (
            page_chunks
            and all(c["id"] in chosen_ids for c in page_chunks)
            and not any(c["kind"] == "visual" or c["unknown_visual"] for c in page_chunks)
        ):
            covered.add(f"{parse_id}:p{n}")
    return EvidencePacket(
        text,
        images,
        aliases,
        [c["id"] for c in chosen],
        image_pages,
        {
            "text_chars": len(text),
            "image_count": len(images),
            "image_pages": image_pages,
            "region_count": len(aliases),
            "chunk_count": len(chosen),
            "broad_text": broad_text,
            "budget_exceeded": len(text) > settings.packet_text_budget,
            "unselected_chunk_count": len(manifest),
        },
        covered,
    )
