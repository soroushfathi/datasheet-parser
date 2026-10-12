"""Auditable before/after probes for the provided CY7C64225 revision *G PDF.

These probes separate output facts from diagnostic bookkeeping. They are not a
gold-standard evaluator or an aggregate accuracy score.
"""

import json
import re
import unicodedata
from collections import Counter, defaultdict
from hashlib import sha256
from pathlib import Path

import pdfplumber

from .models import ExtractionResult, SectionResult
from .quality_policy import ownership_diagnostics

EXPECTED_BAUD_RATES = [
    300,
    600,
    1200,
    1800,
    2400,
    3600,
    4800,
    7200,
    9600,
    14400,
    19200,
    28800,
    38400,
    56000,
    57600,
    115200,
    230400,
]
PIN_CODES = {"package_pin_mismatch", "unknown_pad_disposition", "unaccounted_ic_pads"}
REPORT_VERSION = "cy-quality-comparison-2"


def read_source_facts(path: Path) -> dict:
    """Confirm the exact supplied document and derive printed inventories.

    Table 7 operators were independently inspected in its page image; plain PDF
    text contains broken glyph mappings and cannot be used to verify those signs.
    """
    with pdfplumber.open(path) as document:
        pages = [page.extract_text() or "" for page in document.pages]
        pin_tables = document.pages[5].extract_tables() if len(document.pages) >= 6 else []
    if len(pages) != 21 or "CY7C64225" not in pages[0] or "Rev. *G" not in pages[14]:
        raise ValueError("Source must be the supplied 21-page CY7C64225 revision *G datasheet")
    text = pages[3]
    match = re.search(r"baud rates of\s+(.*?)\s+with even", text, re.DOTALL | re.IGNORECASE)
    rates = [int(value) for value in re.findall(r"\b\d+\b", match.group(1))] if match else []
    figures = sorted(
        {int(value) for page in pages[6:13] for value in re.findall(r"^Figure\s+(\d+)\.", page, re.MULTILINE)}
    )
    if rates != EXPECTED_BAUD_RATES or figures != list(range(3, 10)):
        raise ValueError("Printed source inventory differs from the independently audited reference facts")
    if "Bus/Self Powered Self" not in pages[4] or "Remote Wake-Up Enabled" not in pages[4]:
        raise ValueError("Source Table 1 defaults could not be confirmed")
    if not re.search(r"different sources, 1K resistors are required on RXD and CTS", pages[11]):
        raise ValueError("Source conditional resistor instruction could not be confirmed")
    pin_names = {
        str(row[0]).strip(): str(row[1]).replace("\n", "").strip()
        for table in pin_tables
        for row in table[1:]
        if len(row) >= 2 and str(row[0] or "").strip().isdecimal() and row[1]
    }
    if set(pin_names) != {str(i) for i in range(1, 29)} or not re.search(
        r"integrates a 24 MHz oscillator", pages[3]
    ):
        raise ValueError("Source physical pin inventory or nominal clock could not be confirmed")
    return {
        "filename": path.name,
        "document_sha256": sha256(path.read_bytes()).hexdigest(),
        "revision": "*G",
        "application_figures": figures,
        "physical_pads": [str(i) for i in range(1, 29)],
        "pin_names": pin_names,
        "nominal_clock_mhz": 24,
        "baud_rates": rates,
        "usb_rate_mbps": 12,
        "default_power_mode": "Self",
        "default_remote_wake": "Enabled",
        "isb_condition": "VDD = 3.3 V; 0°C ≤ TA ≤ 55°C",
        "isbh_condition": "VDD = 3.3 V; 55°C < TA ≤ 70°C",
        "operator_verification": "Independent visual inspection of original PDF page 15, Table 7; raw extracted text loses ≤ glyphs",
        "conditional_resistor": "Figure 8 scenario is both devices powered from VBUS; 1 kΩ on RXD/CTS applies when the devices use different power sources",
        "page_references": {
            "figures": [7, 8, 9, 10, 11, 12, 13],
            "pins": [6],
            "baud_rates": [4],
            "defaults": [5],
            "sleep_conditions": [15],
            "conditional_resistor": [12],
        },
    }


