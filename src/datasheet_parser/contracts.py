"""Small model-facing contracts, expanded deterministically into the stable public JSON."""

from pydantic import create_model

from .models import Circuit, Claim, Contract, Finding, Pin, SectionResult, Status

# IDs and the selected target are deterministic, not model-generated output.
CompactClaim = create_model(
    "CompactClaim",
    __base__=Contract,
    **{k: (f.annotation, ...) for k, f in Claim.model_fields.items() if k not in {"id", "applies_to"}},
)


def section_contract(sid):
    fields = {
        "status": (Status, ...),
        "findings": (list[Finding], ...),
        "claims": (list[CompactClaim], ...),
        "search_requests": (list[str], ...),
    }
    if sid == 1:
        fields["pins"] = (list[Pin], ...)
    if sid == 2:
        fields["circuits"] = (list[Circuit], ...)
    return create_model(f"Section{sid}Output", __base__=Contract, **fields)


def expand_section(output, sid, target_id):
    data = dict(output)
    data.pop("search_requests", None)
    claims = [{**c, "id": f"{sid}.{i + 1}", "applies_to": [target_id]} for i, c in enumerate(data["claims"])]
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
