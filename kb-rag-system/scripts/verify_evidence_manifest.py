"""Verificación fail-closed del evidence manifest de una release."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from typing import Optional

from create_evidence_manifest import (
    ARTIFACT_NAMES,
    REQUIRED_FIELDS,
    _sha256_file,
    validate_fields,
)


class EvidenceMismatch(Exception):
    pass


def verify(
    manifest: dict,
    *,
    expected_manifest_hash: str,
    expected_evidence_sha: str,
    expected_main_sha: str,
    expected_image_digest: str,
    expected_controller_builder_digest: str,
    artifact_files: Optional[dict[str, str]] = None,
) -> None:
    canonical = {key: manifest.get(key) for key in REQUIRED_FIELDS}
    recomputed = hashlib.sha256(json.dumps(
        canonical, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    if set(manifest) != set(REQUIRED_FIELDS) | {"manifest_hash"}:
        raise EvidenceMismatch("campos desconocidos o ausentes")
    try:
        validate_fields(manifest)
    except ValueError as exc:
        raise EvidenceMismatch(str(exc)) from exc
    if recomputed != manifest.get("manifest_hash") \
            or recomputed != expected_manifest_hash:
        raise EvidenceMismatch("evidence manifest hash distinto")
    expected = {
        "evidence_sha": expected_evidence_sha,
        "main_sha": expected_main_sha,
        "image_digest": expected_image_digest,
        "controller_builder_digest": expected_controller_builder_digest,
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise EvidenceMismatch(f"{key} distinto")

    if artifact_files is not None:
        if set(artifact_files) != set(ARTIFACT_NAMES):
            raise EvidenceMismatch("faltan archivos de evidencia")
        for name, path in artifact_files.items():
            if _sha256_file(path) != manifest[f"{name}_hash"]:
                raise EvidenceMismatch(f"hash de {name} distinto")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Verifica evidence manifest")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--expected-manifest-hash", required=True)
    parser.add_argument("--expected-evidence-sha", required=True)
    parser.add_argument("--expected-main-sha", required=True)
    parser.add_argument("--expected-image-digest", required=True)
    parser.add_argument("--expected-controller-builder-digest", required=True)
    parser.add_argument("--artifact", action="append", default=[])
    args = parser.parse_args(argv)
    artifact_files = None
    if args.artifact:
        artifact_files = {}
        for item in args.artifact:
            if "=" not in item:
                parser.error("--artifact exige name=path")
            name, path = item.split("=", 1)
            artifact_files[name] = path
    with open(args.manifest) as fh:
        manifest = json.load(fh)
    try:
        verify(
            manifest,
            expected_manifest_hash=args.expected_manifest_hash,
            expected_evidence_sha=args.expected_evidence_sha,
            expected_main_sha=args.expected_main_sha,
            expected_image_digest=args.expected_image_digest,
            expected_controller_builder_digest=(
                args.expected_controller_builder_digest
            ),
            artifact_files=artifact_files,
        )
    except EvidenceMismatch as exc:
        print(f"EVIDENCE REJECTED: {exc}", file=sys.stderr)
        return 1
    print("evidence manifest OK")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
