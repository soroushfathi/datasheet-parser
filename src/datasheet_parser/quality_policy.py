"""Source-faithful extraction policy and conservative semantic coverage metadata.

Coverage describes what the extracted claims say, not document completeness or
engineering accuracy. Deterministic guards flag misplaced facts without deleting,
moving, rewriting or certifying any claim.
"""

import re
from dataclasses import dataclass, field

from .models import Claim, SectionResult
from .requirements import SECTIONS, requirements

QUALITY_POLICY_VERSION = "source-faithful-2"

_COMMON = """SOURCE-FAITHFUL SEMANTIC POLICY:
Check every named subfield of each requirement separately. A nearby related fact
does not establish that requirement: a schematic is not a PCB rule, a supply range
is not a protection threshold, and a nominal frequency is not a frequency limit.
When only some subfields are evidenced, explain the partial coverage in the finding
reason and retain explicit unresolved/not_found claims for the other inspected
subfields. Never imply that an unseen subfield is absent from the entire document.
Keep one natural owning requirement per claim. A related sibling requirement may
refer to the shared fact in its finding reason; do not duplicate the fact simply
to mark both requirements present. Shared context never proves a missing detail.
Preserve source operators exactly: < versus ≤, > versus ≥, ±, polarity, units and
minimum/typical/maximum roles. If source text and image disagree, use the image only
when legible and preserve the source discrepancy explicitly; never guess a bound.
In conditions, prefix an example/application context with 'Scenario:' and an
actual validity or operating predicate with 'Applies when:'. Keep the source words
after the prefix. A scenario is not a universal design requirement. Do not invent
a predicate, additional qualifier, connection or engineering recommendation.
"""

_SECTION_POLICY = {
    1: "Enumerate all selected-package physical pads, including repeated supplies and NC. Keep source I/O types unresolved when the table says only USB; do not infer bidirectional. Distinguish literal labels, alternate functions and source-backed aliases.",
    2: "Read every existing application schematic separately. Use physical IC pad IDs from the raw selected-package pin table. Repeated GND/VDD symbols require source-grounded set connectivity or explicit ambiguity. Account for every pad as observed, explicit schematic NC, or unobserved. Package NC labels alone do not prove a schematic NC marker. Do not design a new circuit.",
    3: "Separate absolute maximum supply/pin limits, current/power limits, junction/storage limits and each ESD test model/pin scope. A recommended operating range cannot fill an absolute maximum requirement.",
    4: "Separate recommended supply bounds, temperature grade and input/output logic thresholds. Read footnotes and conditional exclusions in full. Logic voltage ratings and absolute maximum limits are different facts.",
    5: "Check power rails, sequencing, decoupling per rail, bulk capacitance and startup/inrush separately. An application capacitor value does not prove a mandatory decoupling placement/type rule or a universal startup requirement.",
    6: "Equations need an actual stated calculation rule, not merely a regulator example. Component types must describe external component tolerance, rating, dielectric or ESR. IC supply voltage and θJA/θJC are not external-component types. A resistor value alone does not establish tolerance, power rating or dielectric. Distinguish internal nominal oscillator from external crystal requirements.",
    7: "Dimensions are physical package/land geometry, pitch, height and tolerances for the selected package. θJA/θJC/ψJT belong to 11.metrics and reflow profile/peak/dwell to 17.reflow. RoHS belongs to 17.compliance. An outline drawing is not a recommended land pattern or keep-out rule.",
    8: "Only stated PCB layout rules establish routing, ground-plane/return-path or keep-out coverage. Schematic nets, package dimensions and a thermal test-board assumption do not establish physical trace width/spacing/impedance, ground-plane or component-placement instructions. Keep them in their natural sections.",
    9: "Preserve test conditions, pin groups and all applicable electrical characteristic rows. Do not combine rows with different voltage, temperature or mode predicates. Input/output drive, leakage and capacitance are distinct subfields.",
    10: "Read protocol modes, speed, pins, addressing/configuration defaults and logic/loading separately. Preserve a complete supported baud-rate list and feature defaults. Existing protocol-speed facts may be referenced by timing findings without duplicating or asserting unspecified clock limits. Distinguish current magnitude from sink/source direction and an explicitly stated numerical sign convention. A prose separator dash in a pin description is not automatically a negative numeric bound. Cross-check a dash-adjacent current against explicit driver/loading prose in the supplied evidence; when the source explicitly states a positive maximum magnitude, retain that magnitude with sink/source direction separately. Preserve the raw dash lexeme and explain ambiguity if the sign cannot be resolved; never invent a signed convention.",
    11: "List each stated θJA, θJC, θJB, ψJT or other thermal metric separately with package and test-board conditions. A θJA value does not establish θJC or ψJT. Distinguish thermal shutdown/derating from ordinary operating-temperature limits. Reflow peak/time belong to 17.reflow; a test PCB layer count is a measurement condition, not a copper/via layout recommendation.",
    12: "Distinguish nominal/reference clock frequency, guaranteed minimum/maximum frequency and supported discrete baud rates. A 24 MHz nominal oscillator does not prove frequency bounds or accuracy. Extract setup/hold/delay and startup/reset timing only when actually stated; include full mode scope and lists.",
    13: "Report only actual protection mechanisms and their thresholds, latch/recovery behavior or limitations. A plain VDD supply range belongs to 4.supply or 5.rails and cannot establish 13.behavior. A USB nonfunctional voltage interval is an operating limitation, not proof of a UVLO/OVP mechanism.",
    14: "Keep selected orderable SKU, package suffix, grade/qualification, document revision and document-stated lifecycle separate. Do not infer an ordering-code suffix combination or current lifecycle from external knowledge.",
    15: "Preserve errata, workaround prerequisites, specification footnotes and revision-history changes separately. A normal application note is not necessarily errata. Distinguish contradictory sources from your own interpretation.",
    16: "Inventory each actual performance graph with figure, axes, units and source conditions. A schematic, pinout or package drawing is not a performance graph. Do not invent digitized points or a safe-operating-area boundary.",
    17: "Separate reflow temperature/profile/dwell, MSL/stencil/assembly instructions and stated compliance. Preserve source minimum-versus-maximum conflicts rather than silently choosing. A package outline alone does not establish a stencil or land-pattern recommendation.",
}


