"""Fail-closed policy for Artifact Analysis on-demand scan results.

CRITICAL findings are never exempted.  Every HIGH finding requires a distinct,
unexpired G5V approval scoped to the exact image digest and CVE.  Output is
limited to public vulnerability identifiers and aggregate counts.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any


_SEVERITIES = {"CRITICAL", "HIGH", "MEDIUM", "LOW", "MINIMAL", "NEGLIGIBLE", "UNKNOWN"}
_CVE_RE = re.compile(r"\bCVE-\d{4}-\d{4,}\b", re.IGNORECASE)
_EXPIRY_RE = re.compile(r"\bexpires=(\d{4}-\d{2}-\d{2})\b")


class ScanRejected(RuntimeError):
    """Raised when the vulnerability policy is not satisfied."""


@dataclass(frozen=True)
class Finding:
    severity: str
    identifiers: tuple[str, ...]


def _walk(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _normalise_findings(payload: Any) -> list[Finding]:
    records = payload if isinstance(payload, list) else [payload]
    normalised: list[Finding] = []
    for record in records:
        strings = [item for item in _walk(record) if isinstance(item, str)]
        severities = [item.upper() for item in strings if item.upper() in _SEVERITIES]
        if not severities:
            continue
        identifiers = sorted({match.upper() for item in strings for match in _CVE_RE.findall(item)})
        normalised.append(Finding(severity=severities[0], identifiers=tuple(identifiers)))
    return normalised


def _approved_high(
    *,
    digest: str,
    cve: str,
    approvals_text: str,
    today: date,
) -> bool:
    for line in approvals_text.splitlines():
        if "APROBADO G5V" not in line or digest not in line or cve not in line.upper():
            continue
        required_fields = (
            "security-owner=", "release-owner=", "requester=",
            "exploitability=", "compensating-control=",
        )
        if not all(field in line for field in required_fields):
            continue
        match = _EXPIRY_RE.search(line)
        if match is None:
            continue
        expiry = date.fromisoformat(match.group(1))
        remaining_days = (expiry - today).days
        if 0 <= remaining_days <= 30:
            return True
    return False


def verify(
    payload: Any,
    *,
    digest: str,
    approvals_text: str,
    today: date | None = None,
) -> dict[str, Any]:
    if not re.search(r"@sha256:[0-9a-f]{64}$", digest):
        raise ScanRejected("image digest is not immutable")
    effective_today = today or datetime.now().astimezone().date()
    findings = _normalise_findings(payload)
    critical = [finding for finding in findings if finding.severity == "CRITICAL"]
    if critical:
        ids = sorted({identifier for finding in critical for identifier in finding.identifiers})
        raise ScanRejected(f"CRITICAL vulnerabilities block release: {ids or ['unidentified']}")

    high = [finding for finding in findings if finding.severity == "HIGH"]
    unapproved: list[str] = []
    for finding in high:
        if not finding.identifiers:
            unapproved.append("unidentified-HIGH")
            continue
        for cve in finding.identifiers:
            if not _approved_high(
                digest=digest,
                cve=cve,
                approvals_text=approvals_text,
                today=effective_today,
            ):
                unapproved.append(cve)
    if unapproved:
        raise ScanRejected(
            "HIGH vulnerabilities require exact, unexpired G5V approvals: "
            + ", ".join(sorted(set(unapproved)))
        )

    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    return {"digest": digest, "severity_counts": dict(sorted(counts.items()))}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scan-json", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--approvals", required=True)
    parser.add_argument("--report-out", required=True)
    args = parser.parse_args()

    payload = json.loads(Path(args.scan_json).read_text(encoding="utf-8"))
    approvals_text = Path(args.approvals).read_text(encoding="utf-8")
    report = verify(payload, digest=args.image_digest, approvals_text=approvals_text)
    Path(args.report_out).write_text(
        json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print("container-scan: policy satisfied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
