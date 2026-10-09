"""Versioned contracts. Missing information is data, not an invitation to guess."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Status(StrEnum):
    PRESENT = "present"
    NOT_FOUND = "not_found"
    NOT_APPLICABLE = "not_applicable"
    AMBIGUOUS = "ambiguous"
    CONFLICTING = "conflicting"
    UNREADABLE = "unreadable"


class Target(Contract):
    id: str
    manufacturer: str
    part_number: str
    ordering_code: str | None
    package_name: str
    package_code: str | None
    temperature_grade: str | None
    document_revision: str | None
    evidence_ids: list[str]
    status: Literal["supported", "ambiguous"]
    reason: str


class Catalog(Contract):
    targets: list[Target]
    ambiguities: list[str]


class EvidenceMapItem(Contract):
    requirement_id: str
    region_ids: list[str]
    reason: str


class EvidenceMap(Contract):
    mappings: list[EvidenceMapItem]


class Quantity(Contract):
    minimum: float | None
    typical: float | None
    maximum: float | None
    unit: str | None
    expression: str | None


class Claim(Contract):
    id: str
    requirement_id: str
    parameter: str
    kind: Literal["quantity", "text", "equation", "dimension", "interface", "graph", "note"]
    raw_value: str | None
    value: str | None
    quantity: Quantity | None
    conditions: list[str]
    applies_to: list[str]
    evidence_ids: list[str]
    status: Status
    explanation: str


class Pin(Contract):
    pad: str
    name: str
    electrical_type: Literal[
        "input",
        "output",
        "bidirectional",
        "power_in",
        "power_out",
        "ground",
        "passive",
        "open_drain",
        "nc",
        "unknown",
    ]
    functions: list[str]
    conditions: list[str]
    connection_requirements: list[str]
    evidence_ids: list[str]


class Component(Contract):
    ref: str
    ref_origin: Literal["source", "generated"]
    kind: str
    value: str | None
    terminals: list[str]
    evidence_ids: list[str]


class Net(Contract):
    name: str
    nodes: list[str]  # component_ref.terminal, e.g. U1.16, C1.1
    evidence_ids: list[str]
    status: Literal["supported", "ambiguous"]


class Circuit(Contract):
    id: str
    figure: str
    conditions: list[str]
    components: list[Component]
    nets: list[Net]
    ic_ref: str
    explicitly_unconnected_pads: list[str]
    unobserved_pads: list[str]
    evidence_ids: list[str]
    ambiguities: list[str]


class Finding(Contract):
    requirement_id: str
    status: Status
    evidence_ids: list[str]
    reason: str


class SectionResult(Contract):
    section_id: int = Field(ge=1, le=17)
    status: Status
    findings: list[Finding]
    claims: list[Claim]
    pins: list[Pin]
    circuits: list[Circuit]
    notes: list[str]


class ReviewIssue(Contract):
    category: Literal[
        "incorrect",
        "missing_condition",
        "wrong_target",
        "missing_fact",
        "conflicting_sources",
        "insufficient_evidence",
    ]
    claim_id: str | None
    requirement_id: str
    severity: Literal["critical", "error", "warning"]
    evidence_ids: list[str]
    explanation: str


class Review(Contract):
    supported_claim_ids: list[str]
    issues: list[ReviewIssue]
    reviewed_region_ids: list[str]


class SmokeResult(Contract):
    text_code: str
    image_code: str
    square_color: str


class ExtractionResult(Contract):
    schema_version: Literal["1.0"] = "1.0"
    document_id: str
    parse_id: str
    target: Target
    sections: list[SectionResult]
    run_id: str
    status: Literal["reviewed", "needs_review"]
    issues: list[dict]
    evidence: list[dict]
    metadata: dict

    @model_validator(mode="after")
    def all_sections(self):
        if sorted(s.section_id for s in self.sections) != list(range(1, 18)):
            raise ValueError("Exactly one result for each of the 17 sections is required")
        return self
