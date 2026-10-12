"""Deterministic structural checks do not certify source interpretation."""

import re
from itertools import pairwise

from .models import SectionResult
from .requirements import requirements

_NC_STUB_MARKER = re.compile(r"(?<![a-z0-9])(?:nc|no[\s_-]*connect|unconnected|open)(?![a-z0-9])", re.IGNORECASE)
_NC_POWER_LABEL = re.compile(
    r"(?<![a-z0-9])(?:[a-z]*gnd[a-z0-9]*|ground|vcc[a-z0-9]*|vdd[a-z0-9]*|vss[a-z0-9]*|vbus|vin|power|supply)(?![a-z0-9])",
    re.IGNORECASE,
)
_OPEN_OUTPUT = re.compile(r"(?<![a-z0-9])open[\s_-]*(?:drain|collector)(?![a-z0-9])", re.IGNORECASE)


def _explicit_nc_stub(nets, node):
    """A cited singleton NC marker is a disposition, not a physical connection.

    A power label, open-drain output, additional terminal or second net can never
    establish this exception. The original net object remains unchanged for audit.
    """
    memberships = [net for net in nets if node in net.nodes]
    if len(memberships) != 1:
        return False
    net = memberships[0]
    return bool(
        set(net.nodes) == {node} and net.status == "supported" and net.evidence_ids
        and _NC_STUB_MARKER.search(net.name) and not _NC_POWER_LABEL.search(net.name)
        and not _OPEN_OUTPUT.search(net.name)
    )


