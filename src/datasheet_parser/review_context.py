"""Exact original-draft context and conservative review allegation reconciliation.

These helpers check what the original answer already contains, not whether it is
correct. Source correctness remains the reviewer's job. In particular an alleged
absence is never dismissed because its number occurs somewhere in a long claim.
"""

import re
import unicodedata
from copy import deepcopy

from .contracts import columnar_review, compact_review
from .models import Review, ReviewIssue, SectionResult

INVENTORY_VERSION = "original-inventory-v1"
PRESENCE_VERSION = "original-presence-v1"
RECONCILIATION_VERSION = "exact-existing-fact-v1"

INVENTORY_INSTRUCTIONS = (
    "WHOLE_ORIGINAL_INVENTORY contains the exact original claims outside this part's "
    "requirement scope, in columnar form (pair each row with its columns). It is "
    "original-answer context, not independent source evidence or positive support. "
    "Before alleging missing_fact, check this inventory as well as ORIGINAL: a fact "
    "recorded under another requirement is not globally absent. Report a real "
    "requirement placement/coverage problem explicitly instead of saying it is absent. "
    "Do not output sibling claim IDs in supported_claim_ids or issue.claim_id. "
    "For a single precise missing_fact or missing_condition allegation you may use "
    "the complete explanation 'Missing fact: <full exact fact>' or 'Missing condition: "
    "<full exact condition>'. Never shorten a qualifier or combine several facts into "
    "that form. Other errors and multi-fact allegations require normal explanatory text."
)

PRESENCE_INSTRUCTIONS = (
    "ORIGINAL_PRESENCE_INDEX lists original claims outside this part's requirement "
    "scope with their exact IDs, requirement, parameter, status and values/quantities. "
    "It is an index for checking that a fact is recorded somewhere in the original "
    "section, not source evidence or a complete claim: conditions and explanations "
    "are deliberately not repeated here. Never certify support, correct qualifiers, "
    "source applicability or conditions from this index. Before alleging missing_fact, "
    "check this index as well as ORIGINAL. A fact recorded under another requirement "
    "is not globally absent; state an actual coverage/placement problem explicitly. "
    "Do not output sibling IDs in supported_claim_ids or issue.claim_id. For one precise "
    "absence allegation, the complete explanation may be 'Missing fact: <full exact "
    "fact>' or 'Missing condition: <full exact condition>'; do not bundle facts, shorten "
    "qualifiers or claim that conditions are absent because this index omits them."
)


def _view(original):
    return compact_review(original) if isinstance(original, SectionResult) else deepcopy(original)


def original_inventory(original, scope=None):
    """Encode every out-of-scope original claim exactly; no summaries or truncation.

    A scoped review already receives all local claims/findings and complete global
    pins/circuits. This additional inventory supplies the otherwise invisible sibling
    claims. The caller must include its exact serialized cost in preflight and must
    not trim it to fit a budget. With no scope, all original claims are included.
    """
    view = _view(original)
    local = set(scope or ())
    claims = [claim for claim in view.get("claims", []) if claim["requirement_id"] not in local]
    return {
        "version": INVENTORY_VERSION,
        "claim_count": len(claims),
        **columnar_review({"claims": claims}),
    }


def presence_inventory(original, scope=None):
    """Exact sibling fact index for absence routing, not a lossless review answer.

    It keeps the selected presence fields verbatim, including signs, units, and
    quantity bound roles. Local original claims remain complete in ORIGINAL. This
    smaller index does not replace the original claim or prove that its qualifiers
    are correct; reconciliation always uses the full original, not this index.
    """
    view = _view(original)
    local = set(scope or ())
    keys = ("id", "requirement_id", "parameter", "status", "value", "raw_value", "quantity")
    claims = [
        {key: claim[key] for key in keys if key in claim}
        for claim in view.get("claims", []) if claim["requirement_id"] not in local
    ]
    return {"version": PRESENCE_VERSION, "claim_count": len(claims), **columnar_review({"claims": claims})}


