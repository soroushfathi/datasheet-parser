"""Reproducible metrics against a human-reviewed reference, never model confidence."""

import re
from itertools import combinations

UNIT_SCALE = {
    "V": ("V", 1),
    "mV": ("V", 1e-3),
    "A": ("A", 1),
    "mA": ("A", 1e-3),
    "uA": ("A", 1e-6),
    "nA": ("A", 1e-9),
    "F": ("F", 1),
    "uF": ("F", 1e-6),
    "nF": ("F", 1e-9),
    "pF": ("F", 1e-12),
    "s": ("s", 1),
    "ms": ("s", 1e-3),
    "us": ("s", 1e-6),
    "ns": ("s", 1e-9),
    "Hz": ("Hz", 1),
    "kHz": ("Hz", 1e3),
    "MHz": ("Hz", 1e6),
    "ohm": ("ohm", 1),
    "kohm": ("ohm", 1e3),
}


def text_key(value):
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def number_key(value, unit):
    normalized = (unit or "").replace("µ", "u").replace("μ", "u").replace("Ω", "ohm")
    base, scale = UNIT_SCALE.get(normalized, (normalized, 1))
    return (None if value is None else format(value * scale, ".10g"), base)


def claim_key(claim):
    q = claim.get("quantity")
    value = (
        tuple(number_key(q.get(k), q.get("unit")) for k in ["minimum", "typical", "maximum"])
        + (text_key(q.get("expression")),)
        if q
        else text_key(claim.get("value") if claim.get("value") is not None else claim.get("raw_value"))
    )
    return (
        claim["requirement_id"],
        text_key(claim["parameter"]),
        value,
        tuple(sorted(text_key(c) for c in claim.get("conditions", []))),
    )


def metrics(predicted: set, gold: set):
    tp = len(predicted & gold)
    precision = tp / len(predicted) if predicted else None
    recall = tp / len(gold) if gold else None
    f1 = (2 * tp / (len(predicted) + len(gold))) if predicted or gold else None
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "correct": tp,
        "predicted": len(predicted),
        "expected": len(gold),
        "false_positive": len(predicted - gold),
        "false_negative": len(gold - predicted),
    }


def connections(section):
    pairs = set()
    for circuit in section.get("circuits", []):
        for net in circuit["nets"]:
            if net["status"] == "supported":
                # Ignore arbitrary net names; preserve terminals and source figure identity.
                pairs.update(
                    (
                        text_key(circuit["figure"]),
                        tuple(sorted(text_key(c) for c in circuit["conditions"])),
                        a,
                        b,
                    )
                    for a, b in combinations(sorted(set(net["nodes"])), 2)
                )
    return pairs


def evaluate(prediction: dict, reference: dict) -> dict:
    if prediction["document_id"] != reference["document_id"]:
        raise ValueError("Reference must identify the same source PDF")
    for key in [
        "manufacturer",
        "part_number",
        "package_name",
        "package_code",
        "ordering_code",
        "temperature_grade",
    ]:
        if prediction["target"].get(key) != reference["target"].get(key):
            raise ValueError(f"Reference target mismatch: {key}")
    gold = {s["section_id"]: s for s in reference["sections"]}
    pred = {s["section_id"]: s for s in prediction["sections"]}
    sections = {}
    all_pred, all_gold = set(), set()
    for sid in sorted(set(gold) | set(pred)):
        p = {claim_key(c) for c in pred.get(sid, {}).get("claims", []) if c["status"] == "present"}
        g = {claim_key(c) for c in gold.get(sid, {}).get("claims", []) if c["status"] == "present"}
        sections[str(sid)] = metrics(p, g)
        all_pred.update(p)
        all_gold.update(g)

    def pin_keys(section):
        return {
            (
                p["pad"],
                text_key(p["name"]),
                p["electrical_type"],
                tuple(sorted(text_key(c) for c in p["conditions"])),
            )
            for p in section.get("pins", [])
        }

    def component_keys(section):
        return {
            (
                text_key(circuit["figure"]),
                tuple(sorted(text_key(c) for c in circuit["conditions"])),
                component["ref"],
                text_key(component["kind"]),
                text_key(component["value"]),
                tuple(sorted(component["terminals"])),
            )
            for circuit in section.get("circuits", [])
            for component in circuit["components"]
        }

    pred_findings = {f["requirement_id"]: f["status"] for s in pred.values() for f in s.get("findings", [])}
    gold_findings = {f["requirement_id"]: f["status"] for s in gold.values() for f in s.get("findings", [])}
    answered = {r for r, status in pred_findings.items() if status == "present"}
    answerable = {r for r, status in gold_findings.items() if status == "present"}
    return {
        "claims": metrics(all_pred, all_gold),
        "sections": sections,
        "pin_mapping": metrics(pin_keys(pred.get(1, {})), pin_keys(gold.get(1, {}))),
        "net_connections": metrics(connections(pred.get(2, {})), connections(gold.get(2, {}))),
        "component_values_and_terminals": metrics(
            component_keys(pred.get(2, {})), component_keys(gold.get(2, {}))
        ),
        "requirements": {
            "disposition_matches": sum(pred_findings.get(r) == status for r, status in gold_findings.items()),
            "reference_count": len(gold_findings),
            "answerable_coverage": len(answered & answerable) / len(answerable) if answerable else None,
            "unnecessary_abstentions": len(answerable - answered),
            "predicted_abstentions": sum(
                status in {"ambiguous", "conflicting", "unreadable"} for status in pred_findings.values()
            ),
        },
        "limitations": [
            "Text parameter names use normalized exact matching, not semantic grading.",
            "Generated component identifiers must be aligned with the reference before netlist scoring.",
            "Evidence entailment and completeness of reference require human review.",
        ],
    }
