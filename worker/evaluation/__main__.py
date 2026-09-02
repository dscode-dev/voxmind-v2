"""CLI: run the evaluation dataset and write a report."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from evaluation.report import compare, run_evaluation, summarize_dataset, write_report


def main() -> int:
    parser = argparse.ArgumentParser(prog="evaluation")
    parser.add_argument("--dataset", default="voxmind")
    parser.add_argument("--out", type=Path, help="write the report to this path")
    parser.add_argument("--summary", action="store_true", help="print the dataset summary only")
    parser.add_argument(
        "--compare",
        nargs=2,
        type=Path,
        metavar=("BEFORE", "AFTER"),
        help="print a before/after comparison table",
    )
    args = parser.parse_args()

    if args.compare:
        before = json.loads(args.compare[0].read_text(encoding="utf-8"))
        after = json.loads(args.compare[1].read_text(encoding="utf-8"))
        rows = compare(before, after)
        width = max(len(r["metric"]) for r in rows)
        print(f"{'metric'.ljust(width)}  {'before':>12}  {'after':>12}  {'delta':>8}")
        for row in rows:
            if isinstance(row["before"], list) or isinstance(row["after"], list):
                continue
            delta = "" if row["delta"] is None else f"{row['delta']:+g}"
            print(
                f"{str(row['metric']).ljust(width)}  {str(row['before']):>12}  "
                f"{str(row['after']):>12}  {delta:>8}"
            )
        return 0

    if args.summary:
        print(json.dumps(summarize_dataset(args.dataset), indent=2))
        return 0

    report = run_evaluation(args.dataset)
    totals = report["totals"]
    print(json.dumps({"dataset": report["dataset_summary"], "totals": totals}, indent=2))

    if args.out:
        write_report(report, args.out)
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