def _normalize_exact(text):
    # NFC handles equivalent Unicode character compositions. Do not use compatibility
    # normalization: scientific superscripts/subscripts and unit spellings can matter.
    # Whitespace is normalized, but case, units, signs, bounds and every word remain.
    return " ".join(unicodedata.normalize("NFC", text).split())


def _absence_literal(issue):
    """Accept only the entire, single explicit allegation, not quotes in prose."""
    prefix = {"missing_fact": "Missing fact:", "missing_condition": "Missing condition:"}.get(issue.category)
    if prefix is None or not issue.explanation.startswith(prefix):
        return None
    literal = issue.explanation[len(prefix):].strip()
    if not literal:
        return None
    # Optional matching delimiters are serialization, not part of the fact. Mixed
    # delimiters or additional prose stay literal and therefore cannot falsely match.
    for left, right in (("\"", "\""), ("'", "'"), ("«", "»"), ("“", "”"), ("`", "`")):
        if literal.startswith(left) and literal.endswith(right):
            literal = literal[len(left):-len(right)]
            break
    # Multiple markers constitute bundled allegations, which are not reducible to
    # the exact existence of one field in the original answer.
    if re.search(r"\bMissing (?:fact|condition):", literal):
        return None
    return _normalize_exact(literal) or None


def _fact_fields(claim, category):
    fields = [(f"conditions[{n}]", condition) for n, condition in enumerate(claim.get("conditions", []))]
    if category == "missing_fact":
        for key in ("value", "raw_value"):
            value = claim.get(key)
            if isinstance(value, str) and value:
                fields.append((key, value))
                parameter = claim.get("parameter")
                if parameter:
                    fields.extend(((f"parameter+{key}", f"{parameter}: {value}"),
                                   (f"parameter={key}", f"{parameter} = {value}")))
    return fields


def reconcile_review(review, original):
    """Return an effective review plus a complete, lossless reconciliation audit.

    Resolve only a single explicit absence whose *complete literal* matches an
    existing present claim field, with overlapping citations. Missing conditions
    must match a condition of that same claim. Generic prose, different units,
    signs, qualifiers, statuses, evidence or values remain unresolved. No supported
    claim is created: existing raw positives stay subject to every effective issue.
    """
    if not isinstance(review, Review):
        review = Review.model_validate(review)
    original = _view(original)
    present = [claim for claim in original.get("claims", []) if claim.get("status") == "present"]
    issues, decisions = [], []
    for index, issue in enumerate(review.issues):
        literal = _absence_literal(issue)
        matches = []
        if literal is not None:
            for claim in present:
                if issue.category == "missing_condition" and issue.claim_id != claim["id"]:
                    continue
                if not set(issue.evidence_ids) & set(claim.get("evidence_ids", [])):
                    continue
                for field, value in _fact_fields(claim, issue.category):
                    if _normalize_exact(value) == literal:
                        matches.append({"claim_id": claim["id"], "requirement_id": claim["requirement_id"],
                                        "field": field, "value": value})
        if matches:
            decisions.append({"raw_issue_index": index, "disposition": "already_recorded",
                              "matches": matches, "reason": "Complete absence literal already exists in the original answer."})
        else:
            issues.append(issue)
    negative_ids = {issue.claim_id for issue in issues if issue.claim_id is not None}
    effective = review.model_copy(update={
        "issues": issues,
        "supported_claim_ids": [cid for cid in review.supported_claim_ids if cid not in negative_ids],
    })
    return effective, {
        "version": RECONCILIATION_VERSION,
        "raw_review": review.model_dump(mode="json"),
        "decisions": decisions,
    }


def detailed_review_claim_ids(review):
    """Claim warnings already explained by active review issues, for export grouping.

    Omitting a redundant unverified_claim diagnostic does not certify these claims:
    the detailed issue remains visible and keeps the result in needs_review.
    """
    issues = review.issues if isinstance(review, Review) else [
        ReviewIssue.model_validate(issue) for issue in review["issues"]
    ]
    return {issue.claim_id for issue in issues if issue.claim_id is not None}