def _sections(data):
    return {section["section_id"]: section for section in data["sections"]}


def _claims(data, section_ids=None):
    return [
        claim
        for section in data["sections"]
        if section_ids is None or section["section_id"] in section_ids
        for claim in section["claims"]
    ]


def _text(claim):
    return " ".join(
        [
            claim.get("parameter", ""),
            claim.get("raw_value") or "",
            claim.get("value") or "",
            *claim.get("conditions", []),
        ]
    )


def _sample(claim):
    # IDs are display references only; matching never relies on stable generated IDs.
    sample = {
        key: claim.get(key)
        for key in [
            "id",
            "requirement_id",
            "parameter",
            "raw_value",
            "quantity",
            "conditions",
            "status",
            "evidence_ids",
        ]
    }
    truncated = []
    if isinstance(sample.get("raw_value"), str) and len(sample["raw_value"]) > 600:
        sample["raw_value"] = sample["raw_value"][:600] + "…"
        truncated.append("raw_value")
    conditions = sample.get("conditions") or []
    if len(conditions) > 3 or any(len(value) > 600 for value in conditions):
        sample["conditions"] = [value[:600] + ("…" if len(value) > 600 else "") for value in conditions[:3]]
        truncated.append("conditions")
    if truncated:
        sample["excerpted_fields"] = truncated
    return sample


def _trend(before_score, after_score):
    return (
        "improved"
        if after_score > before_score
        else "regressed"
        if after_score < before_score
        else "unchanged"
    )


def _probe(name, before, after, source, *, bigger_is_better=True, manual=False):
    left, right = before.pop("score"), after.pop("score")
    trend = _trend(left, right) if bigger_is_better else _trend(-left, -right)
    manual = manual or bool(before.get("missing_match") and after.get("missing_match"))
    return {
        "probe": name,
        "status": "needs_manual" if manual else trend,
        "measured_change": trend,
        "before": before,
        "after": after,
        "source": source,
    }


def _figure_inventory(data, expected):
    circuits = _sections(data).get(2, {}).get("circuits", [])
    figures, unresolved = [], []
    for circuit in circuits:
        match = re.search(r"\b(?:figure|fig)\s*(\d+)\b", circuit["figure"], re.IGNORECASE)
        if match:
            figures.append(int(match.group(1)))
        else:
            unresolved.append({"id": circuit["id"], "figure": circuit["figure"]})
    return {
        "score": len(set(figures) & set(expected)),
        "circuit_count": len(circuits),
        "figure_numbers": sorted(set(figures)),
        "missing_figures": sorted(set(expected) - set(figures)),
        "duplicate_figures": {str(k): v for k, v in Counter(figures).items() if v > 1},
        "unparsed_figures": unresolved,
        "limit": "Presence of a circuit object does not certify its engineering connectivity",
    }


