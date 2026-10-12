"""Recover source-backed PDF glyphs without inferring mathematical conditions.

Some producers paint an ASCII inequality plus a separate vector bar instead of
encoding a Unicode operator.  Others retain Microsoft Symbol private-use codes
in their ToUnicode map.  Repair characters before every extraction view, while
keeping the original characters, coordinates, and an auditable repair record.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pdfminer.pdftypes import resolve1
from pdfplumber.page import DerivedPage

TEXT_REPAIR_VERSION = "pdf-glyphs-1"
# Microsoft Symbol's F000 offset preserves its original Symbol encoding byte.
# Only these engineering glyphs, in positively identified Symbol fonts, are used.
SYMBOL_PRIVATE_USE = {"\uf057": "\u03a9", "\uf06d": "\u03bc", "\uf071": "\u03b8"}


def _bbox(obj: dict[str, Any]) -> list[float]:
    return [obj[k] for k in ("x0", "top", "x1", "bottom")]


def _black(color: Any) -> bool:
    # Gray, RGB and CMYK black. Unknown color spaces are deliberately rejected.
    if isinstance(color, (int, float)):
        return color == 0
    return color in ((0, 0, 0), [0, 0, 0], (0, 0, 0, 1), [0, 0, 0, 1])


def _operator_bar(char: dict[str, Any], rectangles: Iterable[dict[str, Any]]) -> dict | None:
    """Match a separate, glyph-width black bar at the bottom of this operator.

    A long underline/table border, a neighboring glyph's bar, a white fill, or
    a bar outside the character's lower quarter is insufficient evidence.  A
    match recovers the actual painted glyph; context such as temperature never
    participates in this decision.
    """
    width = char["x1"] - char["x0"]
    height = char["bottom"] - char["top"]
    if width <= 0 or height <= 0 or not char.get("upright", True):
        return None
    tolerance = max(0.08, min(0.35, width * 0.07))
    matches = []
    for rect in rectangles:
        thickness = rect["bottom"] - rect["top"]
        if (
            rect.get("fill") is True
            and _black(rect.get("non_stroking_color"))
            and abs(rect["x0"] - char["x0"]) <= tolerance
            and abs(rect["x1"] - char["x1"]) <= tolerance
            and height * 0.02 <= thickness <= height * 0.12
            and char["top"] + height * 0.78 <= rect["top"]
            and rect["bottom"] <= char["bottom"] + height * 0.02
        ):
            matches.append(rect)
    # Multiple differently placed marks do not prove one composite operator.
    if len(matches) != 1:
        return None
    return matches[0]


def repair_chars(
    chars: Iterable[dict[str, Any]],
    rectangles: Iterable[dict[str, Any]],
    *,
    symbol_fonts: Iterable[str] = (),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return copied characters, proven repairs, and unresolved encoding records."""
    rects = tuple(rectangles)
    fonts = frozenset(symbol_fonts)
    copied, repairs, unresolved = [], [], []
    for char in chars:
        result = dict(char)
        source = char["text"]
        proof = None
        replacement = source
        if source in ("<", ">") and (bar := _operator_bar(char, rects)) is not None:
            replacement = "\u2264" if source == "<" else "\u2265"
            proof = {"method": "operator_plus_vector_bar", "bar_bbox": _bbox(bar)}
        elif source in SYMBOL_PRIVATE_USE and char.get("fontname") in fonts:
            replacement = SYMBOL_PRIVATE_USE[source]
            proof = {
                "method": "symbol_font_private_use",
                "encoding": "Microsoft Symbol F000+byte",
                "source_codepoint": f"U+{ord(source):04X}",
            }
        if proof:
            result["text"] = replacement
            repairs.append(
                {
                    "raw": source,
                    "replacement": replacement,
                    "bbox": _bbox(char),
                    "fontname": char.get("fontname"),
                    "status": "source_backed",
                    **proof,
                }
            )
        elif source.startswith("(cid:") or any(
            0xE000 <= ord(c) <= 0xF8FF or ord(c) in (0xFFFD, 0xFFFE) for c in source
        ):
            unresolved.append(
                {"raw": source, "bbox": _bbox(char), "fontname": char.get("fontname")}
            )
        copied.append(result)
    return copied, repairs, unresolved


def _symbol_fonts(page) -> set[str]:
    """Require the PDF's actual font resource identity, not surrounding prose."""
    known = set()
    visited = set()

    def resource_fonts(resources):
        resources = resolve1(resources)
        if not isinstance(resources, dict) or id(resources) in visited:
            return
        visited.add(id(resources))
        for font in resolve1(resources.get("Font", {})).values():
            font = resolve1(font)
            if not isinstance(font, dict):
                continue
            candidates = [font, *map(resolve1, font.get("DescendantFonts", []))]
            for candidate in candidates:
                descriptor = resolve1(candidate.get("FontDescriptor", {}))
                for name in (candidate.get("BaseFont"), descriptor.get("FontName")):
                    name = getattr(name, "name", "")
                    if name.split("+")[-1] in {"Symbol", "SymbolMT"}:
                        known.add(name)
        for obj in resolve1(resources.get("XObject", {})).values():
            obj = resolve1(obj)
            if hasattr(obj, "attrs") and "Resources" in obj.attrs:
                resource_fonts(obj.attrs["Resources"])

    resource_fonts(page.page_obj.resources)
    return known


class _RepairedPage(DerivedPage):
    def __init__(self, parent_page, chars):
        self.bbox = parent_page.bbox
        super().__init__(parent_page)
        self._objects = {**parent_page.objects, "char": chars}

    @property
    def objects(self):
        return self._objects


def prepare_page(page):
    """Return a page view with identical geometry and consistent repaired text."""
    chars, repairs, unresolved = repair_chars(page.chars, page.rects, symbol_fonts=_symbol_fonts(page))
    corrected = _RepairedPage(page, chars) if repairs else page
    return corrected, {
        "version": TEXT_REPAIR_VERSION,
        "status": "unresolved" if unresolved else "repaired" if repairs else "unchanged",
        "repairs": repairs,
        "unresolved_glyphs": unresolved,
    }


def repair_metadata(metadata: dict[str, Any], bbox: list[float]) -> dict[str, Any]:
    """Attach only repair/encoding records whose glyph center belongs to a region."""
    def within(record):
        x0, top, x1, bottom = record["bbox"]
        return bbox[0] <= (x0 + x1) / 2 <= bbox[2] and bbox[1] <= (top + bottom) / 2 <= bbox[3]

    repairs = [r for r in metadata["repairs"] if within(r)]
    unresolved = [r for r in metadata["unresolved_glyphs"] if within(r)]
    return {
        "version": metadata["version"],
        "status": "unresolved" if unresolved else "repaired" if repairs else "unchanged",
        "repairs": repairs,
        "unresolved_glyphs": unresolved,
    }
