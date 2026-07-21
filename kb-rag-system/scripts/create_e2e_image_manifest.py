"""Create the immutable gate report for the staging E2E image."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


_COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_IMAGE_DIGEST_RE = re.compile(r"^.+@sha256:[0-9a-f]{64}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} no es JSON legible") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} debe ser un objeto JSON")
    return value


def build_manifest(
    *,
    commit_sha: str,
    image_digest: str,
    sbom_path: str | Path,
    scan_policy_path: str | Path,
) -> dict[str, Any]:
    """Validate both gate artifacts and bind them to one image lineage."""
    if _COMMIT_RE.fullmatch(commit_sha) is None:
        raise ValueError("main_sha no es un commit SHA completo")
    if _IMAGE_DIGEST_RE.fullmatch(image_digest) is None:
        raise ValueError("image_digest no es inmutable")

    sbom_file = Path(sbom_path)
    scan_file = Path(scan_policy_path)
    sbom = _load_object(sbom_file, label="SBOM")
    is_spdx = isinstance(sbom.get("spdxVersion"), str)
    is_cyclonedx = sbom.get("bomFormat") == "CycloneDX"
    if not (is_spdx or is_cyclonedx):
        raise ValueError("SBOM no tiene formato SPDX/CycloneDX")
    components = sbom.get("packages") if is_spdx else sbom.get("components")
    if not isinstance(components, list):
        raise ValueError("SBOM no contiene una lista de componentes")

    scan = _load_object(scan_file, label="scan policy")
    if scan.get("digest") != image_digest:
        raise ValueError("scan policy digest distinto")
    counts = scan.get("severity_counts")
    if not isinstance(counts, dict) or any(
        not isinstance(name, str)
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count < 0
        for name, count in counts.items()
    ):
        raise ValueError("scan policy severity_counts inválido")
    if counts.get("CRITICAL", 0) != 0:
        raise ValueError("scan policy contiene CRITICAL")

    body: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": "e2e_image",
        "status": "passed",
        "main_sha": commit_sha,
        "image_digest": image_digest,
        "sbom_sha256": _sha256(sbom_file),
        "scan_policy_sha256": _sha256(scan_file),
        "scan_severity_counts": dict(sorted(counts.items())),
    }
    body["manifest_hash"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--sbom", required=True)
    parser.add_argument("--scan-policy", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    manifest = build_manifest(
        commit_sha=args.commit_sha,
        image_digest=args.image_digest,
        sbom_path=args.sbom,
        scan_policy_path=args.scan_policy,
    )
    Path(args.out).write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(manifest["manifest_hash"])
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
