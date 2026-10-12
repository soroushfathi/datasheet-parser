"""Normalize schematic IC labels using the selected package's cited pin inventory.

This is structural reconciliation, not source verification. Unknown aliases remain
unknown. Repeated symbols may be replaced as a complete set on one supported net;
the code never invents an individual GND_A -> pad 1 assignment.
"""

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field

from .models import Pin, SectionResult

NORMALIZATION_VERSION = "physical-pads-2"


@dataclass(frozen=True)
class PinAlias:
    """An explicit, source-reviewed semantic alias; citations alone are not proof.

    Callers must establish that the cited source associates ``label`` with ``pad``.
    The normalizer deliberately has no built-in WAKEUP -> WAKE or VCC -> VDD rule.
    """

    label: str
    pad: str
    evidence_ids: tuple[str, ...]
    reason: str


@dataclass
class NormalizationResult:
    section: SectionResult
    diagnostics: list[dict] = field(default_factory=list)
    provenance: list[dict] = field(default_factory=list)
    version: str = NORMALIZATION_VERSION


def _label(value: str) -> str:
    # Keep meaningful punctuation, including +, /, _, active-low bars and periods.
    # Only equivalent rendering of minus and Unicode width/case is canonicalized.
    return unicodedata.normalize("NFKC", value).translate(str.maketrans("−–‐‑", "----")).strip().casefold()


def _unique(values):
    return list(dict.fromkeys(values))


def _pad_sort(value: str):
    return (0, int(value)) if value.isdecimal() else (1, value)


_PACKAGE_FAMILY = re.compile(r"\b(?:VSSOP|HVSSOP|TSSOP|SSOP|SOIC|PDIP|DIP|TQFP|LQFP|QFP|QFN|DFN|BGA|WLCSP|SON|SOT)\b",
                             re.IGNORECASE)
_PIN_COUNT = re.compile(r"\b(\d+)\s*[-–]?\s*pins?\b", re.IGNORECASE)


def source_pin_inventory(regions: list[dict], target) -> list[dict]:
    """Read one complete numeric pin table only when source package context agrees.

    This optional prompt aid is independent of extracted AI answers. It declines
    partial tables, multiple package contexts, conflicting tables and unnamed pin
    columns. ``source_io`` remains verbatim (e.g. USB is not guessed as an I/O type).
    """
    target = target if isinstance(target, dict) else target.model_dump()
    package = target.get("package_name", "")
    families = {m.upper() for m in _PACKAGE_FAMILY.findall(package)}
    counts = {int(m) for m in _PIN_COUNT.findall(package)}
    part = target.get("part_number", "")
    if len(families) != 1 or len(counts) != 1 or not part:
        return []
    pin_count = next(iter(counts))
    by_page = defaultdict(list)
    for region in regions:
        by_page[region.get("page")].append(region)
    candidates = []
    for region in regions:
        rows = region.get("rows")
        if region.get("kind") != "table" or not rows or not region.get("id"):
            continue
        contexts = [r for r in by_page[region.get("page")]
                    if r.get("kind") in {"page", "line"} and r.get("id")]
        context = "\n".join(r.get("text", "") for r in contexts)
        source_families = {m.upper() for m in _PACKAGE_FAMILY.findall(context)}
        source_counts = {int(m) for m in _PIN_COUNT.findall(context)}
        if (source_families != families or source_counts != counts
                or not re.search(r"(?<!\w)" + re.escape(part) + r"(?!\w)", context, re.IGNORECASE)):
            continue
        headers = []
        for index, row in enumerate(rows[:3]):
            if not isinstance(row, list):
                continue
            normalized = [re.sub(r"[^a-z0-9]", "", str(cell or "").lower()) for cell in row]
            pad_columns = [i for i, value in enumerate(normalized) if value in {"pinno", "pinnumber", "pin"}]
            name_columns = [i for i, value in enumerate(normalized)
                            if value in {"name", "pinname", "signal", "signalname"}]
            if len(pad_columns) == len(name_columns) == 1:
                headers.append((index, pad_columns[0], name_columns[0], normalized))
        if len(headers) != 1:
            continue
        start, pad_column, name_column, header = headers[0]
        io_column = next((i for i, cell in enumerate(header) if cell in {"io", "type", "direction"}), None)
        desc_column = next((i for i, cell in enumerate(header) if cell in {"description", "function"}), None)
        parsed = []
        valid = True
        for row in rows[start + 1:]:
            if not isinstance(row, list) or len(row) <= max(pad_column, name_column):
                valid = False
                break
            if not any(str(cell or "").strip() for cell in row):
                continue
            pad = str(row[pad_column] or "").strip()
            raw_name = str(row[name_column] or "").strip()
            if not pad.isdecimal() or not raw_name or not 1 <= int(pad) <= pin_count:
                valid = False
                break
            parsed.append({
                "pad": str(int(pad)), "name": raw_name.replace("\n", ""), "raw_name": raw_name,
                "source_io": row[io_column] if io_column is not None and io_column < len(row) else None,
                "description": row[desc_column] if desc_column is not None and desc_column < len(row) else None,
                "evidence_ids": _unique([region["id"], *(r["id"] for r in contexts if r["kind"] == "page")]),
            })
        if valid and len(parsed) == pin_count and {p["pad"] for p in parsed} == {
            str(i) for i in range(1, pin_count + 1)
        }:
            candidates.append(sorted(parsed, key=lambda row: _pad_sort(row["pad"])))
    # Two distinct tables may correspond to revisions or variants; do not pick one.
    return candidates[0] if len(candidates) == 1 else []