# Named fields are probes of extracted coverage, not universal device requirements.
_FIELDS = {
    "1.pads": {
        "physical_pads": r"\b(?:pad|pin|pinout)\b",
        "package_scope": r"\b(?:package|pinout|SSOP|SOIC|QFN)\b",
    },
    "1.functions": {
        "electrical_type": r"\b(?:input|output|ground|supply|electrical type)\b",
        "alternate_functions": r"\b(?:alternate|multiplex|function|UART|USB)\b",
        "mode_scope": r"\b(?:mode|active|suspend|flow control)\b",
    },
    "1.connections": {
        "unused_pins": r"\b(?:unused|no connect|NC|float)\b",
        "pulls": r"\b(?:pull.up|pull.down|bias)\b",
        "exposed_pad": r"\b(?:exposed pad|thermal pad)\b",
    },
    "2.circuits": {"schematic_inventory": r"\b(?:schematic|application circuit|figure)\b"},
    "2.components": {
        "references": r"\b(?:component|resistor|capacitor|regulator|connector)\b",
        "values": r"\b(?:resistor|capacitor|value|rating)\b",
    },
    "2.connections": {
        "observed_connectivity": r"\b(?:connect|connection|net|schematic)\b",
        "nc_vs_unobserved": r"\b(?:no connect|NC|unobserved|dangling)\b",
    },
    "3.voltages": {
        "supply_limits": r"\b(?:supply|VDD|VCC)\b",
        "pin_limits": r"\b(?:pin voltage|input voltage|output voltage|VIN|VOUT)\b",
    },
    "3.currents_power": {
        "current_limits": r"\b(?:current|mA|ampere)\b",
        "power_limits": r"\b(?:power|dissipation|mW)\b",
    },
    "3.temperatures": {
        "junction_temperature": r"\b(?:junction|TJ)\b",
        "storage_temperature": r"\b(?:storage|TSTG)\b",
    },
    "3.esd": {
        "hbm": r"\b(?:HBM|human.body)\b",
        "cdm": r"\b(?:CDM|charged.device)\b",
        "pin_scope": r"\b(?:pin|terminal|all pads)\b",
    },
    "4.supply": {"supply_range": r"\b(?:supply|VDD|VCC)\b", "numeric_limits": "@bounds"},
    "4.temperature": {
        "temperature_range": r"\b(?:temperature|TA|ambient)\b",
        "grade_scope": r"\b(?:grade|commercial|industrial|automotive)\b",
    },
    "4.logic": {
        "vih": r"\bVIH\b|input.high",
        "vil": r"\bVIL\b|input.low",
        "voh": r"\bVOH\b|output.high",
        "vol": r"\bVOL\b|output.low",
    },
    "5.rails": {
        "rails_pins": r"\b(?:rail|supply|VDD|VCC|pin)\b",
        "voltage_range": r"\b(?:voltage|VDD|VCC)\b",
    },
    "5.sequencing": {
        "power_up": r"\b(?:power.up|startup|start.up)\b",
        "power_down": r"\b(?:power.down|shutdown)\b",
        "ordering": r"\b(?:sequenc|before|after|order)\w*\b",
    },
    "5.decoupling": {
        "value": r"\b(?:decoupling|bypass|capacitor|capacitance)\b",
        "per_rail": r"\b(?:rail|pin|VDD|VCC)\b",
        "placement": r"\b(?:place|placement|close|near|distance)\b",
        "type": r"\b(?:ceramic|dielectric|X7R|X5R|type)\b",
    },
    "5.bulk_startup": {
        "bulk_capacitance": r"\b(?:bulk|reservoir|capacitance)\b",
        "inrush": r"\b(?:inrush|surge current)\b",
        "soft_start": r"\bsoft.start\b",
    },
    "6.equations": {"equation": "@equation", "stated_assumptions": r"\b(?:assum|where|given|divider)\w*\b"},
    "6.types": {
        "tolerance": r"\btolerance\b",
        "ratings": r"\b(?:rating|rated|power rating|voltage rating)\b",
        "dielectric": r"\b(?:dielectric|ceramic|X7R|X5R)\b",
        "esr": r"\bESR\b|equivalent.series.resistance",
    },
    "6.clock": {
        "clock_type": r"\b(?:crystal|oscillator|external clock|internal clock)\b",
        "frequency": r"\b(?:frequency|MHz|kHz|Hz)\b",
        "load_capacitance": r"\b(?:load capacitance|CL)\b",
        "esr": r"\bESR\b",
        "accuracy": r"\b(?:accuracy|ppm|tolerance)\b",
    },
    "7.dimensions": {
        "body_dimensions": r"\b(?:body|package).*(?:width|length|dimension)|\bdimension\b",
        "pitch": r"\bpitch\b",
        "height": r"\bheight\b",
        "tolerances": r"\b(?:tolerance|MIN.?MAX|minimum|maximum)\b",
    },
    "7.land_pattern": {
        "land_pattern": r"\b(?:land pattern|footprint)\b",
        "pad_dimensions": r"\bpad.*(?:dimension|width|length)\b",
        "stencil_relationship": r"\bstencil\b",
    },
    "7.orientation": {
        "pin_one": r"\bpin.?1\b",
        "view_direction": r"\b(?:top.view|bottom.view|orientation)\b",
        "package_identifier": r"\b(?:package identifier|package outline|package code)\b",
    },
    "8.routing": {
        "critical_loops": r"\b(?:critical loop|sensitive node)\b",
        "trace_geometry": r"\b(?:trace width|trace spacing|clearance)\b",
        "impedance": r"\b(?:controlled impedance|differential impedance|trace impedance)\b",
    },
    "8.ground": {
        "ground_plane": r"\bground plane\b",
        "return_paths": r"\breturn path\b",
        "thermal_vias": r"\bthermal via\w*\b",
    },
    "8.keepout": {
        "keepout_area": r"\bkeep.?out\b",
        "placement_rules": r"\b(?:placement|place.*(?:near|close)|component spacing)\b",
    },
    "9.current": {
        "quiescent": r"\b(?:quiescent|IQ)\b",
        "operating": r"\b(?:operating current|IDD|supply current)\b",
        "shutdown": r"\b(?:shutdown current|suspend current|standby current)\b",
    },
    "9.io": {
        "leakage": r"\bleakage\b",
        "capacitance": r"\bcapacitance\b",
        "drive": r"\b(?:drive|sink|source current|IOH|IOL)\b",
        "rise_fall": r"\b(?:rise|fall)\b",
    },
    "9.specific": {"device_characteristics": r"\b(?:characteristic|specification|impedance|threshold)\b"},
    "10.protocols": {
        "protocols_modes": r"\b(?:USB|UART|SPI|I2C|protocol|mode)\b",
        "speed": r"\b(?:baud|speed|Mbps|MHz)\b",
        "pins": r"\b(?:pin|TxD|RxD|SCLK|SDA)\b",
    },
    "10.address": {
        "address_format": r"\baddress\b",
        "configuration": r"\b(?:configur|strap|Self.powered|Bus.powered)\w*\b",
        "defaults": r"\b(?:default|by default)\b",
    },
    "10.levels": {
        "logic_compatibility": r"\b(?:logic|CMOS|TTL|compatible|level)\b",
        "loading": r"\b(?:loading|load capacitance|fan.out|bus capacitance)\b",
    },
    "11.metrics": {
        "theta_ja": r"(?:θ|theta)[ _-]*JA\b|junction.to.ambient",
        "theta_jc": r"(?:θ|theta)[ _-]*JC\b|junction.to.case",
        "theta_jb": r"(?:θ|theta)[ _-]*JB\b|junction.to.board",
        "psi_jt": r"(?:ψ|psi)[ _-]*JT\b",
        "test_board": r"\b(?:test board|layer PCB|JEDEC board|test PCB)\b",
    },
    "11.protection": {
        "thermal_shutdown": r"\bthermal.shutdown\b",
        "derating": r"\b(?:derating|limit.*power consumption)\b",
    },
    "11.layout": {"copper_area": r"\bcopper area\b", "thermal_vias": r"\bthermal via\w*\b"},
    "12.clocks": {
        "nominal_frequency": "@frequency",
        "frequency_limits": "@frequency_bounds",
        "baud_rates": r"\bbaud\b",
        "mode_scope": r"\b(?:mode|system clock|reference clock|SIE)\b",
    },
    "12.delays": {
        "setup": r"\bsetup\b",
        "hold": r"\bhold\b",
        "propagation": r"\b(?:propagation|delay)\b",
        "switching": r"\b(?:switching|rise time|fall time)\b",
    },
    "12.startup_reset": {"startup_time": r"\b(?:startup|start.up|power.up)\b", "reset_time": r"\breset\b"},
    "13.features": {"stated_mechanisms": r"\b(?:OVP|UVLO|OCP|protection|short.circuit|reverse.polarity)\b"},
    "13.behavior": {
        "thresholds": r"\b(?:threshold|trip|UVLO|OVP|OCP)\b",
        "latch_recovery": r"\b(?:latch|recover|retry|restart)\w*\b",
        "limitations": r"\b(?:protection.*limitation|limitation.*protection)\b",
    },
    "14.identity": {
        "manufacturer": r"\b(?:manufacturer|Cypress|Texas Instruments|Winbond|Infineon)\b",
        "selected_part": r"\b(?:part number|ordering code|orderable|device name)\b",
    },
    "14.grades": {
        "package_suffix": r"\b(?:package|suffix)\b",
        "temperature_grade": r"\b(?:temperature grade|commercial|industrial)\b",
        "qualification": r"\b(?:qualification|AEC|automotive|qualified)\b",
    },
    "14.revision_lifecycle": {
        "revision": r"\brevision\b",
        "lifecycle": r"\b(?:lifecycle|obsolete|active|NRND|end.of.life)\b",
    },
    "15.issues": {
        "issue": r"\b(?:issue|errata|limitation|not supported)\b",
        "workaround": r"\b(?:workaround|work.around)\b",
    },
    "15.footnotes": {
        "footnotes": r"\b(?:footnote|note|guaranteed)\b",
        "applicability": r"\b(?:applies|when|condition|scope)\b",
    },
    "15.revision": {
        "history": r"\b(?:revision|history)\b",
        "changes": r"\b(?:change|added|updated|revised|removed)\w*\b",
    },
    "16.inventory": {
        "graph_inventory": r"\b(?:graph|curve|characteristic plot)\b",
        "axes_units": r"\b(?:axis|axes|x.axis|y.axis|units)\b",
        "figure_reference": r"\bfigure\b",
    },
    "16.conditions": {
        "graph_conditions": r"\b(?:graph|curve|plot).*(?:condition|temperature|voltage)|\bcondition\b",
        "safe_operating_area": r"\b(?:safe operating area|SOA)\b",
    },
    "17.reflow": {
        "temperature": r"\b(?:reflow|solder).*temperature|\bpeak temperature\b",
        "profile_dwell": r"\b(?:profile|dwell|time at|duration|ramp)\b",
        "restrictions": r"\b(?:restriction|minimum|maximum|cycles|solderability)\b",
    },
    "17.assembly": {
        "msl": r"\b(?:MSL|moisture sensitivity)\b",
        "stencil": r"\bstencil\b",
        "assembly_notes": r"\b(?:assembly|bake|handling)\b",
    },
    "17.compliance": {
        "rohs": r"\bRoHS\b",
        "reach": r"\bREACH\b",
        "other_stated_compliance": r"\b(?:compliance|compliant|Pb.free|lead.free)\b",
    },
}


