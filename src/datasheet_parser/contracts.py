"""Versioned model-facing contracts with a stable, source-faithful public JSON."""

from typing import Literal

from pydantic import create_model, model_validator

from .models import Circuit, Claim, Contract, Finding, Pin, SectionResult, Status

# IDs and the selected target are deterministic, not model-generated output.
CompactClaim = create_model(
    "CompactClaim",
    __base__=Contract,
    **{k: (f.annotation, ...) for k, f in Claim.model_fields.items() if k not in {"id", "applies_to"}},
)


class NumericBound(Contract):
    """A number belongs to a bound only when that column is explicit in the source."""

    role: Literal["minimum", "typical", "maximum"]
    value: float


class CompactQuantityV2(Contract):
    bounds: list[NumericBound]
    unit: str | None
    expression: str | None

    @model_validator(mode="after")
    def unique_bounds(self):
        roles = [bound.role for bound in self.bounds]
        if len(roles) != len(set(roles)):
            raise ValueError("Each numeric bound may occur only once per claim")
        return self


class NumericClaimV2(Contract):
    requirement_id: str
    parameter: str
    kind: Literal["quantity", "dimension"]
    raw_value: str | None
    quantity: CompactQuantityV2 | None
    conditions: list[str]
    evidence_ids: list[str]
    status: Status
    reason: str


class PlainClaimV2(Contract):
    requirement_id: str
    parameter: str
    kind: Literal["text", "equation", "interface", "graph", "note"]
    value: str | None
    conditions: list[str]
    evidence_ids: list[str]
    status: Status
    reason: str


def _version(version):
    if version in {"exact-v1", "compact-v1"}:
        return "exact-v1"
    if version == "compact-v2":
        return version
    raise ValueError(f"Unknown model contract version: {version}")


def section_contract(sid, version="exact-v1"):
    version = _version(version)
    fields = {
        "status": (Status, ...),
        "findings": (list[Finding], ...),
        "claims": (
            list[NumericClaimV2 | PlainClaimV2] if version == "compact-v2" else list[CompactClaim],
            ...,
        ),
        "search_requests": (list[str], ...),
    }
    if sid == 1:
        fields["pins"] = (list[Pin], ...)
    if sid == 2:
        fields["circuits"] = (list[Circuit], ...)
    suffix = "V2" if version == "compact-v2" else ""
    return create_model(f"Section{sid}Output{suffix}", __base__=Contract, **fields)


def _quantity_display(quantity, raw_value=None):
    if quantity:
        pieces = []
        for key, label in [("minimum", "min"), ("typical", "typ"), ("maximum", "max")]:
            number = quantity.get(key)
            if number is not None:
                unit = f" {quantity['unit']}" if quantity.get("unit") else ""
                pieces.append(f"{label}={number:g}{unit}")
        if quantity.get("expression"):
            pieces.append(quantity["expression"])
        if pieces:
            return "; ".join(pieces)
    return raw_value


def _expand_claim_v2(claim):
    data = dict(claim)
    explanation = data.pop("reason")
    if data["kind"] in {"quantity", "dimension"}:
        compact = data.get("quantity")
        if compact is not None:
            bounds = {row["role"]: row["value"] for row in compact["bounds"]}
            if len(bounds) != len(compact["bounds"]):
                raise ValueError("Duplicate numeric bound")
            data["quantity"] = {
                **{key: bounds.get(key) for key in ["minimum", "typical", "maximum"]},
                "unit": compact["unit"],
                "expression": compact["expression"],
            }
        data["value"] = _quantity_display(data["quantity"], data["raw_value"])
    else:
        data["raw_value"] = data["value"]
        data["quantity"] = None
    return {**data, "explanation": explanation}


def expand_section(output, sid, target_id, version="exact-v1"):
    version = _version(version)
    data = dict(output)
    data.pop("search_requests", None)
    claims = [
        {
            **(_expand_claim_v2(c) if version == "compact-v2" else c),
            "id": f"{sid}.{i + 1}",
            "applies_to": [target_id],
        }
        for i, c in enumerate(data["claims"])
    ]
    return SectionResult.model_validate(
        {
            **data,
            "claims": claims,
            "section_id": sid,
            "pins": data.get("pins", []),
            "circuits": data.get("circuits", []),
            "notes": [],
        }
    )


