"""Verifica promotion + evidence lineage antes de production plan/apply."""

from __future__ import annotations

import argparse
import json
import sys

from create_evidence_manifest import (
    REQUIRED_FIELDS as EVIDENCE_REQUIRED_FIELDS,
    validate_fields as validate_evidence_fields,
)
from create_promotion_manifest import (
    EVIDENCE_COPY_FIELDS,
    REQUIRED_FIELDS,
    _canonical_hash,
)
from create_plan_manifest import generation_from_uri


class PromotionRejected(Exception):
    pass


def verify(
    attestation: dict,
    *,
    evidence_manifest: dict,
    expected_attestation_hash: str,
    expected_evidence_manifest_uri: str,
    expected_evidence_manifest_hash: str,
    expected_main_sha: str,
    expected_image_digest: str,
    expected_controller_builder_digest: str,
    expected_evidence_controller_builder_digest: str,
) -> None:
    if set(attestation) != set(REQUIRED_FIELDS) | {"attestation_hash"}:
        raise PromotionRejected("campos de promotion inválidos")
    recomputed = _canonical_hash(attestation, REQUIRED_FIELDS)
    if recomputed != attestation.get("attestation_hash") \
            or recomputed != expected_attestation_hash:
        raise PromotionRejected("attestation_hash distinto")

    if set(evidence_manifest) \
            != set(EVIDENCE_REQUIRED_FIELDS) | {"manifest_hash"}:
        raise PromotionRejected("campos de evidence inválidos")
    try:
        validate_evidence_fields(evidence_manifest)
    except ValueError as exc:
        raise PromotionRejected(str(exc)) from exc
    evidence_hash = _canonical_hash(
        evidence_manifest, EVIDENCE_REQUIRED_FIELDS,
    )
    if evidence_hash != evidence_manifest.get("manifest_hash") \
            or evidence_hash != expected_evidence_manifest_hash:
        raise PromotionRejected("evidence manifest hash distinto")

    try:
        generation_from_uri(expected_evidence_manifest_uri)
    except ValueError as exc:
        raise PromotionRejected(str(exc)) from exc
    expected = {
        "main_sha": expected_main_sha,
        "image_digest": expected_image_digest,
        "evidence_manifest_uri": expected_evidence_manifest_uri,
        "evidence_manifest_hash": expected_evidence_manifest_hash,
        "controller_builder_digest": expected_controller_builder_digest,
        "evidence_controller_builder_digest": (
            expected_evidence_controller_builder_digest
        ),
    }
    for key, value in expected.items():
        if attestation.get(key) != value:
            raise PromotionRejected(f"{key} distinto")

    if evidence_manifest.get("main_sha") != expected_main_sha:
        raise PromotionRejected("main_sha de evidence distinto")
    if evidence_manifest.get("image_digest") != expected_image_digest:
        raise PromotionRejected("image_digest de evidence distinto")
    if evidence_manifest.get("controller_builder_digest") \
            != expected_evidence_controller_builder_digest:
        raise PromotionRejected("builder de evidence distinto")
    for key in EVIDENCE_COPY_FIELDS:
        if attestation.get(key) != evidence_manifest.get(key):
            raise PromotionRejected(f"promotion evidence {key} distinto")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Verifica promotion/evidence")
    parser.add_argument("--attestation", required=True)
    parser.add_argument("--evidence-manifest", required=True)
    parser.add_argument("--expected-attestation-hash", required=True)
    parser.add_argument("--expected-evidence-manifest-uri", required=True)
    parser.add_argument("--expected-evidence-manifest-hash", required=True)
    parser.add_argument("--expected-main-sha", required=True)
    parser.add_argument("--expected-image-digest", required=True)
    parser.add_argument("--expected-controller-builder-digest", required=True)
    parser.add_argument(
        "--expected-evidence-controller-builder-digest", required=True,
    )
    args = parser.parse_args(argv)
    with open(args.attestation) as fh:
        attestation = json.load(fh)
    with open(args.evidence_manifest) as fh:
        evidence = json.load(fh)
    try:
        verify(
            attestation,
            evidence_manifest=evidence,
            expected_attestation_hash=args.expected_attestation_hash,
            expected_evidence_manifest_uri=args.expected_evidence_manifest_uri,
            expected_evidence_manifest_hash=(
                args.expected_evidence_manifest_hash
            ),
            expected_main_sha=args.expected_main_sha,
            expected_image_digest=args.expected_image_digest,
            expected_controller_builder_digest=(
                args.expected_controller_builder_digest
            ),
            expected_evidence_controller_builder_digest=(
                args.expected_evidence_controller_builder_digest
            ),
        )
    except PromotionRejected as exc:
        print(f"PROMOTION REJECTED: {exc}", file=sys.stderr)
        return 1
    print("promotion attestation OK")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
