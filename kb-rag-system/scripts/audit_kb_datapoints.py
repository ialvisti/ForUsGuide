#!/usr/bin/env python3
"""
KB data-point ↔ ForusBots catalog alignment audit.

Inventories every data point under details.required_data.{must_have,nice_to_have}
in the KB article JSONs (PA/**/*.json) and checks whether each one:
  - resolves deterministically to a ForusBots (module, field) via map_slug,
  - is provided by the handle-ticket request itself, or
  - is ticket/agent-sourced (handled by the ticket-extraction LLM layer).

Usage:
    python scripts/audit_kb_datapoints.py [--articles-dir ../PA]

Exit code 1 when a must_have participant_profile data point neither maps nor is
request-provided (a true alignment gap).
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data_pipeline.forusbots_catalog import is_request_provided, map_slug  # noqa: E402

CURRENT_YEAR_FOR_AUDIT = 2026  # only affects payroll token rendering in output


def slugify(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", str(s or "").lower())
    return re.sub(r"[\s\-]+", "_", s).strip("_")


def iter_data_points(articles_dir: str):
    for path in sorted(glob.glob(os.path.join(articles_dir, "**", "*.json"), recursive=True)):
        if "Tags" in path:
            continue
        try:
            doc = json.load(open(path))
        except Exception as e:  # noqa: BLE001
            print(f"  !! parse error {path}: {e}", file=sys.stderr)
            continue
        rd = (doc.get("details") or {}).get("required_data") or {}
        for tier in ("must_have", "nice_to_have"):
            for dp in rd.get(tier) or []:
                yield os.path.basename(path), tier, dp


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--articles-dir", default=os.path.join(
        os.path.dirname(__file__), "..", "..", "PA"))
    args = parser.parse_args()

    if not os.path.isdir(args.articles_dir):
        print(f"articles dir not found: {args.articles_dir}", file=sys.stderr)
        return 2

    rows = []
    for fname, tier, dp in iter_data_points(args.articles_dir):
        slug = slugify(dp.get("data_point"))
        item = {"field": slug, "description": dp.get("meaning"),
                "why_needed": dp.get("why_needed")}
        if is_request_provided(item):
            status = "request_provided"
            target = "-"
        else:
            mapped = map_slug(item, current_year=CURRENT_YEAR_FOR_AUDIT)
            if mapped:
                status = "deterministic"
                target = "; ".join(f"{m}/{f}" for m, f in mapped)
            else:
                status = "unmapped"
                target = "-"
        rows.append({
            "article": fname, "tier": tier, "slug": slug,
            "source_type": dp.get("source_type"), "status": status, "target": target,
        })

    print(f"TOTAL data points: {len(rows)}\n")
    print("=== tier x source_type ===")
    for (tier, st), n in sorted(Counter((r["tier"], r["source_type"]) for r in rows).items()):
        print(f"  {tier:<13} {str(st):<22} {n}")
    print("\n=== status ===")
    for status, n in sorted(Counter(r["status"] for r in rows).items()):
        print(f"  {status:<18} {n}")

    gaps = [r for r in rows
            if r["tier"] == "must_have"
            and r["source_type"] == "participant_profile"
            and r["status"] == "unmapped"]
    print("\n=== ALIGNMENT GAPS (must_have + participant_profile + unmapped) ===")
    if not gaps:
        print("  none ✓")
    for r in gaps:
        print(f"  {r['slug']:<48} | {r['article'][:48]}")

    print("\n=== ticket/agent-sourced (handled by ticket-extraction layer) ===")
    for r in rows:
        if r["source_type"] in ("message_text", "agent_input", "participant_action") \
                and r["status"] == "unmapped":
            print(f"  [{r['tier']:<12}] [{str(r['source_type']):<18}] {r['slug'][:60]}")

    return 1 if gaps else 0


if __name__ == "__main__":
    sys.exit(main())
