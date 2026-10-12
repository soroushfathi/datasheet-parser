"""Build an auditable CY7C64225 before/after report without model requests."""

import argparse
import json
from pathlib import Path

from datasheet_parser.cy_quality_comparison import compare_files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, default=Path("outputs/CY7C64225-28PVXC-before-quality.json"))
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=Path("work/cy-audit/CY7C64225.pdf"))
    parser.add_argument("--output", type=Path, default=Path("outputs/CY7C64225-quality-comparison.json"))
    args = parser.parse_args()
    report = compare_files(args.before, args.after, args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.output.resolve())
    for probe in report["source_audited_data_probes"]:
        print(f"{probe['probe']}: {probe['status']} ({probe['measured_change']})")


if __name__ == "__main__":
    main()
