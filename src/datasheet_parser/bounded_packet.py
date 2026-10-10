"""Atomic source bundles and truthful image coverage for budget-v1 packets."""

import json
import math
from io import BytesIO

from PIL import Image

from .store import materialize
from .token_budget import estimate_image_tokens, estimate_text_tokens


def _closure(seeds, chunks):
    included = set(seeds) & set(chunks)
    frontier = set(included)
    while frontier:
        linked = {
            edge["chunk_id"]
            for cid in frontier
            for edge in chunks[cid].get("typed_links", [])
            if edge["required"] and edge["chunk_id"] in chunks
        } - included
        included.update(linked)
        frontier = linked
    return included


def _score(cid, priority, chunks):
    value = priority.get(cid, 0)
    if isinstance(value, dict):
        rank = value.get("rank_score") or 0
        # A direct query match precedes optional references at equal/unknown score.
        value = 1 + rank if value.get("route") == "seed" else rank
    return -float(value or 0), chunks[cid]["ordinal"]


def build_bounded_packet(
    store,
    settings,
    parse_id,
    chunks,
    selected,
    *,
    budget,
    priority=None,
    required_regions=(),
    required_ids=(),
    role="extract",
):
    from .evidence_packet import EvidencePacket

    by_id = {c["id"]: c for c in chunks}
    regions = {r["id"]: r for r in store.find("regions", {"parse_id": parse_id})}
    pages = {p["number"]: p for p in store.find("pages", {"parse_id": parse_id})}
    required_regions = set(required_regions)
    missing = required_regions - set(regions)
    if missing:
        raise ValueError(f"Required source regions do not exist: {sorted(missing)}")
    priority = priority or budget.get("priority", {})
    required_ids = set(required_ids) | set(budget.get("required_ids", []))
    required_ids.update(c["id"] for c in chunks if set(c["covered_region_ids"]) & required_regions)
    missing_chunks = required_ids - set(by_id)
    if missing_chunks:
        raise ValueError(f"Required source chunks do not exist: {sorted(missing_chunks)}")
    mandatory = _closure(required_ids, by_id)
    candidates = _closure(set(selected) | mandatory, by_id)
    # Required neighbors belong to their parent's bundle and must not be independently
    # admitted after omitting the table/circuit that gives them meaning.
    dependencies = {
        edge["chunk_id"]
        for cid in candidates
        for edge in by_id[cid].get("typed_links", [])
        if edge["required"]
    }
    roots = (candidates - dependencies) | (set(required_ids) & candidates)
    # Retain a representative root in a cycle of required continuations.
    reachable = _closure(roots, by_id)
    for cid in sorted(candidates - reachable, key=lambda c: by_id[c]["ordinal"]):
        if cid not in reachable:
            roots.add(cid)
            reachable |= _closure({cid}, by_id)
    images_cache = {}

    def image_size(n):
        if n not in images_cache:
            with Image.open(BytesIO(store.read_artifact(pages[n]["image_artifact_id"]))) as image:
                images_cache[n] = image.size
        return images_cache[n]

    def plan(chosen):
        chosen_chunks = [c for c in chunks if c["id"] in chosen]
        source_ids = {
            rid for c in chosen_chunks if c["kind"] != "visual" for rid in c["region_ids"]
        } | required_regions
        visual_pages = {c["page"] for c in chosen_chunks if c["visual"] or c["unknown_visual"]}
        visual_pages |= {regions[rid]["page"] for rid in required_regions if regions[rid]["kind"] == "page"}
        descriptors = []
        for n in sorted(visual_pages):
            page_chunks = [c for c in chosen_chunks if c["page"] == n]
            width, height = image_size(n)
            must_be_whole = (
                any(c["kind"] != "table" and c["visual"] or c["unknown_visual"] for c in page_chunks)
                or f"{parse_id}:p{n}" in required_regions
            )
            table_ids = {rid for c in page_chunks if c["kind"] == "table" for rid in c["region_ids"]}
            bbox, pixels = None, None
            fallback = "source_visual_anchor" if must_be_whole else None
            page_source = {
                rid for rid in source_ids if regions[rid]["page"] == n and regions[rid]["kind"] != "page"
            }
            if not must_be_whole and table_ids:
                bbox, pixels = _safe_crop(pages[n], regions, page_source, width, height)
                if bbox is None:
                    fallback = "unsafe_or_missing_geometry"
            if bbox is None:
                source_ids.add(f"{parse_id}:p{n}")
                descriptor = {
                    "page": n,
                    "kind": "whole_page",
                    "bbox": [0, 0, pages[n].get("width"), pages[n].get("height")],
                    "pixel_bbox": [0, 0, width, height],
                    "width": width,
                    "height": height,
                    "source_region_ids": [f"{parse_id}:p{n}"],
                    "fallback_reason": fallback,
                }
            else:
                descriptor = {
                    "page": n,
                    "kind": "table_crop",
                    "bbox": bbox,
                    "pixel_bbox": pixels,
                    "width": pixels[2] - pixels[0],
                    "height": pixels[3] - pixels[1],
                    "source_region_ids": sorted(page_source),
                    "fallback_reason": None,
                }
            descriptor["estimated_image_tokens"] = estimate_image_tokens(
                descriptor["width"], descriptor["height"]
            )
            descriptors.append(descriptor)

        def order(rid):
            r = regions[rid]
            return r["page"], -1 if r["kind"] == "page" else (r.get("bbox") or [0, 0])[1], rid

        aliases = {f"e{i}": rid for i, rid in enumerate(sorted(source_ids, key=order))}
        reverse = {v: k for k, v in aliases.items()}
        context = {rid: c["context"] for c in chosen_chunks for rid in c["region_ids"]}
        attachments = [
            {
                "page": d["page"],
                "kind": d["kind"],
                "evidence": [reverse[rid] for rid in d["source_region_ids"]],
            }
            for d in descriptors
        ]
        parts = [
            "Cite only [eN] identifiers. Context labels are retrieval metadata; read source evidence.",
            "Source packet is bounded. Unselected or unclassified evidence cannot establish source absence.",
            "Attached images in order: " + json.dumps(attachments),
        ]
        previous = None
        for alias, rid in aliases.items():
            r = regions[rid]
            current = (r["page"], context.get(rid, ""))
            if current != previous:
                parts.append(f"PDF page {current[0]}; context: {current[1]}")
                previous = current
            body = (
                "PAGE IMAGE; read attached whole source page"
                if r["kind"] == "page" and r["page"] in visual_pages
                else r["text"]
            )
            parts.append(f"[{alias}] {r['kind'] if r['kind'] != 'line' else ''}\n{body}")
        text = "\n".join(parts)
        return text, aliases, descriptors, estimate_text_tokens(text)

    limits = {
        "text_tokens": budget.get("text_tokens"),
        "image_tokens": budget.get("image_tokens"),
        "max_images": budget.get("max_images"),
        "total_tokens": budget.get("total_tokens"),
    }

    def exceeded(candidate):
        _, _, descriptors, count = candidate
        image_count = sum(d["estimated_image_tokens"] for d in descriptors)
        return {
            name: {"used": used, "limit": limits[name]}
            for name, used in (
                ("text_tokens", count),
                ("image_tokens", image_count),
                ("max_images", len(descriptors)),
                ("total_tokens", count + image_count),
            )
            if limits[name] is not None and used > limits[name]
        }

    chosen = set(mandatory)
    current = plan(chosen)
    mandatory_overflow = exceeded(current)
    omitted_bundles = []

    def bundle_score(cid):
        # Captions, headings and tables may form a cycle. Its representative ID must
        # inherit the highest source relevance, not depend on insertion order.
        return min(_score(member, priority, by_id) for member in _closure({cid}, by_id))

    for cid in sorted(roots - mandatory, key=bundle_score):
        bundle = _closure({cid}, by_id)
        if bundle <= chosen:
            continue
        candidate = plan(chosen | bundle)
        overflow = exceeded(candidate)
        if overflow:
            omitted_bundles.append(
                {"root_chunk_id": cid, "chunk_ids": sorted(bundle - chosen), "exceeded": overflow}
            )
        else:
            chosen |= bundle
            current = candidate
    text, aliases, descriptors, text_tokens = current
    images = []
    for descriptor in descriptors:
        page = pages[descriptor["page"]]
        image_id = page["image_artifact_id"]
        if descriptor["kind"] == "table_crop":
            with Image.open(BytesIO(store.read_artifact(image_id))) as image:
                out = BytesIO()
                image.crop(descriptor["pixel_bbox"]).save(out, format="PNG")
            image_id = store.artifact(out.getvalue(), "image/png", f"packet-table-p{descriptor['page']}.png")
        images.append(materialize(store, image_id, settings.work_dir / "cache", ".png"))
        descriptor["artifact_id"] = image_id
    covered = {rid for cid in chosen for rid in by_id[cid]["covered_region_ids"]} | required_regions
    full_pages = [d["page"] for d in descriptors if d["kind"] == "whole_page"]
    covered.update(f"{parse_id}:p{n}" for n in full_pages)
    for n in pages:
        page_chunks = [c for c in chunks if c["page"] == n]
        if (
            page_chunks
            and all(c["id"] in chosen for c in page_chunks)
            and not any(c["kind"] == "visual" or c["unknown_visual"] for c in page_chunks)
        ):
            covered.add(f"{parse_id}:p{n}")
    unknown = sorted(n for n, p in pages.items() if p.get("requires_visual_read") and n not in full_pages)
    metrics = {
        "packet_version": "budget-v1",
        "role": role,
        "text_chars": len(text),
        "text_tokens": text_tokens,
        "image_count": len(images),
        "image_pages": full_pages,
        "image_source_pages": [d["page"] for d in descriptors],
        "images": descriptors,
        "estimated_image_tokens": sum(d["estimated_image_tokens"] for d in descriptors),
        "combined_source_tokens_estimate": text_tokens
        + sum(d["estimated_image_tokens"] for d in descriptors),
        "image_token_estimate_kind": "geometry_budget_proxy_not_provider_billing",
        "region_count": len(aliases),
        "chunk_count": len(chosen),
        "broad_text": False,
        "budget": limits,
        "budget_exceeded": bool(exceeded(current)),
        "mandatory_overflow": mandatory_overflow,
        "required_chunk_ids": sorted(mandatory),
        "candidate_chunk_ids": sorted(candidates),
        "omitted_candidate_ids": sorted(candidates - chosen),
        "omitted_chunk_ids": sorted(candidates - chosen),
        "omitted_bundles": omitted_bundles,
        "unselected_chunk_count": len(chunks) - len(chosen),
        "unclassified_visual_pages": unknown,
    }
    return EvidencePacket(
        text,
        images,
        aliases,
        [c["id"] for c in chunks if c["id"] in chosen],
        full_pages,
        metrics,
        covered,
    )