def quality_instructions(section_id: int) -> str:
    if section_id not in SECTIONS:
        raise ValueError("Section ID must be 1 through 17")
    checklist = "; ".join(f"{key}: {', '.join(_FIELDS[key])}" for key in requirements(section_id))
    return _COMMON + "\nSECTION POLICY: " + _SECTION_POLICY[section_id] + "\nSUBFIELD CHECKLIST: " + checklist


def _content(claim: Claim, *, conditions=False) -> str:
    values = [claim.parameter, claim.raw_value or "", claim.value or ""]
    if conditions:
        values.extend(claim.conditions)
    return " ".join(values)


def _matches(claim: Claim, pattern: str, field_name: str = "") -> bool:
    context_fields = {
        "package_scope",
        "mode_scope",
        "test_board",
        "pin_scope",
        "grade_scope",
        "stated_assumptions",
        "applicability",
        "graph_conditions",
    }
    text = _content(claim, conditions=field_name in context_fields)
    if pattern == "@bounds":
        return claim.quantity is not None and any(
            v is not None for v in [claim.quantity.minimum, claim.quantity.maximum]
        )
    if pattern == "@equation":
        return (
            claim.kind == "equation"
            or claim.quantity is not None
            and bool(claim.quantity.expression and "=" in claim.quantity.expression)
        )
    if pattern in {"@frequency", "@frequency_bounds"}:
        is_frequency = bool(
            re.search(r"\b(?:clock|oscillator|frequency|MHz|kHz|Hz)\b", _content(claim), re.IGNORECASE)
        )
        return is_frequency and (pattern == "@frequency" or _matches(claim, "@bounds"))
    return bool(re.search(pattern, text, re.IGNORECASE))


