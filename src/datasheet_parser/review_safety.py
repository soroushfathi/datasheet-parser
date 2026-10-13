"""Keep target identity separate from evidence and preserve negative-reader scope."""

from typing import Literal

from pydantic import Field, create_model

from .models import Review, ReviewIssue


def review_contract(claims, requirement_ids):
    """Constrain generation to original claims, never findings or blind-reader IDs."""
    ids = tuple(sorted({c["id"] for c in claims}))
    claim_type = Literal[ids] if ids else str
    issue = create_model("ScopedReviewIssue", __base__=ReviewIssue,
        claim_id=(claim_type | None if ids else type(None), ...),
        requirement_id=(Literal[tuple(sorted(requirement_ids))], ...))
    return create_model("ScopedReview", __base__=Review,
        supported_claim_ids=(list[claim_type], Field(..., max_length=0) if not ids else ...),
        issues=(list[issue], ...))


def target_identity(target):
    return {k: v for k, v in target.items() if k not in {"evidence_ids", "reason"}}


def review_source_regions(record, blind, cited):
    required = set(cited)
    for reader in (record, blind):
        section = reader["section"]
        evidenced = {c["requirement_id"] for c in section["claims"] if c["evidence_ids"]}
        if any(f["status"] in {"not_found", "unreadable", "ambiguous"}
               and f["requirement_id"] not in evidenced for f in section["findings"]):
            required.update(reader["supplied_region_ids"])
    return required
