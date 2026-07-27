#!/usr/bin/env python3
"""Fail CI when detect-secrets adds a finding to the reviewed baseline."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any


Finding = tuple[str, str, str]


def _findings(
    document: dict[str, Any], *, label: str,
) -> Counter[Finding]:
    if not isinstance(document, dict):
        raise ValueError(f"{label} detect-secrets document must be an object")
    results = document.get("results")
    if not isinstance(results, dict):
        raise ValueError(f"{label} detect-secrets results must be an object")
    findings: Counter[Finding] = Counter()
    for path, entries in results.items():
        if not isinstance(path, str) or not path or Path(path).is_absolute() \
                or ".." in Path(path).parts:
            raise ValueError(f"{label} detect-secrets result path is invalid")
        if not isinstance(entries, list):
            raise ValueError(f"{label} detect-secrets entries must be a list")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"{label} detect-secrets entry must be an object")
            kind = entry.get("type")
            hashed = entry.get("hashed_secret")
            if not isinstance(kind, str) or not kind \
                    or not isinstance(hashed, str) or not hashed:
                raise ValueError(
                    f"{label} detect-secrets entry is missing type or hashed_secret"
                )
            findings[(path, kind, hashed)] += 1
    return findings


def _safe_locations(findings: Counter[Finding]) -> str:
    return ", ".join(sorted({
        f"{path} ({kind})" for path, kind, _hashed in findings
    }))


def _validate_configuration(document: dict[str, Any], *, label: str) -> None:
    version = document.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError(f"{label} detect-secrets version is invalid")
    plugins = document.get("plugins_used")
    if not isinstance(plugins, list) or not plugins or any(
        not isinstance(item, dict)
        or not isinstance(item.get("name"), str)
        or not item["name"]
        for item in plugins
    ):
        raise ValueError(f"{label} detect-secrets plugins_used is invalid")
    filters = document.get("filters_used")
    if not isinstance(filters, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("path"), str)
        or not item["path"]
        for item in filters
    ):
        raise ValueError(f"{label} detect-secrets filters_used is invalid")


def verify_baseline(
    approved: dict[str, Any],
    candidate: dict[str, Any],
    *,
    scan_root: Path | None = None,
) -> None:
    _validate_configuration(approved, label="approved")
    _validate_configuration(candidate, label="candidate")
    for field in ("version", "plugins_used", "filters_used"):
        if candidate.get(field) != approved.get(field):
            raise ValueError(f"detect-secrets configuration changed: {field}")

    approved_findings = _findings(approved, label="approved")
    candidate_findings = _findings(candidate, label="candidate")
    additions = candidate_findings - approved_findings
    if additions:
        # Paths/types are safe to report; never print hashes or source lines.
        raise ValueError(
            "new secret findings outside reviewed baseline: "
            + _safe_locations(additions)
        )
    if scan_root is not None:
        if not scan_root.is_dir():
            raise ValueError("detect-secrets scan root must be a directory")
        disappeared: Counter[Finding] = Counter()
        for finding, approved_count in approved_findings.items():
            path, _kind, _hashed = finding
            if (scan_root / path).is_file() \
                    and candidate_findings[finding] < approved_count:
                disappeared[finding] = (
                    approved_count - candidate_findings[finding]
                )
        if disappeared:
            raise ValueError(
                "reviewed secret findings disappeared from present files: "
                + _safe_locations(disappeared)
            )


def verify_no_findings(candidate: dict[str, Any]) -> None:
    """Fail closed when an uploaded external input resembles any secret."""
    _validate_configuration(candidate, label="candidate")
    findings = _findings(candidate, label="candidate")
    if findings:
        raise ValueError(
            "external input secret findings are forbidden: "
            + _safe_locations(findings)
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--approved", type=Path)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--scan-root", type=Path)
    parser.add_argument("--require-empty", action="store_true")
    args = parser.parse_args()
    candidate = json.loads(args.candidate.read_text())
    if args.require_empty:
        if args.approved is not None or args.scan_root is not None:
            parser.error(
                "--require-empty cannot be combined with --approved/--scan-root"
            )
        verify_no_findings(candidate)
    else:
        if args.approved is None or args.scan_root is None:
            parser.error(
                "--approved and --scan-root are required for baseline comparison"
            )
        verify_baseline(
            json.loads(args.approved.read_text()),
            candidate,
            scan_root=args.scan_root,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
