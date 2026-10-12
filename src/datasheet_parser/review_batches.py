"""Lossless requirement-level review partitions; frozen source is repeated, never expanded."""

import json
from copy import deepcopy

from .errors import ReviewBudgetError
from .models import Review


def scoped_view(view, scope):
    """Keep each requirement's two answers whole, including global pin/circuit context."""
    result = deepcopy(view)
    for key in ("claims", "findings"):
        if key in result:
            result[key] = [row for row in result[key] if row["requirement_id"] in scope]
    return result


def plan_review_batches(original, independent, requirement_ids, prepare, limit, section_id):
    """Preflight the entire plan before inference; singleton requirements are indivisible.

    prepare must repeat the same complete source packet for every partition. Splitting
    requirements, rather than individual claim rows, preserves truthful omission checks.
    """
    if not requirement_ids or len(set(requirement_ids)) != len(requirement_ids):
        raise ValueError("Review batch requirements must be nonempty and unique")
    known = set(requirement_ids)
    for view in (original, independent):
        for key in ("claims", "findings"):
            if any(row["requirement_id"] not in known for row in view.get(key, [])):
                raise ValueError("Review draft contains an unknown requirement")
    ids = [claim["id"] for claim in original.get("claims", [])]
    if len(set(ids)) != len(ids):
        raise ValueError("Review draft has duplicate original claim IDs")

    def partition(scope):
        original_part, independent_part = scoped_view(original, scope), scoped_view(independent, scope)
        request = prepare(original_part, independent_part, scope)
        if request["input_tokens_estimate"] <= limit:
            return [{**request, "scope": list(scope),
                     "original_claim_ids": [c["id"] for c in original_part.get("claims", [])]}]
        if len(scope) == 1:
            raise ReviewBudgetError(section_id, request["input_tokens_estimate"], limit,
                                   reason="indivisible_requirement")
        middle = len(scope) // 2
        return partition(scope[:middle]) + partition(scope[middle:])

    return partition(list(requirement_ids))


def merge_review_batches(reviews):
    supported, inspected, issues, seen = set(), set(), [], set()
    for review in reviews:
        supported.update(review.supported_claim_ids)
        inspected.update(review.reviewed_region_ids)
        for issue in review.issues:
            data = issue.model_dump(mode="json")
            data["evidence_ids"] = sorted(set(data["evidence_ids"]))
            key = json.dumps(data, sort_keys=True, ensure_ascii=False)
            if key not in seen:
                seen.add(key)
                issues.append(issue.model_copy(update={"evidence_ids": data["evidence_ids"]}))
    # A negative finding vetoes positive support, even if another part agrees.
    supported.difference_update(issue.claim_id for issue in issues if issue.claim_id is not None)
    return Review(supported_claim_ids=sorted(supported), issues=issues,
                  reviewed_region_ids=sorted(inspected))
