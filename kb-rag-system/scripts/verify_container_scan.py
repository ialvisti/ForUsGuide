"""Fail-closed policy for Artifact Analysis on-demand scan results.

CRITICAL and HIGH findings are never accepted from candidate-controlled input.
Any future G5V exception must be supplied by an authenticated, multiparty
receipt verifier outside this source-build policy.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_SEVERITIES = {"CRITICAL", "HIGH", "MEDIUM", "LOW", "MINIMAL", "NEGLIGIBLE", "UNKNOWN"}
_CVE_RE = re.compile(r"\bCVE-\d{4}-\d{4,}\b", re.IGNORECASE)


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


def verify(
    payload: Any,
    *,
    digest: str,
) -> dict[str, Any]:
    if not re.search(r"@sha256:[0-9a-f]{64}$", digest):
        raise ScanRejected("image digest is not immutable")
    findings = _normalise_findings(payload)
    critical = [finding for finding in findings if finding.severity == "CRITICAL"]
    if critical:
        ids = sorted({identifier for finding in critical for identifier in finding.identifiers})
        raise ScanRejected(f"CRITICAL vulnerabilities block release: {ids or ['unidentified']}")

    high = [finding for finding in findings if finding.severity == "HIGH"]
    if high:
        identifiers = sorted({
            identifier for finding in high for identifier in finding.identifiers
        })
        raise ScanRejected(
            "HIGH vulnerabilities require authenticated external G5V receipts: "
            + ", ".join(identifiers or ["unidentified-HIGH"])
        )

    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    return {
        "digest": digest,
        "severity_counts": dict(sorted(counts.items())),
        "high_approvals": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scan-json", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--report-out", required=True)
    args = parser.parse_args()

    payload = json.loads(Path(args.scan_json).read_text(encoding="utf-8"))
    report = verify(payload, digest=args.image_digest)
    Path(args.report_out).write_text(
        json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print("container-scan: policy satisfied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
