"""Construcción canónica del evidence manifest de una release."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from typing import Any

from create_plan_manifest import generation_from_uri

ARTIFACT_NAMES = (
    "ci_provenance",
    "sbom",
    "scan",
    "staging_revisions",
    "e2e",
    "differential",
    "rollback",
)

REQUIRED_FIELDS = (
    "evidence_sha",
    "main_sha",
    "image_digest",
    "controller_builder_digest",
    *(field for name in ARTIFACT_NAMES for field in (
        f"{name}_uri", f"{name}_hash",
    )),
    "g2_approval_hash",
    "g4_approval_hash",
    "g5_approval_hash",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_approval_hashes(path: str) -> dict[str, str]:
    """Extrae G2/G4/G5 sólo de filas completas con texto de aprobación."""
    wanted = {"G2", "G4", "G5"}
    rows: dict[str, str] = {}
    with open(path) as fh:
        for raw_line in fh:
            if not raw_line.lstrip().startswith("|"):
                continue
            cells = [cell.strip() for cell in raw_line.strip().strip("|").split("|")]
            if len(cells) != 7 or cells[0] not in wanted:
                continue
            gate = cells[0]
            if not cells[1].startswith(f"APROBADO {gate} "):
                continue
            if any(cell in ("", "—", "-") for cell in cells[2:]):
                continue
            rows[gate] = "|".join(cells)
    missing = sorted(wanted - set(rows))
    if missing:
        raise ValueError(f"faltan aprobaciones reales: {missing}")
    return {
        f"{gate.lower()}_approval_hash": hashlib.sha256(
            rows[gate].encode()
        ).hexdigest()
        for gate in sorted(wanted)
    }


def validate_fields(fields: dict[str, Any]) -> None:
    missing = [name for name in REQUIRED_FIELDS if not fields.get(name)]
    if missing:
        raise ValueError(f"faltan campos del evidence manifest: {missing}")
    for name in ARTIFACT_NAMES:
        generation_from_uri(str(fields[f"{name}_uri"]))
        if _SHA256_RE.fullmatch(str(fields[f"{name}_hash"])) is None:
            raise ValueError(f"{name}_hash no es SHA-256")
    for gate in ("g2", "g4", "g5"):
        if _SHA256_RE.fullmatch(str(fields[f"{gate}_approval_hash"])) is None:
            raise ValueError(f"{gate}_approval_hash no es SHA-256")


def build_manifest(fields: dict[str, Any]) -> dict[str, Any]:
    validate_fields(fields)

    manifest = {key: fields[key] for key in REQUIRED_FIELDS}
    manifest["manifest_hash"] = hashlib.sha256(json.dumps(
        manifest, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Crea evidence manifest")
    parser.add_argument("--evidence-sha", required=True)
    parser.add_argument("--main-sha", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--controller-builder-digest", required=True)
    parser.add_argument("--approvals-file", required=True)
    parser.add_argument("--out", required=True)
    for name in ARTIFACT_NAMES:
        option = name.replace("_", "-")
        parser.add_argument(f"--{option}-uri", required=True)
        parser.add_argument(f"--{option}-file", required=True)
    args = parser.parse_args(argv)

    fields: dict[str, Any] = {
        "evidence_sha": args.evidence_sha,
        "main_sha": args.main_sha,
        "image_digest": args.image_digest,
        "controller_builder_digest": args.controller_builder_digest,
        **load_approval_hashes(args.approvals_file),
    }
    for name in ARTIFACT_NAMES:
        fields[f"{name}_uri"] = getattr(args, f"{name}_uri")
        fields[f"{name}_hash"] = _sha256_file(
            getattr(args, f"{name}_file")
        )
    manifest = build_manifest(fields)
    with open(args.out, "w") as fh:
        json.dump(manifest, fh, sort_keys=True, indent=2)
    print(manifest["manifest_hash"])
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