def normalize_circuit_pads(
    circuit_section: SectionResult,
    pin_section: SectionResult,
    *,
    aliases: tuple[PinAlias, ...] = (),
    source_inventory: list[dict] | None = None,
    source_evidence_allowlist: set[str] | None = None,
) -> NormalizationResult:
    """Return a deep copy plus diagnostics and exact transformation provenance.

    A nonempty ``source_inventory`` must come from ``source_pin_inventory`` and
    supersedes AI pin-name mappings without modifying section 1. Its table must
    have been supplied to section 1 or the figure extraction; the caller passes
    exactly those source IDs in ``source_evidence_allowlist``. Global availability
    alone is insufficient. Undetected raw tables use an explicitly uncertified
    extracted inventory fallback, never an implicit source-truth assertion.

    Pin evidence must be nonempty. A complete inventory is copied into the IC's
    terminals, while missing circuit observations become ``unobserved_pads``;
    package NC names never imply an observed schematic no-connect marker.
    """
    if circuit_section.section_id != 2 or pin_section.section_id != 1:
        raise ValueError("Circuit section 2 and physical pin section 1 are required")
    result = NormalizationResult(circuit_section.model_copy(deep=True))
    inventory = pin_section.pins
    basis = "extracted_section1_not_source_certified"
    if source_inventory:
        inventory = _source_inventory(source_inventory, source_evidence_allowlist)
        if inventory is None:
            result.diagnostics.append({
                "code": "unusable_source_pin_inventory", "severity": "error", "section_id": 2,
                "message": "Raw pin inventory must be complete, numeric, cited and explicitly authorized by supplied pin-table evidence; no normalization applied",
            })
            return result
        basis = "deterministic_selected_source_pin_table"
        raw_by_pad = {pin.pad: pin for pin in inventory}
        wrong_names = [{"pad": pin.pad, "extracted_name": pin.name, "source_name": raw_by_pad[pin.pad].name}
                       for pin in pin_section.pins if pin.pad in raw_by_pad
                       and _label(pin.name) != _label(raw_by_pad[pin.pad].name)]
        extracted_pads = [pin.pad for pin in pin_section.pins]
        missing = sorted(set(raw_by_pad) - set(extracted_pads), key=_pad_sort)
        unexpected = sorted(set(extracted_pads) - set(raw_by_pad), key=_pad_sort)
        duplicate = len(extracted_pads) != len(set(extracted_pads))
        if wrong_names or missing or unexpected or duplicate:
            result.diagnostics.append({
                "code": "pin_inventory_source_conflict", "severity": "error", "section_id": 1,
                "message": "Extracted pin inventory differs from the selected raw pin table; original section 1 remains unchanged",
                "name_disagreements": wrong_names, "missing_source_pads": missing,
                "unexpected_extracted_pads": unexpected, "duplicate_extracted_pads": duplicate,
                "evidence_ids": _unique([e for pin in inventory for e in pin.evidence_ids]),
            })
    pads = [pin.pad for pin in inventory]
    if not pads or len(pads) != len(set(pads)):
        result.diagnostics.append({
            "code": "unusable_pin_inventory", "severity": "error", "section_id": 2,
            "message": "Physical pin inventory is empty or contains duplicate pads; no normalization applied",
        })
        return result
    cited = {pin.pad: pin for pin in inventory if pin.evidence_ids}
    uncited = sorted(set(pads) - set(cited), key=_pad_sort)
    if uncited:
        result.diagnostics.append({
            "code": "uncited_pin_inventory", "severity": "error", "section_id": 2,
            "message": f"Physical pads without source evidence cannot establish circuit mappings: {uncited!r}",
        })
    by_name: dict[str, list[Pin]] = defaultdict(list)
    for pin in cited.values():
        by_name[_label(pin.name)].append(pin)
    alias_index: dict[str, list[PinAlias]] = defaultdict(list)
    for alias in aliases:
        if alias.pad not in cited or not alias.evidence_ids or not alias.reason.strip():
            raise ValueError("Semantic aliases require a cited physical pad, source evidence and rationale")
        alias_index[_label(alias.label)].append(alias)
    for circuit in result.section.circuits:
        _normalize(circuit, cited, by_name, alias_index, result)
    result.provenance.append({
        "strategy": "pin_inventory_basis", "basis": basis,
        "evidence_ids": _unique([e for pin in cited.values() for e in pin.evidence_ids]),
        "source_certified": False,
        "reason": "Raw table parsing grounds pad/name reconciliation only; schematic wiring and remaining section 1 content are not certified"
        if source_inventory else "No authorized unambiguous raw table was supplied; cited AI pin inventory is a structural fallback, not verified source truth",
    })
    for record in [*result.provenance, *result.diagnostics]:
        record["normalization_version"] = NORMALIZATION_VERSION
    return result