def requirement_coverage(section: SectionResult) -> list[dict]:
    """Report claimed subfield coverage without inventing document-level absence."""
    declared = {finding.requirement_id: finding.status.value for finding in section.findings}
    result = []
    for requirement_id in requirements(section.section_id):
        own = [claim for claim in section.claims if claim.requirement_id == requirement_id]
        fields = []
        related = set()
        for name, pattern in _FIELDS[requirement_id].items():
            matched = [claim for claim in own if _matches(claim, pattern, name)]
            present = [claim for claim in matched if claim.status == "present" and claim.evidence_ids]
            reported_absent = [
                claim for claim in matched if claim.status == "not_found" and claim.explanation
            ]
            state = "present" if present else "reported_absent" if reported_absent else "check_needed"
            fields.append(
                {
                    "field": name,
                    "status": state,
                    "claim_ids": [claim.id for claim in matched],
                    "evidence_ids": sorted({e for claim in present for e in claim.evidence_ids}),
                }
            )
            related.update(
                claim.id
                for claim in section.claims
                if claim.requirement_id != requirement_id
                and claim.status == "present"
                and claim.evidence_ids
                and _matches(claim, pattern, name)
            )
        states = {row["status"] for row in fields}
        status = "present" if states == {"present"} else "partial" if "present" in states else "check_needed"
        if declared.get(requirement_id) in {"not_found", "not_applicable"} and "present" not in states:
            status = (
                "reported_absent" if declared[requirement_id] == "not_found" else "reported_not_applicable"
            )
        result.append(
            {
                "requirement_id": requirement_id,
                "declared_status": declared.get(requirement_id),
                "coverage": status,
                "fields": fields,
                "related_claim_ids": sorted(related),
                "basis": "extracted_claims_not_verified_source_completeness",
            }
        )
    return result


