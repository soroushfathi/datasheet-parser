"""Deterministic critical-path ordering for independent source-read stages."""

from dataclasses import dataclass

# Circuit, ordering, pins, power and mechanical sections historically have the longest reads.
SECTION_PRIORITY = (2, 14, 1, 3, 7, 15, 16, 4, 12, 5, 6, 8, 9, 10, 11, 13, 17)


@dataclass(frozen=True)
class Stage:
    section_id: int
    role: str


def stage_order(stage: Stage):
    """Launch long chains early; finish an already-ready review before lower-priority chains."""
    return SECTION_PRIORITY.index(stage.section_id), {"extract": 0, "blind": 1, "review": 2}[stage.role]
