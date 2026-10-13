"""Source-scoped AMS1117 regression checks; never an aggregate accuracy score."""
import argparse
import json
import re
from pathlib import Path


def normalize(text):
    return str(text).replace("μ","µ").replace("℃","°C").replace(" ","").lower()


def check(result,probes):
    rows=[]
    for probe in probes["numeric_probes"]:
        section=next(s for s in result["sections"] if s["section_id"]==probe["section"])
        candidates=[]
        for c in section["claims"]:
            if c["status"] not in {"present","ambiguous","conflicting"}:
                continue
            if not re.search(probe["parameter"],c["parameter"],re.IGNORECASE):
                continue
            if not any(f":p{probe['page']}:" in e or e.endswith(f":p{probe['page']}") for e in c["evidence_ids"]):
                continue
            q=c.get("quantity") or {}
            text=" ".join(str(v or "") for v in (c.get("raw_value"),c.get("value"),q.get("expression")))
            numbers=[float(v) for v in re.findall(r"(?<![A-Za-z0-9])[-+]?\d+(?:\.\d+)?",text)]
            values=[q.get(k) for k in ("minimum","typical","maximum")]
            for center,tolerance in re.findall(r"(\d+(?:\.\d+)?)\s*±\s*(\d+(?:\.\d+)?)",text):
                values.extend([float(center)-float(tolerance),float(center)+float(tolerance)])
            if not any(v is not None and abs(v-probe["value"])<1e-8 for v in values+numbers):
                continue
            if normalize(probe["unit"]) not in normalize(str(q.get("unit") or "")+text):
                continue
            if probe.get("condition") and not re.search(probe["condition"]," ".join(c["conditions"])+" "+text,re.IGNORECASE):
                continue
            candidates.append(c["id"])
        rows.append({"id":probe["id"],"source_page":probe["page"],"status":"located" if candidates else "manual_check",
                     "claim_ids":candidates})
    return rows


def compare(before,after,probes):
    left,right=check(before,probes),check(after,probes)
    same_package=before["target"]["package_name"]==after["target"]["package_name"]
    package_probes={"sot89_thermal","sot89_body_width_low","sot89_body_width_high","sot89_outer_pitch"}
    package_probes.update(p["id"] for p in probes["numeric_probes"] if p.get("package_specific"))
    return {"version":"ams1117-runtime-comparison-1","probe_scope":probes["scope"],
        "target_alignment":{"same_package":same_package,"before":before["target"],"after":after["target"]},
        "before_run":before["run_id"],"after_run":after["run_id"],
        "numeric_probes":[{**r,"before":l["status"],"after":r["status"],
            "change":"not_comparable_package" if not same_package and r["id"] in package_probes else
                     "regression_candidate" if l["status"]=="located" and r["status"]!="located" else
                     "improved_candidate" if l["status"]!="located" and r["status"]=="located" else "unchanged"}
                         for l,r in zip(left,right,strict=True)],
        "manual_probes":probes["manual_probes"],
        "limits":["Regex/number presence does not certify numeric role, qualifiers or every circuit edge.",
                  "Manual_check can mean naming/representation mismatch; inspect cited source and output.",
                  "These probes are not a complete gold standard and cannot prove universal no-regression."]}


def main():
    p=argparse.ArgumentParser()
    for key in ("before","after","probes","output"):
        p.add_argument("--"+key,type=Path,required=True)
    args=p.parse_args()
    read=lambda path: json.loads(path.read_text(encoding="utf-8"))
    report=compare(read(args.before),read(args.after),read(args.probes))
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"changes":{key:sum(r["change"]==key for r in report["numeric_probes"])
                      for key in ("unchanged","improved_candidate","regression_candidate","not_comparable_package")}}))


if __name__=="__main__":
    main()
