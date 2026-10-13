"""Versioned retrieval pipeline; legacy runs retain their original prompts and mapping."""

import json
import time

from .contracts import (
    cited_regions,
    columnar_review,
    compact_review,
    expand_section,
    output_budget,
    section_contract,
    shared_review_texts,
)
from .document_index import ensure_index
from .evidence_packet import build_packet
from .models import Review, Status
from .providers import strict_schema
from .requirements import SECTIONS, requirements
from .retrieval import Retriever
from .review_safety import review_contract, review_source_regions, target_identity
from .token_budget import estimate_text_tokens, request_input_metrics
from .validation import validate_section


class RetrievalWorkflow:
    def request_reserved_tokens(self, run, sid, model, answers=()):
        # Reserve what is actually serialized: review drafts use short packet-local aliases.
        # A nine-digit placeholder is conservatively longer than aliases in a bounded packet;
        # estimating repeated canonical IDs instead can reject a request that actually fits.
        def aliased(value, key=""):
            if isinstance(value, dict):
                return {k: aliased(v, k) for k, v in value.items()}
            if isinstance(value, list):
                if key in {"evidence_ids", "reviewed_region_ids"}:
                    return ["e000000000" for _ in value]
                return [aliased(item) for item in value]
            return value

        values = [strict_schema(model), run["target"], requirements(sid), *(aliased(a) for a in answers)]
        schema_overhead = estimate_text_tokens(json.dumps(strict_schema(model))) if self.settings.model.startswith("cx/") else 0
        return 2048 + schema_overhead + sum(
            estimate_text_tokens(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
            for value in values
        )

    def packet_limits(self, run, sid, role, reserved_tokens=0):
        config = run["retrieval_config"]
        heavy = sid in {1, 2, 3, 7, 9, 10, 14, 15, 16}
        multiplier = 1.5 if heavy else 1.0
        # Review needs cited evidence plus a bounded omission audit for every requirement.
        if role == "review":
            multiplier *= 1.5
        # Two independent readers can cite disjoint images. The review role has a
        # predefined union allowance; source and complete-request token caps still apply.
        image_count = config["max_images"] if heavy else min(4, config["max_images"])
        return {
            "text_tokens": int(config["text_token_budget"] * multiplier),
            "image_tokens": int(config["image_token_budget"] * (1.5 if heavy else 1)),
            "max_images": image_count * (2 if role == "review" else 1),
            "total_tokens": max(0, config["request_token_budget"] - reserved_tokens),
        }

    def discovery_packet(self, parse_id):
        optimized = self.settings.optimization == "budget-v1"
        _, chunks = ensure_index(self.store, parse_id, version="structural-9" if optimized else "structural-8")
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
        # Discovery retains all ordering variants: its mandatory evidence must not be
        # narrowed to the subsequently selected target or silently clipped by a budget.
        return build_packet(self.store, self.settings, parse_id, chunks, selected)

    def retrieval_context(self, run):
        config = run["retrieval_config"]
        key = json.dumps([run["parse_id"], config], sort_keys=True)
        if not hasattr(self, "_retrievers"):
            self._retrievers = {}
        if key not in self._retrievers:
            manifest, chunks = ensure_index(
                self.store, run["parse_id"], version=config.get("index_version", "structural-8")
            )
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
        self, run, sid, *, complement=False, expanded=False, required_regions=(), searches=(), role=None,
        reserved_tokens=None,
    ):
        started = time.monotonic()
        _, retriever = self.retrieval_context(run)
        selected, routes = set(), {}
        for req in requirements(sid):
            ids, why = retriever.select(req, run["target"], complement=complement, expanded=expanded)
            selected.update(ids)
            for cid, route in why.items():
                previous = routes.get(cid)
                if previous is None or (route.get("rank_score") or 0) > (previous.get("rank_score") or 0):
                    routes[cid] = route
        # Reviewer must see every source cited by either extraction, including expanded reads.
        additional = {c["id"] for c in retriever.chunks if set(c["region_ids"]) & set(required_regions)}
        for search in searches:
            additional.update(cid for cid, _ in retriever.rank(search, run["retrieval_config"]["top_k"] * 3))
        selected.update(retriever.expand(additional, run["target"]))
        packet_settings = self.settings.model_copy(
            update={"packet_text_budget": run["retrieval_config"]["text_budget"]}
        )
        role = role or ("blind" if complement else "extract")
        optimized = run.get("packet_version") == "budget-v1" and not expanded
        if optimized and reserved_tokens is None:
            model = Review if role == "review" else section_contract(sid, version=run["contract_version"])
            reserved_tokens = self.request_reserved_tokens(run, sid, model)
        packet = build_packet(
            self.store,
            packet_settings,
            run["parse_id"],
            retriever.chunks,
            selected,
            broad_text=expanded,
            required_regions=required_regions,
            **(
                {"budget": self.packet_limits(run, sid, role, reserved_tokens), "priority": routes, "role": role}
                if optimized else {}
            ),
        )
        packet.metrics.update(
            preparation_seconds=time.monotonic() - started,
            selection_routes=routes,
            index_id=self.retrieval_context(run)[0]["id"],
        )
        self.store.put(
            "evidence_packets",
            {
                "id": f"{run['id']}:{sid}:{role}:{int(expanded)}",
                "run_id": run["id"],
                "section_id": sid,
                "role": role,
                "aliases": packet.aliases,
                "chunk_ids": packet.chunk_ids,
                "metrics": packet.metrics,
            },
        )
        return packet

    def read_retrieved(self, run, sid, *, blind=False):
        version = run.get("contract_version", "exact-v1")
        if sid == 2 and run.get("quality_version") == "quality-v1":
            from .figure_workflow import read_figures, source_app_figures
            if source_app_figures(self, run):
                return read_figures(self, run, blind=blind)
        model = section_contract(sid, version=version)
        stage = f"{'blind' if blind else 'extract'}:{sid}"
        # Single source read: missing/unreadable evidence is recorded, never retried with expansion.
        packet = self.retrieval_packet(run, sid, complement=blind)
        if packet.metrics.get("mandatory_overflow"):
            raise ValueError(f"Section {sid}: required source bundle exceeds packet budget; inspect packet metrics.")
        if run.get("quality_version") == "quality-v1" and not packet.allowed:
            from .models import SectionResult
            self.event(run["id"], stage, "started", kind="code")
            section = SectionResult(section_id=sid, status=Status.AMBIGUOUS,
                findings=[{"requirement_id":req, "status":"ambiguous", "evidence_ids":[],
                           "reason":"No source supplied to this reader; absence is not established."}
                          for req in requirements(sid)],
                claims=[], pins=[], circuits=[], notes=["Source reading skipped: empty evidence packet."])
            packet.metrics.update(source_read_method="skipped_no_source",
                                  unresolved_evidence_requests=["source_not_supplied"])
            self.event(run["id"], stage, "completed", kind="code", method="skipped_no_source")
            return section, set(), [], packet.metrics, list(requirements(sid))
        # Keep the initial prompt unchanged so existing successful checkpoints remain reusable.
        # search_requests now describe retrieval gaps for diagnostics only.
        instruction = (
            f"\nRole: {SECTIONS[sid][1]}. {'Independently read' if blind else 'Extract'} "
            f"section {sid}: {SECTIONS[sid][0]}. You have no other extraction's answer.\n"
            f"TARGET: {json.dumps(target_identity(run['target']) if run.get('quality_version')=='quality-v1' else run['target'], ensure_ascii=False)}\n"
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
        if version == "compact-v2":
            instruction = instruction.replace(
                "One finding for each requirement.",
                "One finding for each requirement. Record only facts applying to the selected ordering code, "
                "grade and package, or explicitly shared family facts. Other grades are not target facts. "
                "Use concise source-faithful rows; each fact once. Numeric bounds must follow the actual "
                "MIN/TYP/MAX columns; nominal values are not automatically typical. Preserve the exact "
                "source lexeme in raw_value. Use expression for formulas or unclassified numbers. "
                "Do not extend merged-cell conditions beyond their source rows. In plain claims, value "
                "retains the source statement. Keep reason empty for clear supported facts. "
                "Omitted candidates do not establish absence. "
            )
            instruction += "\nUse the compact-v2 schema; public IDs and display values are generated by code."
        if run.get("quality_version") == "quality-v1":
            from .quality_policy import quality_instructions
            instruction += "\n" + quality_instructions(sid)
        metrics = {**packet.metrics, "contract_version": version}
        if run.get("packet_version") == "budget-v1":
            metrics["request_token_budget"] = run["retrieval_config"]["request_token_budget"]
        call = self.validated_call if run.get("quality_version") == "quality-v1" else self.call
        raw = call(
            run["id"],
            stage,
            model,
            instruction,
            packet.images,
            request_metrics=metrics,
            output_budget=output_budget(sid, "blind" if blind else "extract") if version == "compact-v2" else 16000,
            **({"validate":lambda output: packet.decode(output.model_dump(mode="json"))}
               if run.get("quality_version") == "quality-v1" else {}),
        )
        if run.get("quality_version") != "quality-v1":
            raw = packet.decode(raw.model_dump(mode="json"))
        searches = raw["search_requests"]
        section = expand_section(raw, sid, run["target"]["id"], version=version)
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
        coverage_uncertainty = {}
        reported_section_status = section.status.value
        for finding in section.findings:
            relevant_unseen = unseen
            relevant_omitted = packet.metrics.get("omitted_chunk_ids", [])
            candidate_coverage = None
            if run.get("quality_version") == "quality-v1":
                from .coverage_uncertainty import visual_candidate_coverage

                candidates, _ = retriever.select(finding.requirement_id, run["target"], complement=blind)
                candidate_coverage = visual_candidate_coverage(
                    [retriever.by_id[cid] for cid in candidates], packet.metrics,
                )
                relevant_unseen = candidate_coverage["uninspected_pages"]
                relevant_omitted = sorted(set(relevant_omitted) & set(candidates))
            if finding.status == Status.NOT_FOUND and (relevant_unseen or relevant_omitted):
                reported_finding = finding.model_dump(mode="json")
                finding.status = Status.AMBIGUOUS
                finding.reason = (
                    f"Retrieved source checked, but graphical pages {relevant_unseen} or omitted candidate bundles "
                    "were not inspected. Absence is not certified."
                )
                unresolved.append(finding.requirement_id)
                coverage_uncertainty[finding.requirement_id] = {
                    "reported_status": "not_found", "effective_status": "ambiguous",
                    "supplied_region_ids": sorted(packet.allowed), "uninspected_candidate_pages": relevant_unseen,
                    "omitted_candidate_ids": relevant_omitted,
                    **({"reported_finding": reported_finding, "reported_section_status": reported_section_status,
                        "visual_candidate_ids": candidate_coverage["visual_candidate_ids"],
                        "candidate_image_coverage": candidate_coverage} if candidate_coverage is not None else {}),
                }
        if run.get("quality_version") == "quality-v1":
            packet.metrics["absence_coverage_uncertainty"] = coverage_uncertainty
            packet.metrics["unresolved_evidence_requests"] = searches
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
        issues = validate_section(section, run["target"]["id"], allowed, allowed,
                                  coverage_uncertainty=metrics.get("absence_coverage_uncertainty"),
                                  allow_explicit_nc_stub=run.get("quality_version") == "quality-v1")
        if unresolved:
            issues.append(
                {
                    "code": "incomplete_visual_search",
                    "section_id": sid,
                    "severity": "warning",
                    "requirements": unresolved,
                }
            )
        if (section.notes if run.get("quality_version") != "quality-v1"
                else metrics.get("unresolved_evidence_requests")):
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

    def blind_retrieved(self, run, sid):
        key = f"{run['id']}:{sid}"
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
        return blind

    def _prepare_review_request(self, run, sid, record, blind, packet, original, independent, scope=None,
                                *, partition_reason=None):
        optimized = run.get("packet_version") == "budget-v1"
        quality = run.get("quality_version") == "quality-v1"
        overflow = packet.metrics.get("mandatory_overflow", {})
        request_requirements = requirements(sid)
        if scope is not None:
            request_requirements = {req: request_requirements[req] for req in scope}
        original_claims = [c for c in record["section"]["claims"] if c["requirement_id"] in request_requirements]
        response_model = review_contract(original_claims, request_requirements) if quality else Review
        dumps = (lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))) if optimized else json.dumps
        prompt = (
            "\nAudit original and independent extractions against source. Neither is authoritative. "
            "Check correctness, missing facts, qualifiers, conditions, wrong targets and absent decisions. "
            "List only supported original claim IDs and issues; no explanation for passed claims. "
            "Agreement alone is not proof. reviewed_region_ids are source aliases actually inspected.\n"
            f"TARGET: {json.dumps(target_identity(run['target']) if quality else run['target'])}\nREQUIREMENTS: {json.dumps(request_requirements)}\n"
            f"ORIGINAL: {dumps(packet.encode(original))}\n"
            f"INDEPENDENT: {dumps(packet.encode(independent))}\n{packet.text}"
        )
        if optimized:
            reverse = {region: alias for alias, region in packet.aliases.items()}
            extra_original = sorted(
                reverse[rid] for rid in packet.allowed - set(record["supplied_region_ids"])
            )
            extra_independent = sorted(
                reverse[rid] for rid in packet.allowed - set(blind["supplied_region_ids"])
            )
            prompt += (
                "\nBoth answers use compact views; omitted empty fields do not mean source absence. "
                "Check every requirement against the omission-audit source, including agreed facts. "
                "Cited numeric source lexemes and bounds are preserved. Inspect target scope and "
                "table column/merged-cell conditions. Output only supported original claim IDs and "
                "concise issues, not a replacement extraction. "
                "The lists below identify evidence supplied to you but not to the respective first reader. "
                "Treat missing facts from these sources as reader coverage gaps. A true statement that "
                "a source was not supplied is not false merely because you can now see that source.\n"
                f"ORIGINAL_ADDITIONAL_SOURCE: {json.dumps(extra_original)}\n"
                f"INDEPENDENT_ADDITIONAL_SOURCE: {json.dumps(extra_independent)}"
            )
        if scope is not None:
            prompt += (
                "\nPartitioned review v1: review ONLY the REQUIREMENTS listed above. Both draft views "
                "contain ALL claims and findings for those requirements, including empty findings. "
                "Other requirements are audited by other parts; do not infer absence from their omission. "
                "Global pins, complete circuits and notes are retained intact. Every part receives the "
                "same frozen source packet, including the omission-audit evidence. Inspect each listed "
                "requirement for missing facts as well as wrong claims. supported_claim_ids and non-null "
                "issue.claim_id MUST be original IDs shown in this part, never independent IDs or row numbers. "
                "Each issue.requirement_id must be in this part's REQUIREMENTS."
            )
        presence_index = None
        if quality:
            prompt += (
                "\nREFERENCE_RULES: supported_claim_ids and non-null issue.claim_id refer ONLY to "
                "original claim IDs. Requirement IDs identify findings, not claims. An issue about "
                "a finding, a missing fact or an independent-reader error has claim_id=null and "
                "the appropriate requirement_id; describe the independent error in explanation. "
                "If ORIGINAL_CLAIM_IDS is empty, supported_claim_ids MUST be [].\n"
                f"ORIGINAL_CLAIM_IDS: {dumps([c['id'] for c in original_claims])}"
                "\nReview quality v1: missing_fact means a fact absent from the entire ORIGINAL section, "
                "not a fact recorded under another requirement. Read the complete relevant original "
                "claim, including its conditions, before alleging a missing_condition. A limitation "
                "already stated in conditions is not missing because it is not repeated in value. "
                "wrong_target is reserved for another product, ordering code, package or temperature "
                "grade; requirement misclassification is an incorrect issue with an explicit placement "
                "explanation, not wrong_target. Source applicability must be checked independently. "
                "For a single precise absence allegation, use the full explanation 'Missing fact: "
                "<complete exact fact>' or 'Missing condition: <complete exact condition>' only when "
                "that literal completely states the allegation. Never bundle facts, abbreviate "
                "qualifiers, or use that form for wrong-value/source-conflict allegations. Generic "
                "explanatory issues stay appropriate when no one exact literal suffices."
            )
            if scope is not None:
                from .review_context import PRESENCE_INSTRUCTIONS, presence_inventory

                presence_index = presence_inventory(
                    compact_review(record["section"], run["target"]["id"]), scope,
                )
                prompt += f"\nORIGINAL_PRESENCE_INDEX: {dumps(presence_index)}\n{PRESENCE_INSTRUCTIONS}"
            if partition_reason == "gateway_504_recovery":
                prompt += (
                    "\nREVIEW_PARTITION_POLICY: gateway_504_recovery. This part audits one complete "
                    "requirement after repeated upstream HTTP 504 failures. Both local draft answers, "
                    "all global pin/circuit context and the frozen source packet remain intact. "
                    "Apply the same source-based correctness and omission checks; transport recovery "
                    "does not weaken evidence requirements or certify other parts."
                )
        generation_budget = output_budget(sid, "review", rows=len(record["section"]["claims"])) if optimized else 6000
        transient = {"review_original_chars", "review_independent_chars", "draft_encoding",
                     "uncompressed_preflight", "serialized_preflight", "draft_text_pool_entries",
                     "request_token_budget", "reservation_warning"}
        metrics = {**{k: v for k, v in packet.metrics.items() if k not in transient},
                   "review_original_chars": len(dumps(original)),
                   "review_independent_chars": len(dumps(independent))}
        if quality:
            metrics["review_quality_version"] = "quality-v1"
            if presence_index is not None:
                metrics["review_presence_index_version"] = presence_index["version"]
                metrics["review_presence_index_claims"] = presence_index["claim_count"]
                metrics["review_presence_index_tokens"] = estimate_text_tokens(dumps(presence_index))
            if partition_reason:
                metrics["review_partition_reason"] = partition_reason
        if optimized:
            from .engine import POLICY

            serialized = POLICY + prompt + (
                f"\nOutput token target: {generation_budget}. Be concise; preserve facts and conditions."
            )
            exact_input = request_input_metrics(serialized, strict_schema(response_model), packet.images, model=self.settings.model)
            if exact_input["input_tokens_estimate"] > run["retrieval_config"]["request_token_budget"]:
                # Change only draft serialization, never the evidence selection or cap.
                # Preserve normal prompts/checkpoints for requests that already fit.
                original_dump, independent_dump = dumps(packet.encode(original)), dumps(packet.encode(independent))
                prompt = prompt.replace(
                    f"ORIGINAL: {original_dump}\n",
                    f"ORIGINAL: {dumps(columnar_review(packet.encode(original)))}\n", 1,
                ).replace(
                    f"INDEPENDENT: {independent_dump}\n",
                    f"INDEPENDENT: {dumps(columnar_review(packet.encode(independent)))}\n", 1,
                )
                prompt += (
                    "\nDraft encoding: columnar-v1. Each claim_tables item has columns and rows. "
                    "Read each row as the object obtained by pairing its cells with those column names. "
                    "Different field sets have separate tables; no facts or qualifiers were removed. "
                    "Public claim IDs in the id column are unchanged. Return those IDs, not row numbers."
                )
                metrics["draft_encoding"] = "columnar-v1"
                metrics["uncompressed_preflight"] = exact_input
                serialized = POLICY + prompt + (
                    f"\nOutput token target: {generation_budget}. Be concise; preserve facts and conditions."
                )
                exact_input = request_input_metrics(serialized, strict_schema(response_model), packet.images, model=self.settings.model)
                if exact_input["input_tokens_estimate"] > run["retrieval_config"]["request_token_budget"]:
                    views = [columnar_review(packet.encode(original)), columnar_review(packet.encode(independent))]
                    pool, packed = shared_review_texts(views)
                    if pool:
                        prompt = prompt.replace(
                            f"ORIGINAL: {dumps(views[0])}\n", f"ORIGINAL: {dumps(packed[0])}\n", 1,
                        ).replace(
                            f"INDEPENDENT: {dumps(views[1])}\n", f"INDEPENDENT: {dumps(packed[1])}\n", 1,
                        )
                        prompt += (
                            "\nSHARED_DRAFT_TEXT: " + dumps(pool)
                            + "\nAny draft value {text_ref:tN} means the exact string at key tN in "
                            "SHARED_DRAFT_TEXT. Expand it before checking the column/claim. "
                            "References are serialization only, not evidence or new claim IDs."
                        )
                        metrics["draft_encoding"] = "columnar-v1+shared-text-v1"
                        metrics["draft_text_pool_entries"] = len(pool)
                        serialized = POLICY + prompt + (
                            f"\nOutput token target: {generation_budget}. Be concise; preserve facts and conditions."
                        )
                        exact_input = request_input_metrics(serialized, strict_schema(response_model), packet.images, model=self.settings.model)
            metrics["serialized_preflight"] = exact_input
            metrics["request_token_budget"] = run["retrieval_config"]["request_token_budget"]
            if overflow:
                metrics["reservation_warning"] = overflow
                if exact_input["input_tokens_estimate"] <= run["retrieval_config"]["request_token_budget"]:
                    metrics["mandatory_overflow"] = {}
        return {"prompt": prompt, "metrics": metrics, "output_budget": generation_budget, "response_model": response_model,
                "input_tokens_estimate": exact_input["input_tokens_estimate"] if optimized else 0}

    @staticmethod
    def _validate_review_part(review, scope, claims, *, allow_declined=False):
        by_id = {c["id"]: c for c in claims}
        if ((not review.reviewed_region_ids and (review.supported_claim_ids or not allow_declined))
                or not set(review.supported_claim_ids) <= set(by_id)):
            raise ValueError("Review contains unknown claims or has not inspected source")
        for issue in review.issues:
            if issue.requirement_id not in scope:
                raise ValueError("Review issue has invalid requirement")
            if issue.claim_id is not None and (
                issue.claim_id not in by_id or by_id[issue.claim_id]["requirement_id"] != issue.requirement_id
            ):
                raise ValueError("Review issue has invalid original claim or requirement association")

    def _review_gateway_504_recovery(self, run, sid):
        """Only a recorded exhaustion of three upstream 504 attempts permits recovery."""
        import re

        if run.get("quality_version") != "quality-v1" or run.get("packet_version") != "budget-v1":
            return None
        for event in sorted(self.store.find("events", {"run_id": run["id"], "status": "failed"}),
                            key=lambda row: (str(row.get("time", "")), row["id"]), reverse=True):
            if not re.fullmatch(rf"review:{sid}(?::part:\d+)?", str(event.get("stage", ""))):
                continue
            telemetry = event.get("telemetry", {})
            if (not isinstance(telemetry, dict) or event.get("kind") not in {None, "llm"}
                    or telemetry.get("code") != "gateway_error"):
                continue
            attempts = telemetry.get("attempts", [])
            if not isinstance(attempts, list):
                continue
            failed = {attempt["attempt"] for attempt in attempts if isinstance(attempt, dict)
                      and attempt.get("status") == "failed" and attempt.get("http_status") == 504
                      and isinstance(attempt.get("attempt"), int) and not isinstance(attempt.get("attempt"), bool)
                      and attempt["attempt"] > 0}
            if len(failed) >= 3:
                return {"failed_event_id": event["id"], "gateway_504_attempts": len(failed),
                        "failed_stage": event["stage"]}
        return None

    def _review_batched(self, run, sid, record, blind, packet, original, independent,
                        *, max_requirements_per_part=None, partition_reason=None, recovery=None):
        from hashlib import sha256

        from .review_batches import merge_review_batches, plan_review_batches

        limit = run["retrieval_config"]["request_token_budget"]
        plan = plan_review_batches(
            original, independent, list(requirements(sid)),
            lambda left, right, scope: self._prepare_review_request(
                run, sid, record, blind, packet, left, right, scope, partition_reason=partition_reason), limit, sid,
            max_requirements_per_part=max_requirements_per_part,
        )
        # All parts are preflighted before any model request. The source packet is
        # identical in every part: no re-retrieval, added pages, or clipped conditions.
        from .engine import POLICY
        from .providers import LLMRequest

        fingerprints = [LLMRequest(
            f"review:{sid}:part:{i}", POLICY + part["prompt"] + (
                f"\nOutput token target: {part['output_budget']}. Be concise; preserve facts and conditions."
            ), strict_schema(part["response_model"]), packet.images, output_budget=part["output_budget"],
        ).fingerprint(self.settings.model, self.settings.reasoning) for i, part in enumerate(plan, 1)]
        policy = ({"partition_reason": partition_reason,
                   "max_requirements_per_part": max_requirements_per_part} if partition_reason else {})
        plan_hash = sha256(json.dumps({"version": "requirement-v1", "provider": run["provider"],
            "aliases": packet.aliases, "chunk_ids": packet.chunk_ids, "fingerprints": fingerprints, **policy},
            sort_keys=True).encode()).hexdigest()
        key = f"{run['id']}:{sid}"
        plan_id = key + ":review-batches-v1"
        self.store.put("review_batch_plans", {
            "id": plan_id, "run_id": run["id"], "section_id": sid,
            "version": "requirement-v1", "plan_hash": plan_hash,
            **policy, **({"recovery": recovery} if recovery else {}),
            "source_region_ids": sorted(packet.allowed), "chunk_ids": packet.chunk_ids,
            "parts": [{"scope": part["scope"], "original_claim_ids": part["original_claim_ids"],
                       "input_tokens_estimate": part["input_tokens_estimate"]} for part in plan],
        })
        self.event(run["id"], f"review:{sid}", "started", kind="code", batch_count=len(plan))
        reviews = []
        for i, part in enumerate(plan, 1):
            part_id = f"{key}:{plan_hash}:part:{i}"
            claims = [c for c in record["section"]["claims"] if c["requirement_id"] in part["scope"]]
            try:
                saved = self.store.get("review_parts", part_id)
                review = Review.model_validate(saved["review"])
            except KeyError:
                part["metrics"].update(review_partition_version="requirement-v1", review_part=i,
                                       review_parts=len(plan), review_requirements=part["scope"])
                if recovery:
                    part["metrics"]["gateway_504_recovery"] = recovery
                def validate_part(parsed, scope=part["scope"], originals=claims):
                    decoded = Review.model_validate(packet.decode(parsed.model_dump(mode="json")))
                    self._validate_review_part(decoded, scope, originals, allow_declined=run.get("quality_version")=="quality-v1")
                    return decoded
                review = self.validated_call(
                    run["id"], f"review:{sid}:part:{i}", part["response_model"], part["prompt"], packet.images,
                    request_metrics=part["metrics"], output_budget=part["output_budget"],
                    validate=validate_part,
                )
                self.store.put("review_parts", {"id": part_id, "run_id": run["id"], "section_id": sid,
                    "plan_hash": plan_hash, "part": i, "scope": part["scope"],
                    "review": review.model_dump(mode="json")})
            self._validate_review_part(review, part["scope"], claims, allow_declined=run.get("quality_version")=="quality-v1")
            if not (set(review.reviewed_region_ids) | cited_regions(review.model_dump(mode="json"))) <= packet.allowed:
                raise ValueError("Saved review part cites unsupplied evidence")
            reviews.append(review)
        return merge_review_batches(reviews), {"partition_version": "requirement-v1",
            "partition_count": len(plan), "partition_plan_id": plan_id, "partition_plan_hash": plan_hash,
            **policy, **({"gateway_504_recovery": recovery} if recovery else {})}

    def review_retrieved(self, run, sid, record):
        try:
            return self._review_retrieved(run, sid, record)
        except Exception as error:
            # Inference completion is not semantic/persistence completion.
            # Keep logical progress accurate for failed decode, planning and writes.
            if not getattr(error, "_datasheet_stage", None):
                error._datasheet_stage = f"review:{sid}"
            self.event(run["id"], f"review:{sid}", "failed", kind="code")
            raise

    def _review_retrieved(self, run, sid, record):
        from .errors import ReviewBudgetError

        if sid == 2 and run.get("quality_version") == "quality-v1" and record.get("packet_metrics", {}).get("figure_workflow_version"):
            from .figure_workflow import review_figures
            return review_figures(self, run, record)

        key = f"{run['id']}:{sid}"
        try:
            return self.store.get("reviews", key)
        except KeyError:
            pass
        blind = self.blind_retrieved(run, sid)
        optimized = run.get("packet_version") == "budget-v1"
        if optimized:
            required = cited_regions(record["section"]) | cited_regions(blind["section"])
            if run.get("quality_version") == "quality-v1":
                required = review_source_regions(record, blind, required)
            original = compact_review(record["section"], run["target"]["id"])
            independent = compact_review(blind["section"], run["target"]["id"])
        else:
            required = set(record["supplied_region_ids"]) | set(blind["supplied_region_ids"])
            original, independent = record["section"], blind["section"]
        packet = self.retrieval_packet(
            run, sid, complement=True, required_regions=required, role="review",
            reserved_tokens=self.request_reserved_tokens(run, sid, Review, (original, independent))
            if optimized else None,
        )
        if run.get("quality_version") == "quality-v1" and not packet.allowed:
            review = Review(supported_claim_ids=[], reviewed_region_ids=[], issues=[
                {"category":"insufficient_evidence", "claim_id":None, "requirement_id":req,
                 "severity":"warning", "evidence_ids":[],
                 "explanation":"Source review not performed: no evidence supplied; requirement remains unresolved."}
                for req in requirements(sid)])
            row = {"id":key, "run_id":run["id"], "section_id":sid,
                   **review.model_dump(mode="json"), "review_method":"skipped_no_source"}
            self.store.put("reviews",row)
            for req in requirements(sid):
                cov = self.store.get("coverage", f"{run['id']}:{req}")
                self.store.put("coverage", {**cov, "review_status":"skipped_no_source", "reviewed_region_ids":[]})
            self.event(run["id"],f"review:{sid}","completed",kind="code",method="skipped_no_source")
            return row
        prepared = self._prepare_review_request(run, sid, record, blind, packet, original, independent)
        overflow = packet.metrics.get("mandatory_overflow", {})
        if overflow and set(overflow) != {"total_tokens"}:
            if optimized:
                raise ReviewBudgetError(sid, prepared["input_tokens_estimate"],
                                       run["retrieval_config"]["request_token_budget"], "cited_evidence_bundle")
            raise ValueError(f"Review {sid}: cited evidence exceeds packet budget; inspect packet metrics.")
        recovery = self._review_gateway_504_recovery(run, sid)
        batched = optimized and (prepared["input_tokens_estimate"] > run["retrieval_config"]["request_token_budget"]
                                 or recovery is not None)
        packet.metrics.update(prepared["metrics"])
        if optimized:
            saved = self.store.get("evidence_packets", f"{run['id']}:{sid}:review:0")
            self.store.put("evidence_packets", {**saved, "metrics": prepared["metrics"]})
        partition = {}
        if batched:
            review, partition = self._review_batched(
                run, sid, record, blind, packet, original, independent,
                max_requirements_per_part=1 if recovery else None,
                partition_reason="gateway_504_recovery" if recovery else None, recovery=recovery,
            )
        else:
            def validate_review(parsed):
                decoded = Review.model_validate(packet.decode(parsed.model_dump(mode="json")))
                self._validate_review_part(decoded, requirements(sid), record["section"]["claims"], allow_declined=run.get("quality_version")=="quality-v1")
                return decoded
            review = self.validated_call(run["id"], f"review:{sid}", prepared["response_model"], prepared["prompt"], packet.images,
                request_metrics=prepared["metrics"], output_budget=prepared["output_budget"], validate=validate_review)
        reconciliation = {}
        if run.get("quality_version") == "quality-v1" and not review.reviewed_region_ids:
            raw_review = review.model_dump(mode="json")
            covered = {i.requirement_id for i in review.issues if i.category=="insufficient_evidence"}
            from .models import ReviewIssue
            review.issues.extend(ReviewIssue(category="insufficient_evidence",claim_id=None,requirement_id=req,
                severity="warning",evidence_ids=[],explanation="Reviewer declined source inspection; this requirement was not source-reviewed.")
                for req in requirements(sid) if req not in covered)
            reconciliation.update(review_method="declined_source_inspection",raw_declined_review=raw_review)
        if run.get("quality_version") == "quality-v1":
            from .review_context import reconcile_review

            review, audit = reconcile_review(review, record["section"])
            reconciliation.update(review_reconciliation=audit,quality_version="quality-v1")
        row = {"id": key, "run_id": run["id"], "section_id": sid, **review.model_dump(mode="json"), **partition}
        row.update(reconciliation)
        self.store.put("reviews", row)
        for req in requirements(sid):
            cov = self.store.get("coverage", f"{run['id']}:{req}")
            self.store.put("coverage", {**cov, "review_status": "issues_found"
                if any(i.requirement_id == req for i in review.issues) else "reviewed",
                "reviewed_region_ids": review.reviewed_region_ids})
        if batched:
            self.event(run["id"], f"review:{sid}", "completed", kind="code", **partition)
        return row