def _safe_crop(page, regions, source_ids, width, height):
    """Crop a whole source table and every supplied note/header on its page, with margin.

    Do not crop untrusted/incomplete geometry. In particular, no assumptions about PDF
    rotation are needed: ingestion records both boxes and page size after rotation.
    """
    page_width, page_height = page.get("width"), page.get("height")
    if not page_width or not page_height or not source_ids:
        return None, None
    boxes = [regions[rid].get("bbox") for rid in source_ids]
    if any(
        not box
        or len(box) != 4
        or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in box)
        or not (0 <= box[0] < box[2] <= page_width and 0 <= box[1] < box[3] <= page_height)
        for box in boxes
    ):
        return None, None
    margin = 12
    bbox = [
        max(0, min(b[0] for b in boxes) - margin),
        max(0, min(b[1] for b in boxes) - margin),
        min(page_width, max(b[2] for b in boxes) + margin),
        min(page_height, max(b[3] for b in boxes) + margin),
    ]
    sx, sy = width / page_width, height / page_height
    pixels = [
        math.floor(bbox[0] * sx),
        math.floor(bbox[1] * sy),
        math.ceil(bbox[2] * sx),
        math.ceil(bbox[3] * sy),
    ]
    if pixels[2] <= pixels[0] or pixels[3] <= pixels[1]:
        return None, None
    # An almost-full-page crop adds little value; retain the full source as the safer option.
    if (pixels[2] - pixels[0]) * (pixels[3] - pixels[1]) > width * height * 0.9:
        return None, None
    return bbox, pixels