def _explicit_pad_name(label, cited):
    """Return an explicit numeric-pad assertion only if its literal name agrees.

    Figure comments and alias annotations are retained in transformation
    provenance, not used to infer aliases or schematic connectivity.
    """
    match = re.fullmatch(r"(?:pad\s*)?(\d+)\s*\((.*)\)", label, re.IGNORECASE)
    if match is None:
        match = re.fullmatch(r"(?:pad\s*)?(\d+)\s*:\s*(.+)", label, re.IGNORECASE)
    if match is None:
        match = re.fullmatch(r"(?:pad\s*)?(\d+)\s+(.+)", label, re.IGNORECASE)
    if match is None:
        return None
    pad, name = match.groups()
    if pad not in cited:
        return {"pad": pad, "agrees": False}
    source_name = cited[pad].name
    if _label(name) == _label(source_name):
        return {"pad": pad, "agrees": True}
    # Only an exact literal name followed by an unmistakable annotation can be
    # separated. Ordinary trailing prose (e.g. 'SUSPEND open schematic stub') is
    # not an accepted pin-name syntax.
    normalized = _label(name)
    pin_name = _label(source_name)
    annotation = r"\s*(?:[;/]\s*(?:schematic|figure)\s+alias\b.*|\(\s*figure\s+\d+\b.*\))"
    if re.fullmatch(re.escape(pin_name) + annotation, normalized):
        return {"pad": pad, "agrees": True}
    return {"pad": pad, "agrees": False}


def _source_inventory(rows, allowlist):
    if allowlist is None or not isinstance(allowlist, set):
        return None
    pads, inventory = [], []
    for row in rows:
        if not isinstance(row, dict):
            return None
        pad, name, evidence = row.get("pad"), row.get("name"), row.get("evidence_ids")
        if (not isinstance(pad, str) or not pad.isdecimal() or str(int(pad)) != pad
                or not isinstance(name, str) or not name.strip()
                or not isinstance(evidence, list) or not evidence or not all(isinstance(e, str) for e in evidence)):
            return None
        proofs = [rid for rid in evidence if rid in allowlist]
        if not any(re.search(r":t\d+$", rid) for rid in proofs):
            return None
        pads.append(pad)
        inventory.append(Pin(pad=pad, name=name, electrical_type="unknown", functions=[], conditions=[],
                             connection_requirements=[], evidence_ids=proofs))
    if len(pads) != len(set(pads)) or set(pads) != {str(i) for i in range(1, len(rows) + 1)}:
        return None
    # Every row must originate in the one unambiguous complete table returned by
    # source_pin_inventory, not an assembly of rows from different package tables.
    table_proofs = [{rid for rid in pin.evidence_ids if re.search(r":t\d+$", rid)} for pin in inventory]
    if not table_proofs or len(set.intersection(*table_proofs)) != 1:
        return None
    return inventory


