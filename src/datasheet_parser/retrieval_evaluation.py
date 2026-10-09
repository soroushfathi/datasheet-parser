"""Measure reference citation coverage independently of model answer quality."""

import re


def citation_coverage(reference, packets):
    expected = {}
    for section in reference["sections"]:
        sid = section["section_id"]
        for finding in section["findings"]:
            expected.setdefault(finding["requirement_id"], set()).update(finding["evidence_ids"])
        for claim in section["claims"]:
            expected.setdefault(claim["requirement_id"], set()).update(claim["evidence_ids"])

        def collect(value):
            result = set()
            if isinstance(value, dict):
                result.update(value.get("evidence_ids", []))
                for item in value.values():
                    result.update(collect(item))
            elif isinstance(value, list):
                for item in value:
                    result.update(collect(item))
            return result

        for key, reqs in [
            ("pins", ["1.pads", "1.functions", "1.connections"]),
            ("circuits", ["2.circuits", "2.components", "2.connections"]),
        ]:
            for req in reqs:
                if req.startswith(f"{sid}."):
                    expected.setdefault(req, set()).update(collect(section[key]))
    rows = []
    for req, ids in sorted(expected.items()):
        packet = packets[int(req.split(".")[0])]
        exact = ids & packet.allowed
        represented = (ids - exact) & packet.covered_regions
        visual = {
            rid
            for rid in ids - exact - represented
            if (match := re.search(r":p(\d+)(?::|$)", rid)) and int(match.group(1)) in packet.image_pages
        }
        missing = ids - exact - visual - represented
        rows.append(
            {
                "requirement_id": req,
                "expected_citations": len(ids),
                "exact": len(exact),
                "covered_by_table_or_complete_page_text": len(represented),
                "covered_by_page_image": len(visual),
                "missing": sorted(missing),
                "citation_recall": (len(exact) + len(visual) + len(represented)) / len(ids) if ids else None,
            }
        )
    total = sum(r["expected_citations"] for r in rows)
    missed = sum(len(r["missing"]) for r in rows)
    return {
        "metric": "reference_citation_coverage",
        "expected_citations": total,
        "citation_recall": (total - missed) / total if total else None,
        "requirements": rows,
        "note": "Source availability only; does not prove retrieval completeness, model reading or factual accuracy. "
        "A page image can cover cited regions without proving they were read.",
        "packets": {
            str(sid): {
                k: p.metrics[k]
                for k in [
                    "text_chars",
                    "image_count",
                    "image_pages",
                    "region_count",
                    "chunk_count",
                    "budget_exceeded",
                ]
            }
            for sid, p in packets.items()
        },
    }
