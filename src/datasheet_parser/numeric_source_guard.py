"""Necessary numeric-literal checks; presence never certifies row/role correctness."""

import math
import re

from .models import Status

NUMBER=r"[-+]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?"
UNIT=r"[GkMmunpµ]?(?:Hz|Ω|A|V|F|W|s|m)"
SCALE={"":1,"G":1e9,"M":1e6,"k":1e3,"m":1e-3,"u":1e-6,"µ":1e-6,"n":1e-9,"p":1e-12}


def unit_parts(unit):
    match=re.fullmatch(r"([GkMmunpµ]?)(Hz|Ω|A|V|F|W|s|m)",str(unit or "").replace("μ","µ").strip())
    return (match[2],SCALE[match[1]]) if match else None


def converted_source_numbers(text, unit):
    """Exact prefix scaling of explicit source literals, never formula evaluation."""
    target=unit_parts(unit)
    if not target:
        return set()
    values=set()
    for number, source_unit in re.findall(rf"(?<![\w.])({NUMBER})\s*({UNIT})(?![A-Za-zµμΩ])",
                                         text.replace("μ","µ").replace("−","-")):
        source=unit_parts(source_unit)
        if source and source[0]==target[0]:
            values.add(float(number)*source[1]/target[1])
    return values


def source_numbers(text):
    text=text.replace("−","-")
    values={float(n) for n in re.findall(rf"(?<![\d.])({NUMBER})(?![\d.])",text)}
    for center,tolerance in re.findall(rf"({NUMBER})\s*±\s*({NUMBER})",text):
        values.update({float(center)-float(tolerance),float(center)+float(tolerance)})
    return values


def guard_numeric_sources(sections,regions,allowed_by_section):
    by_id={r["id"]:r for r in regions}
    audit=[]
    for section in sections:
        changed=set()
        for claim in section.claims:
            if not claim.quantity or not claim.evidence_ids:
                continue
            tables=[by_id[rid] for rid in claim.evidence_ids if rid in by_id
                    and rid in allowed_by_section[section.section_id] and by_id[rid].get("kind")=="table"]
            numeric_cells=sum(bool(re.fullmatch(rf"\s*{NUMBER}(?:\s*(?:/|~|±|\s)\s*{NUMBER})*\s*",str(cell)))
                              for table in tables for row in table.get("rows",[]) for cell in row if cell is not None)
            if numeric_cells < 3:
                # Raster drawings and image-only tables cannot be adjudicated by
                # text membership. Their visual readings remain untouched.
                continue
            texts=[str(by_id[rid].get("text", "")) for rid in claim.evidence_ids
                   if rid in by_id and rid in allowed_by_section[section.section_id]]
            if not texts:
                continue
            text="\n".join(texts)
            values=source_numbers(text) | converted_source_numbers(text,claim.quantity.unit)
            # Expressions remain source data; no inferred formula evaluations.
            unsupported={role:value for role in ("minimum","typical","maximum")
                         if (value:=getattr(claim.quantity,role)) is not None
                         and not any(math.isclose(value,n,rel_tol=1e-10,abs_tol=1e-10) for n in values)}
            if not unsupported:
                continue
            original=claim.model_dump(mode="json")
            for role in unsupported:
                setattr(claim.quantity,role,None)
            claim.status=Status.AMBIGUOUS
            claim.raw_value=None
            claim.value=None
            claim.explanation += " Numeric bounds not confirmed in cited source were withheld; model draft remains in numeric audit."
            changed.add(claim.requirement_id)
            audit.append({"claim_id":claim.id,"section_id":section.section_id,
                "unsupported_bounds":unsupported,"original_model_claim":original,
                "source_region_ids":claim.evidence_ids,"code":"source_numeric_not_confirmed",
                "severity":"warning","limit":"Literal membership is necessary, not a source-row or numeric-role certificate."})
        for finding in section.findings:
            if finding.requirement_id in changed:
                finding.status=Status.AMBIGUOUS
                finding.reason += " Some proposed numeric bounds could not be confirmed in cited source."
        if changed:
            section.status=Status.AMBIGUOUS
    return audit
