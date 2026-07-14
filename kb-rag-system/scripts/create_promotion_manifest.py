"""
Crea la promotion attestation canónica (Tarea 12 Paso 2 / Tarea 15 Paso 9).

Vincula `main_sha + image_digest` con provenance/SBOM/scan y los hashes del
staging canónico, E2E, diferencial y rollback. Production plan/apply rechazan
cualquier digest/SHA sin una attestation válida o con cualquier hash distinto.
Write-once: si cambia un reporte, se emite otra attestation (no se sobrescribe).
"""

from __future__ import annotations

import argparse
import hashlib
import json

REQUIRED_FIELDS = (
    "main_sha", "image_digest", "ci_provenance_hash", "sbom_hash",
    "scan_hash", "staging_revision_hashes", "e2e_hash", "differential_hash",
    "rollback_hash", "g2_approval", "g4_approval", "g5_approval",
)


def build_promotion(fields: dict) -> dict:
    missing = [f for f in REQUIRED_FIELDS if fields.get(f) in (None, "", [])]
    if missing:
        raise ValueError(f"faltan campos de la promotion attestation: {missing}")
    body = {k: fields[k] for k in REQUIRED_FIELDS}
    body["attestation_hash"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return body


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Crea la promotion attestation")
    for f in REQUIRED_FIELDS:
        p.add_argument(f"--{f.replace('_', '-')}", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)
    fields = {f: getattr(args, f) for f in REQUIRED_FIELDS}
    attestation = build_promotion(fields)
    with open(args.out, "w") as fh:
        json.dump(attestation, fh, sort_keys=True, indent=2)
    print(attestation["attestation_hash"])
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