def ownership_diagnostics(section: SectionResult) -> list[dict]:
    """Narrow guards: retain the original fact and flag its inappropriate owner."""
    result = []
    for claim in section.claims:
        if claim.status != "present":
            continue
        owner, suggested = claim.requirement_id, None
        parameter = claim.parameter
        text = _content(claim)
        thermal = bool(
            re.search(r"(?:θ|theta|ψ|psi)[ _-]*(?:JA|JC|JB|JT)\b|thermal impedance", parameter, re.IGNORECASE)
        )
        thermal = thermal or bool(
            claim.quantity and claim.quantity.unit and re.search(r"(?:°?C|K)\s*/\s*W", claim.quantity.unit)
        )
        reflow = bool(re.search(r"\b(?:reflow|solder.*(?:peak|profile|dwell))\b", parameter, re.IGNORECASE))
        if thermal and owner in {"6.types", "7.dimensions", "8.ground"}:
            suggested = "11.metrics"
        elif reflow and owner in {"6.types", "7.dimensions", "11.metrics"}:
            suggested = "17.reflow"
        elif owner == "13.behavior" and re.fullmatch(
            r"(?:VDD|VCC)?\s*supply voltage(?: range)?", parameter, re.IGNORECASE
        ):
            if not re.search(
                r"\b(?:OVP|UVLO|OCP|threshold|trip|protection|latch|recover)\w*\b", text, re.IGNORECASE
            ):
                suggested = "4.supply"
        elif owner in {"8.routing", "8.ground"}:
            layout_rule = re.search(
                r"\b(?:trace|route|routing|plane|return path|layout|placement|keep.?out|via)\w*\b",
                text,
                re.IGNORECASE,
            )
            connection = re.search(
                r"\b(?:connection|connected|connects|ground symbol|voltage divider|regulator to supply)\b",
                text,
                re.IGNORECASE,
            )
            if connection and not layout_rule:
                suggested = "2.connections"
        elif (
            owner == "8.keepout"
            and re.search(
                r"\b(?:package|body|lead|pin.1).*(?:outline|width|length|height|pitch|diameter|dimension)|\blead (?:pitch|angle)\b",
                parameter,
                re.IGNORECASE,
            )
            and not re.search(r"\b(?:keep.?out|placement|component spacing)\b", text, re.IGNORECASE)
        ):
            suggested = "7.dimensions"
        if suggested:
            result.append(
                {
                    "code": "semantic_requirement_mismatch",
                    "severity": "warning",
                    "section_id": section.section_id,
                    "claim_id": claim.id,
                    "requirement_id": owner,
                    "suggested_requirement_id": suggested,
                    "evidence_ids": list(claim.evidence_ids),
                    "message": f"{claim.parameter}: related fact does not establish {owner}; source review should consider {suggested}",
                    "action": "guard_only_original_claim_preserved",
                }
            )
    return result


@dataclass
class SemanticPolicyResult:
    sections: list[SectionResult]
    diagnostics: list[dict] = field(default_factory=list)
    coverage: list[dict] = field(default_factory=list)
    provenance: list[dict] = field(default_factory=list)


def semantic_postprocess(sections: list[SectionResult]) -> SemanticPolicyResult:
    """Guard-only reconciliation; destinations never become implicitly reviewed."""
    return SemanticPolicyResult(
        sections=[section.model_copy(deep=True) for section in sections],
        diagnostics=[issue for section in sections for issue in ownership_diagnostics(section)],
        coverage=[row for section in sections for row in requirement_coverage(section)],
        provenance=[
            {
                "policy_version": QUALITY_POLICY_VERSION,
                "action": "semantic_guards_only_no_fact_values_or_ownership_changed",
            }
        ],
    )
