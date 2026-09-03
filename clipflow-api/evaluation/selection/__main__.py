"""CLI: run the selection evaluation and print the comparison."""
from __future__ import annotations

import argparse
from pathlib import Path

from evaluation.selection.report import compare, run_evaluation, write_report


def main() -> int:
    parser = argparse.ArgumentParser(prog="evaluation.selection")
    parser.add_argument("--limit", type=int, default=3, help="max selected per run")
    parser.add_argument("--out", type=Path, help="write the full report here")
    parser.add_argument("--ranking", action="store_true", help="print the full ranking")
    args = parser.parse_args()

    report = run_evaluation(limit=args.limit)

    print(f"Selection evaluation ({report['dataset']['cases']} synthetic cases, "
          f"topic '{report['dataset']['topic']}')")
    print(report["note"])
    print()

    engine = report["engine"]
    print(f"considered={engine['considered']}  eligible={engine['eligible']}  "
          f"ineligible={engine['ineligible']}  "
          f"semantic={engine['semantic_provider']} "
          f"(evaluated={engine['semantic_evaluated']}, failures={engine['semantic_failures']})")
    print()

    rows = compare(report)
    width = max(len(row["metric"]) for row in rows)
    print(f"{'metric'.ljust(width)}  {'recency_only':>14}  {'selection_v1':>14}  {'delta':>7}")
    for row in rows:
        delta = "" if row["delta"] is None else f"{row['delta']:+g}"
        print(f"{row['metric'].ljust(width)}  {str(row['recency_only']):>14}  "
              f"{str(row['selection_v1']):>14}  {delta:>7}")

    print()
    print(f"baseline picked:     {report['baseline']['selected_ids']}")
    print(f"selection-v1 picked: {report['selection_v1']['selected_ids']}")
    print()
    after = report["selection_v1"]
    print(f"score spread: {after['score_min']} .. {after['score_max']} "
          f"(range {after['score_spread']}, {after['distinct_scores']} distinct)")

    if args.ranking:
        print()
        print("full ranking:")
        for item in report["ranking"]:
            print(f"  {item['rank']:>2}. {item['score']:.4f}  {item['candidate_id']:26} "
                  f"{item['decision']:9} {','.join(item['reasons'] or item['blocked_by'])}")
        print()
        print("ineligible:")
        for item in report["ineligible"]:
            mark = "permanent" if item["permanent"] else "temporary"
            print(f"      {item['candidate_id']:26} {mark:10} {','.join(item['reasons'])}")

    if args.out:
        write_report(report, args.out)
        print()
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
