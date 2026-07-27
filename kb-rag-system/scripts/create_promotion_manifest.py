"""Crea una promotion attestation desde un evidence manifest exacto."""

from __future__ import annotations

import argparse
import hashlib
import json

from create_evidence_manifest import (
    ARTIFACT_NAMES,
    REQUIRED_FIELDS as EVIDENCE_REQUIRED_FIELDS,
    validate_fields as validate_evidence_fields,
)
from create_plan_manifest import generation_from_uri

EVIDENCE_COPY_FIELDS = (
    "evidence_sha",
    *(field for name in ARTIFACT_NAMES for field in (
        f"{name}_uri", f"{name}_hash",
    )),
    "artifact_claims",
)

REQUIRED_FIELDS = (
    "main_sha",
    "image_digest",
    "evidence_manifest_uri",
    "evidence_manifest_hash",
    "evidence_controller_builder_digest",
    "controller_builder_digest",
    *EVIDENCE_COPY_FIELDS,
)


def _canonical_hash(body: dict, fields: tuple[str, ...]) -> str:
    canonical = {key: body.get(key) for key in fields}
    return hashlib.sha256(json.dumps(
        canonical, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def _validate_evidence_internal(evidence: dict) -> None:
    if set(evidence) != set(EVIDENCE_REQUIRED_FIELDS) | {"manifest_hash"}:
        raise ValueError("evidence manifest con campos inválidos")
    validate_evidence_fields(evidence)
    if _canonical_hash(evidence, EVIDENCE_REQUIRED_FIELDS) \
            != evidence.get("manifest_hash"):
        raise ValueError("evidence manifest alterado")


def build_promotion(fields: dict) -> dict:
    missing = [name for name in REQUIRED_FIELDS if not fields.get(name)]
    if missing:
        raise ValueError(f"faltan campos de la promotion attestation: {missing}")
    generation_from_uri(str(fields["evidence_manifest_uri"]))
    body = {key: fields[key] for key in REQUIRED_FIELDS}
    body["attestation_hash"] = _canonical_hash(body, REQUIRED_FIELDS)
    return body


def build_promotion_from_evidence(
    evidence: dict,
    *,
    evidence_manifest_uri: str,
    controller_builder_digest: str,
) -> dict:
    _validate_evidence_internal(evidence)
    fields = {
        "main_sha": evidence["main_sha"],
        "image_digest": evidence["image_digest"],
        "evidence_manifest_uri": evidence_manifest_uri,
        "evidence_manifest_hash": evidence["manifest_hash"],
        "evidence_controller_builder_digest": (
            evidence["controller_builder_digest"]
        ),
        "controller_builder_digest": controller_builder_digest,
    }
    fields.update({key: evidence[key] for key in EVIDENCE_COPY_FIELDS})
    return build_promotion(fields)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Crea promotion desde evidence")
    parser.add_argument("--evidence-manifest", required=True)
    parser.add_argument("--evidence-manifest-uri", required=True)
    parser.add_argument("--controller-builder-digest", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    with open(args.evidence_manifest) as fh:
        evidence = json.load(fh)
    attestation = build_promotion_from_evidence(
        evidence,
        evidence_manifest_uri=args.evidence_manifest_uri,
        controller_builder_digest=args.controller_builder_digest,
    )
    with open(args.out, "w") as fh:
        json.dump(attestation, fh, sort_keys=True, indent=2)
    print(attestation["attestation_hash"])
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