def _without_empty(value):
    """Remove only empty structure; retain zero, false, and source text verbatim."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            item = _without_empty(item)
            if item is None or item == "" or isinstance(item, (dict, list)) and not item:
                continue
            result[key] = item
        return result
    if isinstance(value, list):
        return [_without_empty(item) for item in value]
    return value


def compact_review(section, target_id=None):
    """A lossless semantic view for review; IDs still refer to original public claims.

    Repeated selected-target lists may be omitted when the caller supplies that target;
    a different applicability is retained so the reviewer can detect wrong-target facts.
    Nonempty legacy explanations stay because they can contain otherwise unrecorded facts.
    """
    data = section.model_dump(mode="json") if isinstance(section, SectionResult) else section
    claims = []
    for original in data["claims"]:
        claim = {key: value for key, value in original.items() if key != "explanation"}
        if target_id is not None and claim.get("applies_to") == [target_id]:
            claim.pop("applies_to", None)
        raw, value = claim.get("raw_value"), claim.get("value")
        if claim.get("quantity") is not None:
            if value == raw or value == _quantity_display(claim["quantity"], raw):
                claim.pop("value", None)
        elif raw == value:
            claim.pop("raw_value", None)
        if original.get("explanation"):
            claim["reason"] = original["explanation"]
        claims.append(claim)
    return _without_empty({**data, "claims": claims})


def columnar_review(view):
    """Losslessly remove repeated claim field names from an already compact view.

    Group by the actual nonempty field set; do not pad every row with nulls or
    shorten facts, numbers, qualifiers, citations or public claim identifiers.
    """
    groups = {}
    for claim in view.get("claims", []):
        columns = tuple(claim)
        groups.setdefault(columns, []).append([claim[key] for key in columns])
    return {
        **{key: value for key, value in view.items() if key != "claims"},
        "claim_tables": [{"columns": list(columns), "rows": rows} for columns, rows in groups.items()],
    }


def shared_review_texts(views):
    """Intern repeated long draft text only when exact token estimates predict savings."""
    import json
    from collections import Counter

    from .token_budget import estimate_text_tokens

    counts = Counter()

    def collect(value):
        if isinstance(value, str) and len(value) >= 80:
            counts[value] += 1
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(views)
    pool, reverse = {}, {}
    for value, count in counts.items():
        key = f"t{len(pool)}"
        tokens = lambda item: estimate_text_tokens(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
        if count >= 2 and count * tokens(value) > tokens({key: value}) + count * tokens({"text_ref": key}) + 10:
            pool[key], reverse[value] = value, key

    def encode(value):
        if isinstance(value, str) and value in reverse:
            return {"text_ref": reverse[value]}
        if isinstance(value, dict):
            return {key: encode(item) for key, item in value.items()}
        if isinstance(value, list):
            return [encode(item) for item in value]
        return value

    return pool, encode(views)


def cited_regions(section):
    """Collect citations from findings, claims, pins, circuits, components and nets."""
    data = section.model_dump(mode="json") if isinstance(section, SectionResult) else section
    citations = set()

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "evidence_ids":
                    citations.update(item)
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(data)
    return citations


def output_budget(sid, role, rows=None):
    """A generation target, never a truncation rule; complexity keeps detailed rows."""
    extraction = {
        1: 4500,
        2: 9000,
        3: 5000,
        4: 3500,
        5: 3500,
        6: 4000,
        7: 6000,
        8: 3000,
        9: 6500,
        10: 3500,
        11: 3500,
        12: 5000,
        13: 3000,
        14: 4500,
        15: 4000,
        16: 4500,
        17: 3500,
    }
    if sid not in extraction:
        raise ValueError("Section ID must be between 1 and 17")
    if role == "review":
        budget = 4000 if sid in {2, 7, 9, 12} else 2800 if sid in {1, 3, 14, 15} else 1800
        return max(budget, min(4000, 1200 + (rows or 0) * 35))
    if role not in {"extract", "blind"}:
        raise ValueError(f"Unknown section role: {role}")
    return max(extraction[sid], min(9000, 1800 + (rows or 0) * 100))