def _unknown_pin_token(token, prefix, expected, pin_names):
    def normalize(value):
        return unicodedata.normalize("NFKC", value).translate(str.maketrans("−–‐‑", "----")).casefold()

    token = token.removeprefix(prefix)
    match = re.fullmatch(r"(?:pad\s*)?(\d+)(?:\s*[:(]\s*|\s+)(.+)", token, re.IGNORECASE)
    if match:
        pad, name = match.groups()
        if pad not in expected:
            return "unknown_numeric_pad"
        name = re.split(
            r"\s*(?:;\s*(?:schematic|figure)\s+alias\b|/\s*figure\s+alias\b|\(\s*figure\b)",
            name.rstrip(")"),
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip()

        if pin_names and normalize(name) != normalize(pin_names.get(pad, "")):
            return "explicit_pad_name_disagreement"
        return "numeric_format_candidate"
    literal_matches = [pad for pad, name in (pin_names or {}).items() if normalize(token) == normalize(name)]
    if len(literal_matches) == 1:
        return "unique_literal_name_format_candidate"
    if len(literal_matches) > 1:
        return "repeated_pin_label_needs_graph_context"
    suffix = re.fullmatch(r"(.+)_([A-Za-z]+|\d+)", token)
    if suffix and sum(normalize(suffix[1]) == normalize(name) for name in (pin_names or {}).values()) > 1:
        return "repeated_pin_occurrence_label_needs_graph_context"
    return "unknown_numeric_pad" if token.isdecimal() else "unresolved_symbolic_or_prose_label"


def _physical_pads(data, expected, pin_names=None):
    expected = set(expected)
    sections = _sections(data)
    inventory = {pin["pad"] for pin in sections.get(1, {}).get("pins", [])}
    rows = []
    token_counts = Counter()
    for circuit in sections.get(2, {}).get("circuits", []):
        prefix = circuit["ic_ref"] + "."
        observed = {
            node.removeprefix(prefix)
            for net in circuit["nets"]
            for node in net["nodes"]
            if node.startswith(prefix)
        }
        explicit_nc = set(circuit["explicitly_unconnected_pads"])
        unobserved = set(circuit["unobserved_pads"])
        accounted = observed | explicit_nc | unobserved
        component = next((row for row in circuit["components"] if row["ref"] == circuit["ic_ref"]), None)
        terminals = set(component["terminals"]) if component else set()
        classified = defaultdict(list)
        for token in sorted((accounted | terminals) - expected):
            classified[_unknown_pin_token(token, prefix, expected, pin_names)].append(token)
        token_counts.update({kind: len(tokens) for kind, tokens in classified.items()})
        rows.append(
            {
                "figure": circuit["figure"],
                "accounted_known_pads": sorted(accounted & expected, key=int),
                "missing_physical_pads": sorted(expected - accounted, key=int),
                "unknown_dispositions": sorted(accounted - expected),
                "unknown_ic_terminals": sorted(terminals - expected),
                "observed_physical_pads": sorted(observed & expected, key=int),
                "explicit_nc": sorted(explicit_nc),
                "unobserved": sorted(unobserved),
                "unknown_token_classification": dict(classified),
            }
        )
    safe = sum(
        not row["missing_physical_pads"]
        and not row["unknown_dispositions"]
        and not row["unknown_ic_terminals"]
        for row in rows
    )
    return {
        "score": safe / len(rows) if rows and inventory == expected else 0,
        "inventory_matches_28_pads": inventory == expected,
        "structurally_accounted_circuits": safe,
        "circuits": rows,
        "critical_pin_issue_count": sum(
            issue.get("code") in PIN_CODES and issue.get("severity") == "critical" for issue in data["issues"]
        ),
        "unknown_pin_token_counts": dict(token_counts),
        "token_classification_limit": "Numeric formatting candidates require matching source names; symbolic aliases remain source ambiguities, not mere format errors",
        "limit": "Observed/NC/unobserved accounting is structural; unobserved is not proof of NC or correct wiring",
    }


def _math(text):
    text = unicodedata.normalize("NFKC", text).lower().replace("≤", "<=").replace("≥", ">=")
    text = text.replace("\\leq", "<=").replace("\\le", "<=").replace("\\geq", ">=")
    text = re.sub(r"(?:degrees?\s*celsius|°\s*c)", "c", text)
    text = re.sub(r"t\s*[_ {]?\s*a\s*}?", "ta", text)
    return re.sub(r"\s+", "", text)


def _sleep_conditions(data, sid, high):
    found = []
    for claim in _claims(data, {sid}):
        parameter = claim["parameter"]
        is_high = bool(
            re.search(r"\bI[_\s]*S[_\s]*B[_\s]*H\b|sleep.*high temperature", parameter, re.IGNORECASE)
        )
        is_low = (
            bool(re.search(r"\bI[_\s]*S[_\s]*B\b|sleep.*(?:mode|current)", parameter, re.IGNORECASE))
            and not is_high
        )
        if claim["status"] != "present" or not (is_high if high else is_low):
            continue
        text = _math(" ".join([claim.get("raw_value") or "", claim.get("value") or "", *claim["conditions"]]))
        low, upper = (55, 70) if high else (0, 55)
        low_operator = "<" if high else "<="
        chain = rf"(?<!\d){low}c?{re.escape(low_operator)}ta<={upper}c?(?!\d)"
        lower = bool(re.search(rf"ta{'>' if high else '>='}{low}c?(?!\d)", text)) or bool(
            re.search(rf"(?<!\d){low}c?{re.escape(low_operator)}ta", text)
        )
        upper_ok = bool(re.search(rf"ta<={upper}c?(?!\d)", text)) or bool(
            re.search(rf"(?<!\d){upper}c?>=ta", text)
        )
        individual = lower and upper_ok
        correct = bool(re.search(chain, text)) or individual
        wrong = bool(re.search(rf"(?<!\d){low}c?(?:<|<=)ta<{upper}c?(?!\d)", text))
        found.append(
            {**_sample(claim), "source_predicate_preserved": correct, "strict_upper_bound_error": wrong}
        )
    good = sum(row["source_predicate_preserved"] and not row["strict_upper_bound_error"] for row in found)
    return {
        "score": good / len(found) if found else 0,
        "matched_claim_count": len(found),
        "correct_predicate_count": good,
        "examples": found[:3],
        "missing_match": not found,
    }


def _baud_inventory(data, sid, expected):
    rows, numeric_tokens = [], set()
    for claim in _claims(data, {sid}):
        text = " ".join([claim["parameter"], claim.get("raw_value") or "", claim.get("value") or ""])
        if not re.search(r"baud.*rate|rate.*baud", text, re.IGNORECASE):
            continue
        numbers = {int(value) for value in re.findall(r"\b\d+\b", text) if int(value) >= 300}
        if len(numbers) >= 3:
            rows.append(_sample(claim))
            numeric_tokens.update(numbers)
    matched = numeric_tokens & set(expected)
    return {
        "score": len(matched),
        "covered_printed_rates": sorted(matched),
        "missing_printed_rates": sorted(set(expected) - matched),
        "other_numeric_tokens_for_manual_check": sorted(numeric_tokens - set(expected)),
        "examples": rows[:2],
        "limit": "Union of explicit discrete list tokens; interval endpoints do not establish intermediate baud rates",
    }


def _timing_baud_context(data, expected, nominal_mhz):
    local = _baud_inventory(data, 12, expected)
    owner = _baud_inventory(data, 10, expected)
    covered = set(local["covered_printed_rates"]) | set(owner["covered_printed_rates"])
    clocks, invented_bound_candidates = [], []
    for claim in _claims(data, {12}):
        if claim["status"] != "present" or claim["requirement_id"] != "12.clocks":
            continue
        text = " ".join([claim["parameter"], claim.get("raw_value") or "", claim.get("value") or ""])
        if re.search(r"baud|Mbps", text, re.IGNORECASE) or not re.search(
            r"clock|oscillator|frequency|MHz", text, re.IGNORECASE
        ):
            continue
        quantity = claim.get("quantity") or {}
        has_bounds = quantity.get("minimum") is not None or quantity.get("maximum") is not None
        if has_bounds:
            invented_bound_candidates.append(_sample(claim))
        elif re.search(rf"\b{nominal_mhz}(?:\.0)?\s*MHz\b", text, re.IGNORECASE) or (
            (quantity.get("unit") or "").casefold() == "mhz"
            and any(
                str(v) in {str(nominal_mhz), str(float(nominal_mhz))}
                for v in [quantity.get("typical"), quantity.get("expression")]
            )
        ):
            clocks.append(_sample(claim))
    return {
        "score": len(covered),
        "covered_printed_rates": sorted(covered),
        "missing_printed_rates": sorted(set(expected) - covered),
        "canonical_owner": "10.protocols",
        "local_list": {k: v for k, v in local.items() if k != "score"},
        "owner_list": {k: v for k, v in owner.items() if k != "score"},
        "local_copy_is_required": False,
        "complete_owned_list_available": not owner["missing_printed_rates"],
        "nominal_clock_examples": clocks[:2],
        "frequency_bound_claims_needing_source_check": invented_bound_candidates[:2],
        "nominal_versus_limit_distinction_recorded": bool(clocks) and not invented_bound_candidates,
        "limit": "Informational cross-section availability: section 12 need not duplicate baud rates owned by section 10. Nominal clock presence does not verify guaranteed limits.",
    }


def _nc_marker_warning_candidates(data):
    candidates = []
    for circuit in _sections(data).get(2, {}).get("circuits", []):
        prefix = circuit["ic_ref"] + "."
        for net in circuit["nets"]:
            for pad in circuit["explicitly_unconnected_pads"]:
                node = prefix + pad
                if node not in net["nodes"]:
                    continue
                others = [other for other in net["nodes"] if other != node]
                if not others or all(
                    re.fullmatch(r"(?:schematic label\s+)?NO\s+CONNECT", other, re.IGNORECASE)
                    for other in others
                ):
                    candidates.append(
                        {
                            "figure": circuit["figure"],
                            "pad": pad,
                            "net_name": net["name"],
                            "nodes": list(net["nodes"]),
                            "evidence_ids": list(net["evidence_ids"]),
                            "disposition": "candidate_NC_marker_or_singleton_stub_requires_manual_source_review",
                        }
                    )
    warnings = [issue for issue in data["issues"] if issue.get("code") == "nc_connected"]
    return {
        "nc_connected_warning_count": len(warnings),
        "marker_or_singleton_nc_net_count": len(candidates),
        "warning_candidate_upper_bound": min(len(warnings), len(candidates)),
        "examples": candidates[:5],
        "limit": "Candidate representation mismatch only; no warning or source connectivity is automatically removed",
    }


def _wrong_quantity_warnings(data):
    claims = {claim["id"]: claim for claim in _claims(data)}
    rows = [
        {"issue": issue, "claim": _sample(claims[issue["claim_id"]])}
        for issue in data["issues"]
        if issue.get("code") == "missing_quantity"
        and issue.get("claim_id") in claims
        and claims[issue["claim_id"]]["status"] == "not_found"
    ]
    return {
        "score": len(rows),
        "count": len(rows),
        "examples": rows[:4],
        "limit": "This measures false validator warnings, not extraction accuracy",
    }


def _present_fact(claim, fact):
    if claim["status"] != "present" or not claim.get("evidence_ids"):
        return False
    text = _text(claim)
    if fact == "usb_12mbps":
        text = " ".join([claim["parameter"], claim.get("raw_value") or "", claim.get("value") or ""])
        quantity = claim.get("quantity") or {}
        qvalue = [
            quantity.get("minimum"),
            quantity.get("typical"),
            quantity.get("maximum"),
            quantity.get("expression"),
        ]
        value = bool(re.search(r"\b12\s*Mbps\b", text, re.IGNORECASE)) or (
            (quantity.get("unit") or "").lower() == "mbps" and any(str(v) in {"12", "12.0"} for v in qvalue)
        )
        return value and bool(re.search(r"\bUSB\b", text, re.IGNORECASE))
    if fact == "self_powered_default":
        return bool(
            re.search(r"\bdefault\b", text, re.IGNORECASE)
            and re.search(r"\bself(?:[ -]powered)?\b", text, re.IGNORECASE)
        )
    return bool(
        re.search(r"remote[ -]?wake[ -]?up", text, re.IGNORECASE)
        and re.search(r"\bdefault\b", text, re.IGNORECASE)
        and re.search(r"\benabled\b", text, re.IGNORECASE)
    )


def _missing_fact_candidates(data):
    rows = []
    for issue in data["issues"]:
        if issue.get("category") != "missing_fact":
            continue
        allegation = issue.get("explanation", "")
        candidates = []
        if re.search(r"12[ -]?\s*Mbps", allegation, re.IGNORECASE):
            candidates.append("usb_12mbps")
        if re.search(r"default", allegation, re.IGNORECASE) and re.search(
            r"\bSelf\b", allegation, re.IGNORECASE
        ):
            candidates.append("self_powered_default")
        if re.search(r"default", allegation, re.IGNORECASE) and re.search(
            r"Remote.*Wake", allegation, re.IGNORECASE
        ):
            candidates.append("remote_wake_default_enabled")
        sid = issue.get("section_id")
        for fact in candidates:
            matched = [claim for claim in _claims(data, {sid} if sid else None) if _present_fact(claim, fact)]
            if matched:
                rows.append(
                    {
                        "fact": fact,
                        "allegation": issue,
                        "matching_existing_claims": [_sample(c) for c in matched[:2]],
                        "disposition": "candidate_false_absence_requires_manual_review",
                    }
                )
    return {
        "score": len(rows),
        "candidate_count": len(rows),
        "candidate_allegation_count": len(
            {(row["allegation"].get("requirement_id"), row["allegation"].get("explanation")) for row in rows}
        ),
        "examples": rows,
        "limit": "A matching fact does not prove the entire allegation false; scope and qualifiers must be checked",
    }


def _ownership(data):
    rows = [
        issue
        for section in data["sections"]
        for issue in ownership_diagnostics(SectionResult.model_validate(section))
    ]
    return {
        "score": len(rows),
        "high_confidence_misplacement_count": len(rows),
        "examples": rows[:12],
        "by_owner": dict(Counter(issue["requirement_id"] for issue in rows)),
        "limit": "Guard-only classification; facts are retained, destination accuracy is not certified",
    }


def _resistor_conditions(data):
    rows = []
    for claim in _claims(data):
        text = _text(claim)
        if (
            claim["status"] != "present"
            or not re.search(r"RXD", text, re.IGNORECASE)
            or not re.search(r"\bCTS\b", text, re.IGNORECASE)
        ):
            continue
        if not re.search(r"1\s*k\s*(?:Ω|ohm)?|1000|1K resist", text, re.IGNORECASE):
            continue
        scenario = [condition for condition in claim["conditions"] if condition.startswith("Scenario:")]
        predicates = [
            condition
            for condition in claim["conditions"]
            if condition.startswith("Applies when:")
            and re.search(r"different (?:power )?sources", condition, re.IGNORECASE)
        ]
        contradictory = [
            condition
            for condition in claim["conditions"]
            if not condition.startswith("Scenario:")
            and re.search(r"both.*powered from VBUS", condition, re.IGNORECASE)
        ]
        rows.append(
            {
                **_sample(claim),
                "scenario_labels": scenario,
                "different_sources_predicates": predicates,
                "scenario_misreported_as_predicate": contradictory,
            }
        )
    correct = sum(
        bool(row["different_sources_predicates"]) and not row["scenario_misreported_as_predicate"]
        for row in rows
    )
    return {
        "score": correct / len(rows) if rows else 0,
        "matched_claim_count": len(rows),
        "clearly_scoped_claims": correct,
        "examples": rows[:4],
        "limit": "Presence of labels measures predicate separation; circuit resistor placement still needs source review",
    }


def _diagnostics(data):
    issues = data["issues"]
    model = [issue for issue in issues if issue.get("category")]
    code = [issue for issue in issues if issue.get("code")]
    metadata = data.get("metadata", {})
    reconciliations = metadata.get("review_reconciliation", {})
    raw_count = sum(len(row.get("raw_review", {}).get("issues", [])) for row in reconciliations.values())
    decisions = [decision for row in reconciliations.values() for decision in row.get("decisions", [])]
    linked = metadata.get("quality_audit", {}).get("linked_review_diagnostics", [])
    return {
        "main_issue_count": len(issues),
        "code_generated_count": len(code),
        "model_review_count": len(model),
        "severity": dict(Counter(issue.get("severity", "unspecified") for issue in issues)),
        "model_categories": dict(Counter(issue["category"] for issue in model)),
        "validator_codes": dict(Counter(issue["code"] for issue in code)),
        "raw_review_count": raw_count if reconciliations else None,
        "raw_review_count_available": bool(reconciliations),
        "reconciled_allegation_count": len(decisions),
        "linked_diagnostic_count": len(linked),
        "nc_marker_warning_candidates": _nc_marker_warning_candidates(data),
        "limit": "Differences include new grouping/guards and model variability; issue totals cannot be interpreted as accuracy",
    }


def _claim_changes(before, after):
    def key(claim):
        return (claim["requirement_id"], " ".join(claim["parameter"].casefold().split()))

    left, right = {}, {}
    for claim in _claims(before):
        left.setdefault(key(claim), []).append(claim)
    for claim in _claims(after):
        right.setdefault(key(claim), []).append(claim)
    changed = []
    for match in sorted(left.keys() & right.keys()):
        if len(left[match]) != 1 or len(right[match]) != 1:
            continue
        before_claim, after_claim = left[match][0], right[match][0]
        ignored = {"id", "evidence_ids", "applies_to"}
        a = {key: value for key, value in before_claim.items() if key not in ignored}
        b = {key: value for key, value in after_claim.items() if key not in ignored}
        if a != b:
            changed.append(
                {
                    "match": {"requirement_id": match[0], "parameter": match[1]},
                    "before": _sample(before_claim),
                    "after": _sample(after_claim),
                }
            )
    return {
        "matching_method": "Exact normalized requirement+parameter; generated claim IDs and source parse IDs ignored",
        "matched_unique_keys": sum(len(left[k]) == len(right[k]) == 1 for k in left.keys() & right.keys()),
        "changed_matched_claims": len(changed),
        "changed_examples": changed[:12],
        "unmatched_before_keys": len(left.keys() - right.keys()),
        "unmatched_after_keys": len(right.keys() - left.keys()),
        "limit": "Renamed/split claims are unmatched, not automatically lost/new engineering facts",
    }


def compare_outputs(before: dict, after: dict, source_facts: dict) -> dict:
    """Compare measured source probes; never modify either extraction artifact."""
    probes = []
    figures = source_facts["application_figures"]
    probes.append(
        _probe(
            "application_schematic_inventory",
            _figure_inventory(before, figures),
            _figure_inventory(after, figures),
            {"pages": [7, 8, 9, 10, 11, 12, 13], "figures": figures},
        )
    )
    probes.append(
        _probe(
            "physical_pad_accounting",
            _physical_pads(before, source_facts["physical_pads"], source_facts.get("pin_names")),
            _physical_pads(after, source_facts["physical_pads"], source_facts.get("pin_names")),
            {"page": 6, "pads": source_facts["physical_pads"]},
        )
    )
    for sid in [9, 15]:
        for high in [False, True]:
            name = "ISBH" if high else "ISB"
            probes.append(
                _probe(
                    f"section_{sid}_{name}_source_predicate",
                    _sleep_conditions(before, sid, high),
                    _sleep_conditions(after, sid, high),
                    {
                        "page": 15,
                        "table": 7,
                        "expected": source_facts["isbh_condition" if high else "isb_condition"],
                        "basis": "visually_verified_original_table",
                    },
                )
            )
    for sid in [10]:
        probes.append(
            _probe(
                f"section_{sid}_complete_discrete_baud_list",
                _baud_inventory(before, sid, source_facts["baud_rates"]),
                _baud_inventory(after, sid, source_facts["baud_rates"]),
                {"page": 4, "rates": source_facts["baud_rates"]},
            )
        )
    timing_before = _timing_baud_context(
        before, source_facts["baud_rates"], source_facts.get("nominal_clock_mhz", 24)
    )
    timing_after = _timing_baud_context(
        after, source_facts["baud_rates"], source_facts.get("nominal_clock_mhz", 24)
    )
    local_trend = _trend(
        len(timing_before["local_list"]["covered_printed_rates"]),
        len(timing_after["local_list"]["covered_printed_rates"]),
    )
    timing = _probe(
        "section_12_complete_discrete_baud_list",
        timing_before,
        timing_after,
        {"page": 4, "rates": source_facts["baud_rates"], "canonical_owner": "10.protocols"},
    )
    timing["classification"] = "informational_scope_aware_cross_section_availability"
    timing["local_copy_change_informational_only"] = local_trend
    probes.append(timing)
    probes.append(
        _probe(
            "absent_quantity_false_validator_warnings",
            _wrong_quantity_warnings(before),
            _wrong_quantity_warnings(after),
            {"basis": "Absent quantities do not require invented numeric values"},
            bigger_is_better=False,
        )
    )
    probes.append(
        _probe(
            "already_recorded_fact_absence_allegation_candidates",
            _missing_fact_candidates(before),
            _missing_fact_candidates(after),
            {"pages": [5, 15], "facts": ["USB 12 Mbps", "Self default", "Remote Wake-Up Enabled default"]},
            bigger_is_better=False,
            manual=True,
        )
    )
    probes.append(
        _probe(
            "high_confidence_requirement_scope_contamination",
            _ownership(before),
            _ownership(after),
            {
                "basis": "Package geometry versus thermal/reflow; operating voltage versus protection; schematic versus PCB rules"
            },
            bigger_is_better=False,
        )
    )
    probes.append(
        _probe(
            "RXD_CTS_1k_scenario_and_validity_predicate",
            _resistor_conditions(before),
            _resistor_conditions(after),
            {"page": 12, "expected": source_facts["conditional_resistor"]},
        )
    )
    return {
        "report_version": REPORT_VERSION,
        "source_facts": source_facts,
        "before": {"run_id": before["run_id"], "status": before["status"], "parse_id": before["parse_id"]},
        "after": {"run_id": after["run_id"], "status": after["status"], "parse_id": after["parse_id"]},
        "source_audited_data_probes": probes,
        "diagnostic_bookkeeping": {"before": _diagnostics(before), "after": _diagnostics(after)},
        "claim_data_changes": _claim_changes(before, after),
        "interpretation": [
            "No aggregate accuracy percentage is computed.",
            "Fewer issues can result from grouping or reconciliation; inspect data probes separately.",
            "Presence and structural checks do not certify every value, condition or circuit edge.",
            "Candidate false-absence allegations require manual scope and qualifier review.",
        ],
    }


def compare_files(before_path: Path, after_path: Path, source_path: Path) -> dict:
    before = json.loads(before_path.read_text(encoding="utf-8"))
    after = json.loads(after_path.read_text(encoding="utf-8"))
    ExtractionResult.model_validate(before)
    ExtractionResult.model_validate(after)
    facts = read_source_facts(source_path)
    if before["document_id"] != facts["document_sha256"] or after["document_id"] != before["document_id"]:
        raise ValueError("Both outputs must refer to the exact source PDF")
    if before["target"]["ordering_code"] != after["target"]["ordering_code"]:
        raise ValueError("Cannot compare different selected ordering codes")
    return compare_outputs(before, after, facts)
