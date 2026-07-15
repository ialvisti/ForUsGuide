#!/usr/bin/env python3
"""Fail CI when detect-secrets adds a finding to the reviewed baseline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _findings(document: dict[str, Any]) -> set[tuple[str, str, str]]:
    findings: set[tuple[str, str, str]] = set()
    for path, entries in (document.get("results") or {}).items():
        for entry in entries or []:
            findings.add((
                str(path),
                str(entry.get("type") or ""),
                str(entry.get("hashed_secret") or ""),
            ))
    return findings


def verify_baseline(approved: dict[str, Any], candidate: dict[str, Any]) -> None:
    for field in ("version", "plugins_used", "filters_used"):
        if candidate.get(field) != approved.get(field):
            raise ValueError(f"detect-secrets configuration changed: {field}")

    additions = sorted(_findings(candidate) - _findings(approved))
    if additions:
        # Paths/types are safe to report; never print hashes or source lines.
        locations = sorted({f"{path} ({kind})" for path, kind, _ in additions})
        raise ValueError(
            "new secret findings outside reviewed baseline: "
            + ", ".join(locations)
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--approved", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args()
    verify_baseline(
        json.loads(args.approved.read_text()),
        json.loads(args.candidate.read_text()),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
