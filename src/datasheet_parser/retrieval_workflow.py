"""Versioned retrieval pipeline; legacy runs retain their original prompts and mapping."""

import json
import time

from .contracts import expand_section, section_contract
from .document_index import ensure_index
from .evidence_packet import build_packet
from .models import Review, Status
from .requirements import SECTIONS, requirements
from .retrieval import Retriever
from .validation import validate_section


class RetrievalWorkflow:
    def discovery_packet(self, parse_id):
        _, chunks = ensure_index(self.store, parse_id)
        selected = {
            c["id"]
            for c in chunks
            if c["unknown_visual"]
            or c["page"] == 1
            or any(
                word in c["text"].lower()
                for word in [
                    "orderable",
                    "ordering information",
                    "package option",
                    "device comparison",
                    "device information",
                    "order number",
                ]
            )
        }
        # Do not infer variants from mechanically available packages. Relevant ordering pages are
        # indexed across the whole PDF and their notes/continuations are retained.
        frontier = set(selected)
        for _ in range(2):
            frontier = {link for c in chunks if c["id"] in frontier for link in c["links"]} - selected
            selected.update(frontier)
        if not selected:
            selected = {c["id"] for c in chunks}
        return build_packet(self.store, self.settings, parse_id, chunks, selected)

    def retrieval_context(self, run):
        config = run["retrieval_config"]
        key = json.dumps([run["parse_id"], config], sort_keys=True)
        if not hasattr(self, "_retrievers"):
            self._retrievers = {}
        if key not in self._retrievers:
            manifest, chunks = ensure_index(self.store, run["parse_id"])
            try:
                saved = self.store.get("mapping_batches", f"{run['id']}:retrieval")
                if saved["index_id"] != manifest["id"]:
                    raise ValueError("Source/index changed since mapping; create a new run")
            except KeyError:
                pass
            self._retrievers[key] = (manifest, Retriever(self.store, manifest, chunks, config))
        return self._retrievers[key]

    def map_retrieval(self, run):
        record_id = f"{run['id']}:retrieval"
        try:
            self.store.get("mapping_batches", record_id)
            return
        except KeyError:
            pass
        self.event(run["id"], "retrieve:map", "started", kind="code")
        start = time.monotonic()
        manifest, retriever = self.retrieval_context(run)
        rows = []
        for req in requirements():
            selected, routes = retriever.select(req, run["target"])
            regions = sorted({rid for cid in selected for rid in retriever.by_id[cid]["region_ids"]})
            rows.append(
                {
                    "id": f"{run['id']}:{req}",
                    "run_id": run["id"],
                    "requirement_id": req,
                    "candidate_region_ids": regions,
                    "candidate_chunk_ids": sorted(selected),
                    "selection_routes": routes,
                    "extraction_status": "pending",
                    "review_status": "pending",
                }
            )
        self.store.put_many("coverage", rows)
        self.store.put(
            "mapping_batches",
            {
                "id": record_id,
                "run_id": run["id"],
                "index_id": manifest["id"],
                "kind": "retrieval",
                "requirement_count": len(rows),
            },
        )
        self.event(
            run["id"],
            "retrieve:map",
            "completed",
            kind="code",
            telemetry={"elapsed_seconds": time.monotonic() - start},
            index_id=manifest["id"],
            chunk_count=len(retriever.chunks),
            unresolved_references=len(manifest["unresolved_references"]),
        )

    def retrieval_packet(
        self, run, sid, *, complement=False, expanded=False, required_regions=(), searches=()
    ):
        started = time.monotonic()
        _, retriever = self.retrieval_context(run)
        selected, routes = set(), {}
        for req in requirements(sid):
            ids, why = retriever.select(req, run["target"], complement=complement, expanded=expanded)
            selected.update(ids)
            routes.update(why)
        # Reviewer must see every source cited by either extraction, including expanded reads.
        additional = {c["id"] for c in retriever.chunks if set(c["region_ids"]) & set(required_regions)}
        for search in searches:
            additional.update(cid for cid, _ in retriever.rank(search, run["retrieval_config"]["top_k"] * 3))
        selected.update(retriever.expand(additional, run["target"]))
        packet_settings = self.settings.model_copy(
            update={"packet_text_budget": run["retrieval_config"]["text_budget"]}
        )
        packet = build_packet(
            self.store,
            packet_settings,
            run["parse_id"],
            retriever.chunks,
            selected,
            broad_text=expanded,
            required_regions=required_regions,
        )
        packet.metrics.update(
            preparation_seconds=time.monotonic() - started,
            selection_routes=routes,
            index_id=self.retrieval_context(run)[0]["id"],
        )
        self.store.put(
            "evidence_packets",
            {
                "id": f"{run['id']}:{sid}:{'blind' if complement else 'extract'}:{int(expanded)}",
                "run_id": run["id"],
                "section_id": sid,
                "aliases": packet.aliases,
                "chunk_ids": packet.chunk_ids,
                "metrics": packet.metrics,
            },
        )
        return packet

    def read_retrieved(self, run, sid, *, blind=False):
        model = section_contract(sid)
        stage = f"{'blind' if blind else 'extract'}:{sid}"
        # Single source read: missing/unreadable evidence is recorded, never retried with expansion.
        packet = self.retrieval_packet(run, sid, complement=blind)
        # Keep the initial prompt unchanged so existing successful checkpoints remain reusable.
        # search_requests now describe retrieval gaps for diagnostics only.
        instruction = (
            f"\nRole: {SECTIONS[sid][1]}. {'Independently read' if blind else 'Extract'} "
            f"section {sid}: {SECTIONS[sid][0]}. You have no other extraction's answer.\n"
            f"TARGET: {json.dumps(run['target'], ensure_ascii=False)}\n"
            f"REQUIREMENTS: {json.dumps(requirements(sid))}\n"
            "One finding for each requirement. Extract all rows, qualifiers, units, variants and footnotes. "
            "Use evidence aliases exactly. Keep explanations empty for straightforward supported facts; "
            "explain uncertainty, absence and conflict. Do not repeat facts in prose. "
            "Only pins in section 1, existing application circuit/netlist in section 2. "
            "Internal/block/test diagrams do not establish application wiring. Preserve unobserved vs NC. "
            "search_requests: short source references/search phrases for missing evidence, otherwise []. "
            "An unsuccessful retrieval does not establish not_found; request more evidence when needed. "
            f"Document-wide canonical text search: False.\n{packet.text}"
        )
        output = self.call(
            run["id"],
            stage,
            model,
            instruction,
            packet.images,
            request_metrics=packet.metrics,
            output_budget=16000,
        )
        raw = packet.decode(output.model_dump(mode="json"))
        searches = raw["search_requests"]
        section = expand_section(raw, sid, run["target"]["id"])
        all_allowed = packet.allowed
        all_images = set(packet.image_pages)
        # Missing evidence in this packet cannot certify absence in unseen graphical evidence.
        _, retriever = self.retrieval_context(run)
        unseen = sorted(
            {
                c["page"]
                for c in retriever.chunks
                if (c["kind"] == "visual" or c["unknown_visual"])
                and (not c["package_hint"] or c["package_hint"] == run["target"].get("package_code"))
            }
            - all_images
        )
        unresolved = []
        for finding in section.findings:
            if finding.status == Status.NOT_FOUND and unseen:
                finding.status = Status.AMBIGUOUS
                finding.reason = f"Retrieved source checked, but graphical pages {unseen} were not inspected."
                unresolved.append(finding.requirement_id)
        if unresolved and section.status == Status.NOT_FOUND:
            section.status = Status.AMBIGUOUS
        if searches:
            section.notes.append(
                "Evidence expansion disabled; unresolved evidence requests: " + "; ".join(searches)
            )
            section.status = Status.AMBIGUOUS
        return section, all_allowed, sorted(all_images), packet.metrics, unresolved

    def extract_retrieved(self, run, sid):
        record_id = f"{run['id']}:{sid}"
        try:
            return self.store.get("results", record_id)
        except KeyError:
            pass
        section, allowed, visual, metrics, unresolved = self.read_retrieved(run, sid)
        issues = validate_section(section, run["target"]["id"], allowed, allowed)
        if unresolved:
            issues.append(
                {
                    "code": "incomplete_visual_search",
                    "section_id": sid,
                    "severity": "warning",
                    "requirements": unresolved,
                }
            )
        if section.notes:
            issues.append({"code": "unresolved_evidence_request", "section_id": sid, "severity": "warning"})
        row = {
            "id": record_id,
            "run_id": run["id"],
            "section_id": sid,
            "section": section.model_dump(mode="json"),
            "validation": issues,
            "visual_pages": visual,
            "supplied_region_ids": sorted(allowed),
            "packet_metrics": metrics,
        }
        self.store.put("results", row)
        for finding in section.findings:
            if finding.requirement_id in requirements(sid):
                cov = self.store.get("coverage", f"{run['id']}:{finding.requirement_id}")
                self.store.put(
                    "coverage",
                    {**cov, "extraction_status": finding.status.value, "extraction_visual_pages": visual},
                )
        return row

    def review_retrieved(self, run, sid, record):
        key = f"{run['id']}:{sid}"
        try:
            return self.store.get("reviews", key)
        except KeyError:
            pass
        try:
            blind = self.store.get("blind_reads", key + ":blind")
        except KeyError:
            section, allowed, visual, _, unresolved = self.read_retrieved(run, sid, blind=True)
            blind = {
                "id": key + ":blind",
                "run_id": run["id"],
                "section": section.model_dump(mode="json"),
                "supplied_region_ids": sorted(allowed),
                "visual_pages": visual,
                "unresolved_requirements": unresolved,
            }
            self.store.put("blind_reads", blind)
        required = set(record["supplied_region_ids"]) | set(blind["supplied_region_ids"])
        packet = self.retrieval_packet(run, sid, complement=True, required_regions=required)
        prompt = (
            "\nAudit original and independent extractions against source. Neither is authoritative. "
            "Check correctness, missing facts, qualifiers, conditions, wrong targets and absent decisions. "
            "List only supported original claim IDs and issues; no explanation for passed claims. "
            "Agreement alone is not proof. reviewed_region_ids are source aliases actually inspected.\n"
            f"TARGET: {json.dumps(run['target'])}\nREQUIREMENTS: {json.dumps(requirements(sid))}\n"
            f"ORIGINAL: {json.dumps(packet.encode(record['section']))}\n"
            f"INDEPENDENT: {json.dumps(packet.encode(blind['section']))}\n{packet.text}"
        )
        parsed = self.call(
            run["id"],
            f"review:{sid}",
            Review,
            prompt,
            packet.images,
            request_metrics=packet.metrics,
            output_budget=6000,
        )
        review = Review.model_validate(packet.decode(parsed.model_dump(mode="json")))
        claims = {c["id"] for c in record["section"]["claims"]}
        if not review.reviewed_region_ids or not set(review.supported_claim_ids) <= claims:
            raise ValueError("Review contains unknown claims or has not inspected source")
        for issue in review.issues:
            if issue.requirement_id not in requirements(sid):
                raise ValueError("Review issue has invalid requirement")
        row = {"id": key, "run_id": run["id"], "section_id": sid, **review.model_dump(mode="json")}
        self.store.put("reviews", row)
        for req in requirements(sid):
            cov = self.store.get("coverage", f"{run['id']}:{req}")
            self.store.put(
                "coverage",
                {
                    **cov,
                    "review_status": "issues_found"
                    if any(i.requirement_id == req for i in review.issues)
                    else "reviewed",
                    "reviewed_region_ids": review.reviewed_region_ids,
                },
            )
        return row
