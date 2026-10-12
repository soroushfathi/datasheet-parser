"""Source-defined application figures are atomic, independently read audit units.

All unit requests are planned and preflighted before inference. Whole figure
pages and the raw selected-package pin table remain frozen for both readers and
the reviewer. No failed read expands its evidence or changes the token limit.
"""

import json
import re
from copy import deepcopy
from hashlib import sha256

from .contracts import cited_regions, compact_review, expand_section, output_budget, section_contract
from .evidence_packet import build_packet
from .models import Review, SectionResult, Status
from .netlist_normalization import source_pin_inventory
from .providers import LLMRequest, ProviderError, strict_schema
from .quality_policy import quality_instructions
from .requirements import requirements
from .review_batches import merge_review_batches
from .review_context import RECONCILIATION_VERSION, reconcile_review
from .token_budget import estimate_text_tokens, request_input_metrics

FIGURE_WORKFLOW_VERSION = "application-figures-1"
CAPTION = re.compile(r"^\s*(?:Figure|Fig\.?)\s+(\d+(?:[.-]\d+)*)(?:\s*[.:]\s*|\s+)(.+)$", re.IGNORECASE)
CAPTION_PROSE = re.compile(r"^(?:shows?|illustrates?|depicts?|presents?|provides?|is|are|and|through|to)\b", re.IGNORECASE)
REFERENCE_PAGE = re.compile(r"^\s*(?:table\s+of\s+contents|contents|list\s+of\s+(?:figures|illustrations)|references)\b", re.IGNORECASE)
APPLICATION = re.compile(
    r"application\s+(?:circuits?|diagrams?|schematics?)|(?:typical|reference)\s+application", re.IGNORECASE
)
POSITIVE_CAPTION = re.compile(
    r"application|schematic|reference\s+design|converter|cable|interfacing|(?:bus|self)[ -]powered", re.IGNORECASE
)
EXCLUDED = re.compile(
    r"\b(?:block|internal|test|timing|waveform|mechanical|package|footprint|layout|land\s+pattern)\b",
    re.IGNORECASE,
)
SECTION_END = re.compile(
    r"^(?:DC |AC )?(?:Electrical Characteristics|Specifications)|^Absolute Maximum|"
    r"^Pin Configuration|^Pin Description|^Package|^Ordering|^Document History|^Revision History",
    re.IGNORECASE,
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def source_app_figures(engine, run):
    """Find actual application captions, not references, using source headings.

    Application scope carries across its following pages until a new explicitly
    different section. Figure numbers and page ranges are never part-specific.
    Captions on the same number's continued pages form one atomic figure unit.
    """
    pages = sorted(engine.store.find("pages", {"parse_id": run["parse_id"]}), key=lambda p: p["number"])
    regions = engine.store.find("regions", {"parse_id": run["parse_id"]})
    by_page = {}
    for region in regions:
        if region["kind"] == "line":
            by_page.setdefault(region["page"], []).append(region)
    found = {}
    application_scope = False
    for page in pages:
        lines = sorted(by_page.get(page["number"], []), key=lambda r: (r.get("bbox", [0, 0])[1], r["id"]))
        # The leading lines contain page/section titles. Do not infer a new title
        # from a schematic's IC labels or a cross-reference buried in prose.
        titles = [r["text"].strip() for r in lines[:10]] or page.get("text", "").splitlines()[:10]
        if any(REFERENCE_PAGE.match(title) for title in titles):
            application_scope = False
            continue
        if any(SECTION_END.match(title) for title in titles):
            application_scope = False
        if any(APPLICATION.search(title) for title in titles):
            application_scope = True
        for region in lines:
            match = CAPTION.match(region["text"])
            if not match:
                continue
            number, caption = match.groups()
            if CAPTION_PROSE.match(caption) or EXCLUDED.search(caption) or not (application_scope or POSITIVE_CAPTION.search(caption)):
                continue
            if number in found:
                old = found[number]
                if caption.casefold() != old["caption"].casefold() and "continued" not in caption.casefold():
                    raise ValueError(f"Application figure {number} has conflicting source captions")
                old["pages"] = sorted(set(old["pages"]) | {page["number"]})
                old["caption_region_ids"].append(region["id"])
            else:
                found[number] = {
                    "number": number,
                    "key": re.sub(r"[^a-z0-9_-]", "-", number.casefold()),
                    "label": f"Figure {number}",
                    "caption": caption,
                    "pages": [page["number"]],
                    "caption_region_ids": [region["id"]],
                }
    return list(found.values())


def _source_plan(engine, run):
    manifest, retriever = engine.retrieval_context(run)
    figures = source_app_figures(engine, run)
    if not figures:
        raise ValueError("Figure workflow requires actual application source captions")
    regions = engine.store.find("regions", {"parse_id": run["parse_id"]})
    inventory = source_pin_inventory(regions, run["target"])
    by_id = {r["id"]: r for r in regions}
    pin_regions = {rid for pin in inventory for rid in pin["evidence_ids"]}
    pin_pages = {by_id[rid]["page"] for rid in pin_regions}
    pages = {p["number"]: p for p in engine.store.find("pages", {"parse_id": run["parse_id"]})}
    plan = {
        "version": FIGURE_WORKFLOW_VERSION,
        "index_id": manifest["id"],
        "parse_id": run["parse_id"],
        "target": run["target"],
        "raw_pin_inventory": inventory,
        "pin_region_ids": sorted(pin_regions),
        "pin_pages": sorted(pin_pages),
        "figures": figures,
        "source_images": {
            str(n): pages[n]["image_artifact_id"] for n in sorted(pin_pages | {n for f in figures for n in f["pages"]})
        },
    }
    digest = sha256(_json(plan).encode()).hexdigest()
    plan.update(id=f"{run['id']}:2:{digest}", run_id=run["id"], section_id=2, plan_hash=digest)
    engine.store.put("figure_plans", plan)
    return plan, retriever.chunks, by_id


def _packet(engine, run, plan, unit, chunks, regions, role, answers=(), required=()):
    pages = set(unit["pages"]) | set(plan["pin_pages"])
    # Scope is established from source before reading. Trim only links to other
    # figure units; every source chunk within the frozen unit stays mandatory.
    local = [deepcopy(c) for c in chunks if c["page"] in pages]
    ids = {c["id"] for c in local}
    for chunk in local:
        chunk["links"] = [cid for cid in chunk.get("links", []) if cid in ids]
        chunk["typed_links"] = [edge for edge in chunk.get("typed_links", []) if edge["chunk_id"] in ids]
    required = set(required)
    if not required <= set(regions) or any(regions[rid]["page"] not in pages for rid in required):
        raise ValueError("Figure answer cites source outside its frozen figure/pin pages")
    model = Review if role == "review" else section_contract(2, version=run["contract_version"])
    reserved = engine.request_reserved_tokens(run, 2, model, answers)
    # Raw source inventory is an optional deterministic aid, not an AI answer.
    reserved += estimate_text_tokens(_json(plan["raw_pin_inventory"])) + 512
    packet = build_packet(
        engine.store, engine.settings, run["parse_id"], local, ids,
        required_ids=ids,
        required_regions=required | set(plan["pin_region_ids"]) | {f"{run['parse_id']}:p{n}" for n in pages},
        budget=engine.packet_limits(run, 2, role, reserved), role=role,
    )
    packet.metrics.update(figure_workflow_version=FIGURE_WORKFLOW_VERSION,
                          figure_plan_hash=plan["plan_hash"], figure_number=unit["number"],
                          index_id=plan["index_id"])
    return packet


def _figure_scope(plan, unit, packet):
    return (
        "\nSOURCE_FIGURE_INVENTORY: " + _json([
            {"figure": f["label"], "caption": f["caption"], "pages": f["pages"]} for f in plan["figures"]
        ])
        + "\nFIGURE_SCOPE: " + _json({"figure": unit["label"], "caption": unit["caption"], "pages": unit["pages"]})
        + "\nProcess ONLY this FIGURE_SCOPE. The inventory lists other units that separate requests process; "
        "never report those figures missing here. Attached pin source is package context, not another circuit. "
        "Inspect the complete supplied figure and prose, including component values, all wiring, labels and NC. "
        "IC net nodes and terminals use physical pad numbers when source correspondence is provable. "
        "The raw pin inventory below is source text, independently derived, and is not an AI extraction. "
        "Use literal pin names as context; never invent mappings for duplicated GND/VDD or unequal names. "
        "Retain ambiguous named terminals if physical correspondence cannot be established. "
        "Source package NC does not establish an observed schematic no-connect; distinguish unobserved pads. "
        "Multiple parts/ICs require observed references or explicit generated ref_origin. "
        "Never substitute engineering recommendations for the existing circuit.\nRAW_PIN_INVENTORY: "
        + _json(packet.encode(plan["raw_pin_inventory"]))
    )


def _preflight(engine, run, model, prompt, packet, budget):
    from .engine import POLICY

    serialized = POLICY + prompt + f"\nOutput token target: {budget}. Be concise; preserve facts and conditions."
    actual = request_input_metrics(serialized, strict_schema(model), packet.images)
    limit = run["retrieval_config"]["request_token_budget"]
    overflow = packet.metrics.get("mandatory_overflow", {})
    if set(overflow) - {"total_tokens"} or actual["input_tokens_estimate"] > limit:
        error = ProviderError("Atomic figure request exceeds configured budget; source was not clipped",
                              code="request_budget_exceeded")
        error.figure_number = packet.metrics["figure_number"]
        raise error
    metrics = {**packet.metrics, "request_token_budget": limit, "serialized_preflight": actual}
    if overflow:
        metrics.update(reservation_warning=overflow, mandatory_overflow={})
    fingerprint = LLMRequest("figure-unit", serialized, strict_schema(model), packet.images,
                             output_budget=budget).fingerprint(engine.settings.model, engine.settings.reasoning)
    return metrics, fingerprint


def _read_requests(engine, run, plan, chunks, regions, blind):
    role = "blind" if blind else "extract"
    model = section_contract(2, version=run["contract_version"])
    prepared = []
    for unit in plan["figures"]:
        packet = _packet(engine, run, plan, unit, chunks, regions, role)
        prompt = (
            f"\n{'Independently read' if blind else 'Extract'} existing application circuit, section 2. "
            "You have no other extraction's answer. One finding per requirement. Extract every component "
            "and observed connection in this one figure, with exact source aliases. Keep all conditions. "
            "Only existing application wiring establishes a circuit; pin diagrams and block diagrams do not. "
            "Use the compact-v2 schema; public claim IDs are generated by code. search_requests are diagnostics "
            "only; unsuccessful source reading is ambiguous/unreadable rather than definitive source absence.\n"
            f"TARGET: {_json(run['target'])}\nREQUIREMENTS: {_json(requirements(2))}"
            + "\n" + quality_instructions(2) + _figure_scope(plan, unit, packet) + "\n" + packet.text
        )
        budget = output_budget(2, role)
        metrics, fingerprint = _preflight(engine, run, model, prompt, packet, budget)
        stage = f"{role}:2:figure:{unit['key']}"
        prepared.append({"unit": unit, "packet": packet, "prompt": prompt, "metrics": metrics,
                         "output_budget": budget, "fingerprint": fingerprint, "stage": stage, "model": model})
    return prepared


def _canonical(section, unit, blind):
    data = section.model_dump(mode="json")
    prefix = f"2.{'b' if blind else ''}f{unit['key']}."
    for i, claim in enumerate(data["claims"], 1):
        claim["id"] = prefix + str(i)
    for i, circuit in enumerate(data["circuits"], 1):
        named_figure = re.search(r"(?:Figure|Fig\.?)\s*(\d+(?:[.-]\d+)*)", circuit["figure"], re.IGNORECASE)
        if named_figure and named_figure.group(1) != unit["number"]:
            raise ValueError("Circuit belongs to a different source figure than the requested unit")
        circuit["id"] = f"fig{unit['key']}:{i}:{circuit['id']}"
        circuit["figure"] = f"{unit['label']}. {unit['caption']}"
    if not data["circuits"]:
        data["status"] = "ambiguous"
        for finding in data["findings"]:
            if finding["requirement_id"] in {"2.circuits", "2.connections"}:
                finding.update(status="ambiguous", reason=f"{unit['label']} exists in source but circuit was not reconstructed")
                finding["evidence_ids"] = list(dict.fromkeys([
                    *finding["evidence_ids"], *unit["caption_region_ids"],
                ]))
        data["notes"].append(f"{unit['label']} exists in source but circuit reading is incomplete")
    return SectionResult.model_validate(data)


def _read_records(engine, run, plan, chunks, regions, blind, *, logical_events=True):
    # Complete-plan preflight precedes the first paid call, including on resume.
    prepared = _read_requests(engine, run, plan, chunks, regions, blind)
    role = "blind" if blind else "extract"
    stage = f"{role}:2"
    if logical_events:
        engine.event(run["id"], stage, "started", kind="code", figure_count=len(prepared))
    records = []
    try:
        for item in prepared:
            unit, packet = item["unit"], item["packet"]
            key = f"{run['id']}:2:{plan['plan_hash']}:{role}:{unit['key']}:{item['fingerprint']}"
            try:
                row = engine.store.get("figure_reads", key)
                section = SectionResult.model_validate(row["section"])
            except KeyError:
                parsed = engine.call(run["id"], item["stage"], item["model"], item["prompt"], packet.images,
                                     request_metrics=item["metrics"], output_budget=item["output_budget"])
                decoded = packet.decode(parsed.model_dump(mode="json"))
                section = _canonical(expand_section(decoded, 2, run["target"]["id"],
                                                    version=run["contract_version"]), unit, blind)
                section.notes.extend(f"Retrieval diagnostic: {phrase}" for phrase in decoded["search_requests"])
                row = {"id": key, "run_id": run["id"], "section_id": 2, "role": role,
                       "figure_number": unit["number"], "figure_key": unit["key"],
                       "plan_hash": plan["plan_hash"], "request_fingerprint": item["fingerprint"],
                       "section": section.model_dump(mode="json"), "packet_metrics": item["metrics"],
                       "supplied_region_ids": sorted(packet.allowed), "visual_pages": packet.image_pages,
                       "aliases": packet.aliases, "chunk_ids": packet.chunk_ids,
                       "unresolved_evidence_requests": decoded["search_requests"]}
                _validate_unit(section, packet)
                engine.store.put("figure_reads", row)
            _validate_unit(section, packet)
            records.append(row)
    except Exception as error:
        error._datasheet_stage = stage
        if logical_events:
            engine.event(run["id"], stage, "failed", kind="code")
        raise
    if logical_events:
        engine.event(run["id"], stage, "completed", kind="code", figure_count=len(records))
    return records


def _validate_unit(section, packet):
    if {f.requirement_id for f in section.findings} != set(requirements(2)) or len(section.findings) != len(requirements(2)):
        raise ValueError("Figure read must cover each section 2 requirement once")
    if any(c.requirement_id not in requirements(2) for c in section.claims):
        raise ValueError("Figure claim uses an unknown requirement")
    if not cited_regions(section.model_dump(mode="json")) <= packet.allowed:
        raise ValueError("Figure read cites unsupplied evidence")


def _merge_records(records):
    sections = [SectionResult.model_validate(row["section"]) for row in records]
    findings = []
    unresolved = []
    for req in requirements(2):
        parts = [next(f for f in s.findings if f.requirement_id == req) for s in sections]
        states = {f.status for f in parts}
        if states <= {Status.PRESENT, Status.NOT_APPLICABLE}:
            status = Status.PRESENT if Status.PRESENT in states else Status.NOT_APPLICABLE
        elif states == {Status.UNREADABLE}:
            status = Status.UNREADABLE
        else:
            status = Status.CONFLICTING if Status.CONFLICTING in states else Status.AMBIGUOUS
        reason = "; ".join(dict.fromkeys(f.reason for f in parts if f.reason))
        if status in {Status.AMBIGUOUS, Status.UNREADABLE, Status.CONFLICTING}:
            unresolved.append(req)
            reason = reason or "One or more source-defined application figures remain incompletely read"
        findings.append({"requirement_id": req, "status": status,
                         "evidence_ids": sorted({rid for f in parts for rid in f.evidence_ids}), "reason": reason})
    section = SectionResult.model_validate({
        "section_id": 2, "status": "ambiguous" if unresolved else "present", "findings": findings,
        "claims": [c for s in sections for c in s.claims], "pins": [],
        "circuits": [c for s in sections for c in s.circuits],
        "notes": list(dict.fromkeys(n for s in sections for n in s.notes)),
    })
    return section, unresolved


def read_figures(engine, run, *, blind=False):
    """Return the ordinary retrieval read tuple, with all source figures merged."""
    plan, chunks, regions = _source_plan(engine, run)
    records = _read_records(engine, run, plan, chunks, regions, blind)
    section, unresolved = _merge_records(records)
    allowed = {rid for row in records for rid in row["supplied_region_ids"]}
    visual = sorted({n for row in records for n in row["visual_pages"]})
    searches = list(dict.fromkeys(phrase for row in records for phrase in row.get("unresolved_evidence_requests", [])))
    if searches:
        section.status = Status.AMBIGUOUS
    uncertainty = {
        req: {"reported_status": "not_found", "effective_status": "ambiguous",
              "supplied_region_ids": sorted(allowed),
              "source_figures_unresolved": [row["figure_number"] for row in records
                  if any(f["requirement_id"] == req and f["status"] == "not_found" for f in row["section"]["findings"])]}
        for req in unresolved
        if any(f["requirement_id"] == req and f["status"] == "not_found"
               for row in records for f in row["section"]["findings"])
    }
    return section, allowed, visual, {
        "figure_workflow_version": FIGURE_WORKFLOW_VERSION, "figure_plan_id": plan["id"],
        "figure_plan_hash": plan["plan_hash"], "figure_count": len(records),
        "figure_read_ids": [row["id"] for row in records], "image_pages": visual,
        "unresolved_evidence_requests": searches, "absence_coverage_uncertainty": uncertainty,
        "figures": [{"figure_number": row["figure_number"], "metrics": row["packet_metrics"]} for row in records],
    }, unresolved


def _unit_view(full, row, unit, blind):
    section = deepcopy(row["section"])
    prefix = f"2.{'b' if blind else ''}f{unit['key']}."
    section["claims"] = [c for c in full["claims"] if c["id"].startswith(prefix)]
    section["circuits"] = [c for c in full["circuits"] if c["id"].startswith(f"fig{unit['key']}:")]
    # Findings/notes stay unit-local, so another figure's absence cannot leak in.
    return section


def review_figures(engine, run, record):
    """Audit two independent full answers separately for every frozen figure unit."""
    plan, chunks, regions = _source_plan(engine, run)
    key = f"{run['id']}:2"
    try:
        saved = engine.store.get("reviews", key)
        if saved.get("figure_plan_hash") == plan["plan_hash"]:
            return saved
    except KeyError:
        pass
    blind = engine.blind_retrieved(run, 2)
    # Reconstruct exact request keys, so model/prompt/source changes never reuse
    # an unrelated unit read. Normal resumes use records without paid requests.
    original_rows = _read_records(engine, run, plan, chunks, regions, False, logical_events=False)
    blind_rows = _read_records(engine, run, plan, chunks, regions, True, logical_events=False)
    prepared = []
    for unit, original_row, independent_row in zip(plan["figures"], original_rows, blind_rows, strict=True):
        original_section = _unit_view(record["section"], original_row, unit, False)
        independent_section = _unit_view(blind["section"], independent_row, unit, True)
        original = compact_review(original_section, run["target"]["id"])
        independent = compact_review(independent_section, run["target"]["id"])
        packet = _packet(engine, run, plan, unit, chunks, regions, "review", (original, independent),
                         cited_regions(original_section) | cited_regions(independent_section))
        scope = _figure_scope(plan, unit, packet)
        scoped_run = deepcopy(run)
        scoped_run["retrieval_config"]["request_token_budget"] -= estimate_text_tokens(scope) + 64
        request = engine._prepare_review_request(scoped_run, 2, original_row, independent_row,
                                                packet, original, independent)
        prompt = request["prompt"] + scope
        metrics, fingerprint = _preflight(engine, run, Review, prompt, packet, request["output_budget"])
        metrics.update({k: v for k, v in request["metrics"].items()
                        if k not in {"request_token_budget", "serialized_preflight", "mandatory_overflow"}})
        prepared.append({"unit": unit, "packet": packet, "prompt": prompt, "metrics": metrics,
                         "fingerprint": fingerprint, "output_budget": request["output_budget"],
                         "claims": original_section["claims"], "original_section": original_section})
    engine.event(run["id"], "review:2", "started", kind="code", figure_count=len(prepared))
    reviews, raw_reviews, unit_audits = [], [], []
    try:
        for request in prepared:
            unit, packet = request["unit"], request["packet"]
            part_id = f"{key}:{plan['plan_hash']}:{unit['key']}:{request['fingerprint']}"
            try:
                part = engine.store.get("review_figure_parts", part_id)
                review = Review.model_validate(part["review"])
            except KeyError:
                parsed = engine.call(run["id"], f"review:2:figure:{unit['key']}", Review,
                                     request["prompt"], packet.images, request_metrics=request["metrics"],
                                     output_budget=request["output_budget"])
                review = Review.model_validate(packet.decode(parsed.model_dump(mode="json")))
                engine._validate_review_part(review, requirements(2), request["claims"])
                if not (set(review.reviewed_region_ids) | cited_regions(review.model_dump(mode="json"))) <= packet.allowed:
                    raise ValueError("Figure review cites unsupplied source")
                engine.store.put("review_figure_parts", {
                    "id": part_id, "run_id": run["id"], "section_id": 2,
                    "figure_number": unit["number"], "plan_hash": plan["plan_hash"],
                    "request_fingerprint": request["fingerprint"], "packet_metrics": request["metrics"],
                    "aliases": packet.aliases, "chunk_ids": packet.chunk_ids,
                    "review": review.model_dump(mode="json"),
                })
            engine._validate_review_part(review, requirements(2), request["claims"])
            if not (set(review.reviewed_region_ids) | cited_regions(review.model_dump(mode="json"))) <= packet.allowed:
                raise ValueError("Saved figure review cites unsupplied source")
            # Absence is scoped to this existing schematic. The same component
            # value in another figure is not evidence that this unit recorded it,
            # even when both claims cite the shared package pin table.
            effective, audit = reconcile_review(review, request["original_section"])
            raw_reviews.append(review)
            reviews.append(effective)
            unit_audits.append({
                "figure_number": unit["number"], "figure_key": unit["key"],
                "figure": unit["label"], "review_part_id": part_id, **audit,
            })
    except Exception as error:
        error._datasheet_stage = "review:2"
        engine.event(run["id"], "review:2", "failed", kind="code")
        raise
    merged = merge_review_batches(reviews)
    reconciliation = {
        "version": RECONCILIATION_VERSION,
        "scope": "source_figure_units",
        "raw_review": merge_review_batches(raw_reviews).model_dump(mode="json"),
        "unit_reviews": unit_audits,
        # These indices refer to the unit's raw_review, not the merged raw list.
        "decisions": [{
            "figure_number": unit["figure_number"], "figure_key": unit["figure_key"],
            "review_part_id": unit["review_part_id"], "unit_issue_index": decision["raw_issue_index"],
            **{key: value for key, value in decision.items() if key != "raw_issue_index"},
        } for unit in unit_audits for decision in unit["decisions"]],
    }
    row = {"id": key, "run_id": run["id"], "section_id": 2, **merged.model_dump(mode="json"),
           "figure_plan_id": plan["id"], "figure_plan_hash": plan["plan_hash"],
           "partition_version": FIGURE_WORKFLOW_VERSION, "partition_count": len(reviews),
           "quality_version": "quality-v1", "review_reconciliation": reconciliation}
    engine.store.put("reviews", row)
    for req in requirements(2):
        cov = engine.store.get("coverage", f"{run['id']}:{req}")
        engine.store.put("coverage", {**cov, "review_status": "issues_found"
                                     if any(i.requirement_id == req for i in merged.issues) else "reviewed",
                                     "reviewed_region_ids": merged.reviewed_region_ids})
    engine.event(run["id"], "review:2", "completed", kind="code", figure_count=len(reviews))
    return row