def _normalize(circuit, cited, by_name, alias_index, result):
    def diagnostic(code, message, severity="warning", evidence_ids=()):
        result.diagnostics.append({
            "code": code, "severity": severity, "section_id": 2,
            "circuit_id": circuit.id, "message": message, "evidence_ids": list(evidence_ids),
        })
        circuit.ambiguities = _unique([*circuit.ambiguities, message])

    ic_components = [component for component in circuit.components if component.ref == circuit.ic_ref]
    if len(ic_components) != 1:
        diagnostic("unusable_ic_component", "Circuit requires exactly one IC component", "error")
        return
    ic = ic_components[0]
    if not ic.evidence_ids or not circuit.evidence_ids:
        diagnostic("uncited_ic_component", "Cannot reconcile IC pads without schematic evidence", "error")
        return
    prefix = circuit.ic_ref + "."

    def terminal(value):
        return value.removeprefix(prefix)

    labels = _unique([
        *(terminal(value) for value in ic.terminals),
        *(node.removeprefix(prefix) for net in circuit.nets for node in net.nodes if node.startswith(prefix)),
        *(terminal(value) for value in circuit.explicitly_unconnected_pads),
        *(terminal(value) for value in circuit.unobserved_pads),
    ])
    mapping = {}
    for label in labels:
        candidates = by_name.get(_label(label), [])
        explicit = alias_index.get(_label(label), [])
        if label in cited:
            mapping[label] = label
            continue
        explicit_pad = _explicit_pad_name(label, cited)
        if explicit_pad is not None:
            pad = explicit_pad["pad"]
            if not explicit_pad["agrees"]:
                diagnostic("explicit_pad_name_conflict",
                           f"Explicit pad/name token {label!r} disagrees with the cited pin inventory", "error")
                continue
            mapping[label] = pad
            result.provenance.append({
                "circuit_id": circuit.id, "labels": [label], "pads": [pad],
                "strategy": "explicit_pad_matching_literal_source_name",
                "evidence_ids": _unique([*cited[pad].evidence_ids, *ic.evidence_ids]),
                "reason": "Numeric pad and leading literal pin name both agree with the selected inventory; annotations do not certify aliases or wiring",
            })
            continue
        if len(candidates) == 1:
            pin = candidates[0]
            if explicit and any(alias.pad != pin.pad for alias in explicit):
                diagnostic("conflicting_pin_alias", f"Alias {label!r} contradicts the pin inventory", "error")
            mapping[label] = pin.pad
            result.provenance.append({
                "circuit_id": circuit.id, "labels": [label], "pads": [pin.pad],
                "strategy": "cited_pin_name", "evidence_ids": _unique([*pin.evidence_ids, *ic.evidence_ids]),
            })
        elif explicit and len({alias.pad for alias in explicit}) > 1:
            diagnostic("conflicting_pin_alias", f"Multiple source aliases disagree about {label!r}", "error")
        elif explicit and len({alias.pad for alias in explicit}) == 1:
            pad = explicit[0].pad
            # An alias cannot override a contradictory literal physical pin name.
            if candidates and pad not in {pin.pad for pin in candidates}:
                diagnostic("conflicting_pin_alias", f"Alias {label!r} contradicts the pin inventory", "error")
                continue
            mapping[label] = pad
            result.provenance.append({
                "circuit_id": circuit.id, "labels": [label], "pads": [pad],
                "strategy": "explicit_source_alias",
                "evidence_ids": _unique([*cited[pad].evidence_ids, *(e for a in explicit for e in a.evidence_ids)]),
                "reason": "; ".join(alias.reason for alias in explicit),
            })

    grouped: dict[str, list[str]] = defaultdict(list)
    for label in labels:
        if label in mapping:
            continue
        match = re.fullmatch(r"(.+)_([A-Za-z]+|\d+)", label)
        base = _label(match[1]) if match else _label(label)
        if len(by_name.get(base, [])) > 1:
            grouped[base].append(label)
    set_replacements = {}
    dispositions = {terminal(v) for v in [*circuit.explicitly_unconnected_pads, *circuit.unobserved_pads]}
    for base, group_labels in grouped.items():
        physical = sorted((pin.pad for pin in by_name[base]), key=_pad_sort)
        memberships = [{i for i, net in enumerate(circuit.nets) if prefix + label in net.nodes}
                       for label in group_labels]
        common = memberships[0] if memberships else set()
        same_net = len(common) == 1 and all(indices == common for indices in memberships)
        if (len(group_labels) == len(physical) and same_net and not set(group_labels) & dispositions
                and not set(physical) & set(mapping.values())):
            net_index = next(iter(common))
            net = circuit.nets[net_index]
            if net.status == "supported" and net.evidence_ids:
                set_replacements[net_index] = [*set_replacements.get(net_index, []), (group_labels, physical)]
                result.provenance.append({
                    "circuit_id": circuit.id, "labels": group_labels, "pads": physical,
                    "strategy": "complete_same_net_pin_set", "net": net.name,
                    "evidence_ids": _unique([*(e for p in by_name[base] for e in p.evidence_ids),
                                             *net.evidence_ids]),
                    "reason": "Whole repeated-pin set shares one supported net; individual label identity is unknown",
                })
                continue
        diagnostic("ambiguous_repeated_pin", f"Cannot assign repeated labels {group_labels!r} to pads {physical!r}",
                   evidence_ids=[e for p in by_name[base] for e in p.evidence_ids])

    replaced_labels = {label for groups in set_replacements.values() for labels, _ in groups for label in labels}
    replaced_pads = {pad for groups in set_replacements.values() for _, pads in groups for pad in pads}
    for index, net in enumerate(circuit.nets):
        net.nodes = _unique([prefix + mapping.get(node.removeprefix(prefix), node.removeprefix(prefix))
                             if node.startswith(prefix) else node for node in net.nodes])
        for group_labels, physical in set_replacements.get(index, []):
            net.nodes = _unique([node for node in net.nodes if node not in {prefix + v for v in group_labels}]
                                + [prefix + pad for pad in physical])
            net.evidence_ids = _unique([*net.evidence_ids,
                                       *(e for pad in physical for e in cited[pad].evidence_ids)])
    ic.terminals = _unique([mapping.get(terminal(label), terminal(label)) for label in ic.terminals
                           if terminal(label) not in replaced_labels]
                          + sorted(replaced_pads | set(cited), key=_pad_sort))
    ic.evidence_ids = _unique([*ic.evidence_ids, *(e for pin in cited.values() for e in pin.evidence_ids)])
    circuit.explicitly_unconnected_pads = _unique([
        mapping.get(terminal(value), terminal(value)) for value in circuit.explicitly_unconnected_pads
    ])
    circuit.unobserved_pads = _unique([
        mapping.get(terminal(value), terminal(value)) for value in circuit.unobserved_pads
    ])
    for label in labels:
        if label not in mapping and label not in replaced_labels:
            diagnostic("unresolved_pin_label", f"No source-supported physical pad mapping for {label!r}")
    observed = {node.removeprefix(prefix) for net in circuit.nets for node in net.nodes if node.startswith(prefix)}
    accounted = observed | set(circuit.explicitly_unconnected_pads) | set(circuit.unobserved_pads)
    missing = sorted(set(cited) - accounted, key=_pad_sort)
    circuit.unobserved_pads = _unique([*circuit.unobserved_pads, *missing])
    if missing:
        result.provenance.append({
            "circuit_id": circuit.id, "pads": missing, "strategy": "unobserved_inventory_pads",
            "evidence_ids": _unique([e for pad in missing for e in cited[pad].evidence_ids]),
            "reason": "No schematic connection or explicit NC observation exists in the extracted graph",
        })
    conflicts = defaultdict(set)
    for index, net in enumerate(circuit.nets):
        for node in net.nodes:
            if node.startswith(prefix) and node.removeprefix(prefix) in cited:
                conflicts[node].add(index)
    for node, indices in conflicts.items():
        if len(indices) > 1:
            diagnostic("physical_pad_net_conflict", f"{node} belongs to multiple nets after reconciliation", "critical")
