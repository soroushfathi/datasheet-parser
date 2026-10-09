"""Explicit coverage checklist. Data absence and processing failure are distinct."""

SECTIONS = {
    1: (
        "pinout",
        "pins",
        {
            "pads": "Every physical pad including exposed pad, names, numbering and package applicability",
            "functions": "Electrical types, alternate functions and mode-dependent behavior",
            "connections": "Must-not-float pins, pulls, unused pins and exposed pad connection",
        },
    ),
    2: (
        "netlist",
        "circuit",
        {
            "circuits": "Identify existing application schematics, distinguish block/internal/test diagrams",
            "components": "External component references, values, terminals and alternatives",
            "connections": "Only observed schematic connectivity, explicit NC versus unobserved pins",
        },
    ),
    3: (
        "absolute_maximum_ratings",
        "specifications",
        {
            "voltages": "Absolute maximum supply and pin voltages, including symbolic limits",
            "currents_power": "Absolute maximum currents and power dissipation",
            "temperatures": "Absolute junction, case and storage temperature limits",
            "esd": "HBM and CDM ESD limits with applicable pin groups and test conditions",
        },
    ),
    4: (
        "recommended_operating_conditions",
        "specifications",
        {
            "supply": "Recommended supply min/typ/max and conditions",
            "temperature": "Recommended operating temperature for selected grade",
            "logic": "VIH VIL VOH VOL and conditions; separate absolute limits",
        },
    ),
    5: (
        "power_requirements",
        "application",
        {
            "rails": "Power rails, pins and operating voltage ranges",
            "sequencing": "Power-up, power-down and sequencing requirements",
            "decoupling": "Decoupling by power pin, values, type and placement",
            "bulk_startup": "Bulk capacitance, inrush and soft-start",
        },
    ),
    6: (
        "external_component_selection",
        "application",
        {
            "equations": "Component calculation equations and stated assumptions",
            "types": "Tolerance, ratings, dielectric, ESR and other type requirements",
            "clock": "Crystal or oscillator frequency, load capacitance, ESR and accuracy",
        },
    ),
    7: (
        "footprint_package",
        "mechanical",
        {
            "dimensions": "Selected package dimensions, pitch, height and tolerances",
            "land_pattern": "Recommended land pattern, pad dimensions and stencil relationship",
            "orientation": "Pin 1 marking, top/bottom view and package identifiers",
        },
    ),
    8: (
        "layout_guidelines",
        "application",
        {
            "routing": "Critical loops, sensitive nodes, trace width, spacing and impedance",
            "ground": "Ground plane, return paths and thermal vias",
            "keepout": "Keep-out areas and component placement",
        },
    ),
    9: (
        "electrical_characteristics",
        "specifications",
        {
            "current": "Quiescent, operating and shutdown currents with conditions",
            "io": "Input leakage/capacitance, output drive and rise/fall times",
            "specific": "All applicable part-specific electrical characteristics and test conditions",
        },
    ),
    10: (
        "communication_interfaces",
        "pins",
        {
            "protocols": "Protocols, modes, clock or bus speed and applicable pins",
            "address": "Address format, address/configuration pins and defaults",
            "levels": "Logic compatibility and bus loading/capacitance restrictions",
        },
    ),
    11: (
        "thermal",
        "specifications",
        {
            "metrics": "Theta JA/JC/top/bottom, psi JT, test board and package conditions",
            "protection": "Thermal shutdown and derating",
            "layout": "Copper area and thermal via recommendations",
        },
    ),
    12: (
        "timing",
        "specifications",
        {
            "clocks": "Clock frequency limits by mode",
            "delays": "Setup, hold, propagation and switching delays with conditions",
            "startup_reset": "Startup and reset timing",
        },
    ),
    13: (
        "protection",
        "specifications",
        {
            "features": "Protection types actually stated: OVP UVLO OCP short circuit reverse polarity etc",
            "behavior": "Protection thresholds, latch-up, recovery and limitations",
        },
    ),
    14: (
        "part_identification",
        "identity",
        {
            "identity": "Manufacturer, selected part and ordering codes",
            "grades": "Package suffixes, temperature grade and qualification",
            "revision_lifecycle": "Document revision and lifecycle as stated in this document, not current web status",
        },
    ),
    15: (
        "errata_notes",
        "notes",
        {
            "issues": "Known issues and workarounds actually stated",
            "footnotes": "Specification footnotes and scope of applicability",
            "revision": "Revision history and stated changes",
        },
    ),
    16: (
        "performance_graphs",
        "notes",
        {
            "inventory": "Characteristic graphs, labels, axes, units and figure references",
            "conditions": "Graph conditions and safe operating area; no invented digitized points",
        },
    ),
    17: (
        "soldering_assembly",
        "mechanical",
        {
            "reflow": "Reflow temperature/profile and soldering restrictions",
            "assembly": "MSL, stencil and assembly notes for selected ordering/package",
            "compliance": "RoHS REACH and other compliance actually stated",
        },
    ),
}


def requirements(section_id: int | None = None) -> dict[str, str]:
    return {
        f"{sid}.{key}": text
        for sid, (_, _, entries) in SECTIONS.items()
        if section_id is None or sid == section_id
        for key, text in entries.items()
    }


KEYWORDS = {
    1: ["pin", "pad", "strap"],
    2: ["application", "schematic", "circuit"],
    3: ["absolute", "esd"],
    4: ["recommended", "operating conditions", "logic"],
    5: ["power", "supply", "decoupl", "capacitor"],
    6: ["crystal", "oscillator", "component", "capacitor"],
    7: ["package", "dimension", "land pattern", "mechanical"],
    8: ["layout", "routing", "ground plane"],
    9: ["electrical", "characteristics"],
    10: ["interface", "protocol", "spi", "mii", "rs232", "rs-232"],
    11: ["thermal", "junction", "derating"],
    12: ["timing", "delay", "clock", "reset"],
    13: ["protection", "latch", "shutdown"],
    14: ["order", "device information", "revision"],
    15: ["revision", "errata", "note"],
    16: ["curve", "graph", "waveform"],
    17: ["solder", "reflow", "msl", "rohs", "reach", "stencil"],
}
