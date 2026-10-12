"""Image coverage respects atomic tables and whole-page visual anchors.

A supplied table crop proves that table was supplied, not that an entire page was
inspected. Correcting a coverage warning restores the reader's limited-source
disposition; it never certifies absence in the full document.
"""

from .models import Finding, SectionResult, Status

COVERAGE_VERSION = "candidate-image-coverage-v1"
LIMITED_ABSENCE = "not_found is limited to supplied evidence; document-wide absence is not certified."


def visual_candidate_coverage(candidates, metrics):
    """Return exact candidate-level image proofs and genuinely uninspected IDs.

    Table crops must list every table region ID on the same source page. Real
    visual anchors and unknown visual pages require a whole-page image. The
    historical image_pages field contains only whole-page attachments, so it is
    a valid compatibility proof, while image_source_pages alone proves nothing.
    """
    descriptors = metrics.get("images", [])
    whole_pages = set(metrics.get("image_pages", [])) | {
        image["page"] for image in descriptors if image.get("kind") == "whole_page"
    }
    visual_candidates, unseen, proofs = [], [], []
    for chunk in candidates:
        if not (chunk.get("visual") or chunk.get("unknown_visual") or chunk.get("kind") == "visual"):
            continue
        visual_candidates.append(chunk["id"])
        page = chunk["page"]
        if page in whole_pages:
            proofs.append({"chunk_id": chunk["id"], "page": page, "kind": chunk.get("kind"),
                           "basis": "whole_page", "source_region_ids": sorted(set(chunk.get("region_ids", [])))})
            continue
        region_ids = set(chunk.get("region_ids", []))
        crops = [image for image in descriptors
                 if image.get("kind") == "table_crop" and image.get("page") == page]
        crop_sources = {rid for image in crops for rid in image.get("source_region_ids", [])}
        if (chunk.get("kind") == "table" and not chunk.get("unknown_visual")
                and region_ids and region_ids <= crop_sources):
            proofs.append({"chunk_id": chunk["id"], "page": page, "kind": "table",
                           "basis": "table_crop", "source_region_ids": sorted(region_ids)})
        else:
            unseen.append({"chunk_id": chunk["id"], "page": page, "kind": chunk.get("kind"),
                           "requires_whole_page": chunk.get("kind") != "table" or bool(chunk.get("unknown_visual")),
                           "source_region_ids": sorted(region_ids)})
    return {"version": COVERAGE_VERSION, "visual_candidate_ids": sorted(set(visual_candidates)),
            "uninspected_chunk_ids": sorted({row["chunk_id"] for row in unseen}),
            "uninspected_pages": sorted({row["page"] for row in unseen}),
            "uninspected": unseen, "proofs": proofs}


def reconcile_absence_coverage(section, packet_metrics, candidates_by_requirement):
    """Conservatively undo only coverage-generated false uncertainty in a copy.

    The caller re-selects candidates using this run's frozen source/index/target.
    Missing candidate context, omitted source, unresolved requests/figures or any
    positive/unresolved claim veto restoration. Evidence and before/provenance
    snapshots remain available in the audit. Original section records stay intact.
    """
    original = section if isinstance(section, SectionResult) else SectionResult.model_validate(section)
    result = original.model_copy(deep=True)
    audit = {"version": COVERAGE_VERSION, "decisions": []}
    if (packet_metrics.get("unresolved_evidence_requests") or packet_metrics.get("omitted_chunk_ids")
            or packet_metrics.get("omitted_candidate_ids") or packet_metrics.get("omitted_bundles")
            or packet_metrics.get("mandatory_overflow")):
        return result, audit
    provenance = packet_metrics.get("absence_coverage_uncertainty", {})
    for finding in result.findings:
        req = finding.requirement_id
        uncertainty = provenance.get(req, {})
        if not (finding.status == Status.AMBIGUOUS and uncertainty.get("reported_status") == "not_found"
                and uncertainty.get("effective_status") == "ambiguous"):
            continue
        if (uncertainty.get("omitted_candidate_ids") or uncertainty.get("source_figures_unresolved")
                or req not in candidates_by_requirement or not candidates_by_requirement[req]
                or any(claim.requirement_id == req and claim.status not in {Status.NOT_FOUND, Status.NOT_APPLICABLE}
                       for claim in result.claims)):
            continue
        coverage = visual_candidate_coverage(candidates_by_requirement[req], packet_metrics)
        if not coverage["visual_candidate_ids"] or coverage["uninspected_chunk_ids"]:
            continue
        # If conversion recorded the exact visual set, a changed candidate set is
        # not proof of the original read's coverage and cannot undo its warning.
        recorded_candidates = uncertainty.get("visual_candidate_ids")
        if recorded_candidates is not None and set(recorded_candidates) != set(coverage["visual_candidate_ids"]):
            continue
        raw = uncertainty.get("reported_finding")
        if raw is not None:
            reported = Finding.model_validate(raw)
            if reported.requirement_id != req or reported.status != Status.NOT_FOUND or not reported.reason.strip():
                continue
            restored_evidence = reported.evidence_ids
            restored_reason = reported.reason
        else:
            # Older records lost the reader's original reason. Say explicitly
            # that this explanation is generated from retained status/proofs.
            restored_evidence = finding.evidence_ids
            restored_reason = (
                "The reader reported not_found; its original reason was not retained. "
                "The generated graphical-coverage warning is corrected because all relevant image candidates were supplied."
            )
        before = finding.model_dump(mode="json")
        finding.status = Status.NOT_FOUND
        finding.evidence_ids = list(restored_evidence)
        finding.reason = f"{restored_reason} Scope: {LIMITED_ABSENCE}".strip()
        audit["decisions"].append({
            "requirement_id": req, "action": "restore_reported_absence_with_limited_source_scope",
            "before_finding": before, "after_finding": finding.model_dump(mode="json"),
            "coverage_provenance": uncertainty, "candidate_image_coverage": coverage,
        })
    return result, audit