def validate_section(
    section: SectionResult, target_id: str, region_ids: set[str], allowed_evidence: set[str] | None = None,
    *, coverage_uncertainty: dict | None = None, allow_explicit_nc_stub: bool = False,
) -> list[dict]:
    issues = []

    def issue(code, message, claim_id=None, severity="error"):
        issues.append(
            {
                "code": code,
                "message": message,
                "claim_id": claim_id,
                "severity": severity,
                "section_id": section.section_id,
            }
        )

    def evidence(ids, owner):
        if not ids:
            issue("missing_evidence", f"{owner} has no evidence", owner)
        for rid in ids:
            if rid not in region_ids:
                issue("invalid_evidence", f"{owner} references unknown region {rid}", owner)
            elif allowed_evidence is not None and rid not in allowed_evidence:
                issue("unseen_evidence", f"{owner} cites a region not supplied in this extraction", owner)

    expected = set(requirements(section.section_id))
    finding_ids = [f.requirement_id for f in section.findings]
    if len(finding_ids) != len(set(finding_ids)) or set(finding_ids) != expected:
        issue("requirement_coverage", "Findings must cover each section requirement exactly once")
    for finding in section.findings:
        uncertainty = (coverage_uncertainty or {}).get(finding.requirement_id, {})
        derived_absence_uncertainty = (
            finding.status == "ambiguous" and uncertainty.get("reported_status") == "not_found"
            and uncertainty.get("effective_status") == "ambiguous"
        )
        if finding.status != "not_found" and not derived_absence_uncertainty:
            evidence(finding.evidence_ids, finding.requirement_id)
        if finding.status == "not_found" and not finding.reason:
            issue("absence_without_reason", finding.requirement_id)
        if finding.status in {"ambiguous", "conflicting", "unreadable"}:
            issue("unresolved_requirement", finding.reason, finding.requirement_id, "warning")
        if finding.status in {"not_found", "not_applicable"} and any(
            c.requirement_id == finding.requirement_id and c.status == "present" for c in section.claims
        ):
            issue("contradictory_disposition", finding.requirement_id)
    if section.status in {"not_found", "not_applicable"} and (
        section.pins or section.circuits or any(c.status == "present" for c in section.claims)
    ):
        issue("contradictory_section_status", "Absent section contains extracted data")
    ids = [c.id for c in section.claims]
    if len(ids) != len(set(ids)):
        issue("duplicate_claim", "Duplicate claim IDs")
    for claim in section.claims:
        if claim.requirement_id not in expected:
            issue("wrong_requirement", claim.requirement_id, claim.id)
        if claim.applies_to != [target_id]:
            issue("wrong_target", "Claims must explicitly apply only to the selected target", claim.id)
        if claim.status != "not_found" or claim.evidence_ids:
            evidence(claim.evidence_ids, claim.id)
        if claim.status in {"not_found", "not_applicable"} and not claim.explanation:
            issue("absence_without_reason", "Absent claim requires an explanation", claim.id)
        q = claim.quantity
        if claim.status == "present" and not q and not (claim.value or claim.raw_value):
            issue("empty_present_claim", "Present claim has no value", claim.id)
        # Absent/unreadable facts do not have a numeric value to structure. Their
        # disposition and explanation are checked above; requiring a quantity
        # here would report the expected absence as an independent data error.
        if claim.kind == "quantity" and claim.status == "present" and q is None:
            issue("missing_quantity", "Quantity claim requires a structured quantity", claim.id)
        if q:
            values = [v for v in [q.minimum, q.typical, q.maximum] if v is not None]
            if not values and not q.expression:
                issue("empty_quantity", "Quantity has neither bounds nor symbolic expression", claim.id)
            if any(a > b for a, b in pairwise(values)):
                issue("inverted_bounds", "Minimum/typical/maximum are not ordered", claim.id)
            if values and not q.unit:
                issue("missing_unit", "Numeric quantity lacks an explicit unit", claim.id)
        if claim.status in {"ambiguous", "conflicting", "unreadable"}:
            issue("unresolved_claim", claim.explanation, claim.id, "warning")
    pads = [p.pad for p in section.pins]
    if len(pads) != len(set(pads)):
        issue("duplicate_pad", "Physical pads must be unique; combine alternate functions")
    if section.pins and section.section_id != 1:
        issue("misplaced_pins", "Pin objects belong to section 1")
    if section.circuits and section.section_id != 2:
        issue("misplaced_circuit", "Circuit objects belong to section 2")
    for pin in section.pins:
        evidence(pin.evidence_ids, f"pad:{pin.pad}")
        if pin.electrical_type == "unknown":
            issue("unresolved_pin_type", pin.pad, severity="warning")
    for circuit in section.circuits:
        if circuit.ambiguities:
            issue("unresolved_circuit", "; ".join(circuit.ambiguities), circuit.id, "warning")
        refs = [c.ref for c in circuit.components]
        if len(refs) != len(set(refs)):
            issue("duplicate_component", circuit.id)
        terminals = {f"{c.ref}.{pin}" for c in circuit.components for pin in c.terminals}
        if circuit.ic_ref not in refs:
            issue("missing_ic", circuit.id)
        memberships = {}
        net_names = [n.name for n in circuit.nets]
        if len(net_names) != len(set(net_names)):
            issue("duplicate_net", circuit.id)
        for component in circuit.components:
            evidence(component.evidence_ids, component.ref)
            if len(component.terminals) != len(set(component.terminals)):
                issue("duplicate_component_terminal", component.ref)
        for net in circuit.nets:
            evidence(net.evidence_ids, net.name)
            if net.status == "ambiguous":
                issue("ambiguous_connection", net.name, severity="warning")
            for node in net.nodes:
                if node not in terminals:
                    issue("unknown_terminal", node)
                if node in memberships and memberships[node] != net.name:
                    issue("terminal_in_two_nets", node)
                memberships[node] = net.name
        for pad in circuit.explicitly_unconnected_pads:
            node = f"{circuit.ic_ref}.{pad}"
            if node in memberships and not (allow_explicit_nc_stub and _explicit_nc_stub(circuit.nets, node)):
                issue("nc_connected", pad)
        for pad in circuit.unobserved_pads:
            if f"{circuit.ic_ref}.{pad}" in memberships:
                issue("unobserved_connected", pad)
        if set(circuit.explicitly_unconnected_pads) & set(circuit.unobserved_pads):
            issue("nc_unobserved_overlap", circuit.id)
        evidence(circuit.evidence_ids, circuit.id)
    return issues


def validate_cross_sections(sections: list[SectionResult]) -> list[dict]:
    by_id = {s.section_id: s for s in sections}
    issues = []
    if 1 in by_id and 2 in by_id:
        pads = {p.pad for p in by_id[1].pins}
        for circuit in by_id[2].circuits:
            observed = {
                node.removeprefix(circuit.ic_ref + ".")
                for net in circuit.nets
                for node in net.nodes
                if node.startswith(circuit.ic_ref + ".")
            }
            accounted = observed | set(circuit.explicitly_unconnected_pads) | set(circuit.unobserved_pads)
            if pads - accounted:
                issues.append(
                    {
                        "code": "unaccounted_ic_pads",
                        "severity": "warning",
                        "message": f"{circuit.id}: pads lack observed, NC or unobserved disposition: {sorted(pads - accounted)}",
                    }
                )
            if accounted - pads:
                issues.append(
                    {
                        "code": "unknown_pad_disposition",
                        "severity": "critical",
                        "message": f"{circuit.id}: dispositions reference unknown pads: {sorted(accounted - pads)}",
                    }
                )
            for comp in circuit.components:
                if comp.ref == circuit.ic_ref:
                    unknown = set(comp.terminals) - pads
                    if unknown:
                        issues.append(
                            {
                                "code": "package_pin_mismatch",
                                "severity": "critical",
                                "message": f"{circuit.id}: unknown IC pads {sorted(unknown)}",
                            }
                        )
    return issues
