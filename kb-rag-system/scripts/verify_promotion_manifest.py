"""
Verifica la promotion attestation en production plan/apply (Tarea 12 Paso 2).

Sin attestation válida o con cualquier hash distinto (digest o SHA), el build
termina ANTES de `terraform`. Cubre positive/tampering/wrong-digest/wrong-SHA
(ver tests/test_release_manifests.py).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys

from create_promotion_manifest import REQUIRED_FIELDS


class PromotionRejected(Exception):
    pass


def verify(attestation: dict, *, expected_main_sha: str,
           expected_image_digest: str) -> None:
    body = {k: attestation.get(k) for k in REQUIRED_FIELDS}
    recomputed = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if recomputed != attestation.get("attestation_hash"):
        raise PromotionRejected("attestation_hash no coincide (alterada)")
    if attestation.get("main_sha") != expected_main_sha:
        raise PromotionRejected("main_sha distinto del promovido")
    if attestation.get("image_digest") != expected_image_digest:
        raise PromotionRejected("image_digest distinto del promovido")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Verifica la promotion attestation")
    p.add_argument("--attestation", required=True)
    p.add_argument("--expected-main-sha", required=True)
    p.add_argument("--expected-image-digest", required=True)
    args = p.parse_args(argv)
    with open(args.attestation) as fh:
        attestation = json.load(fh)
    try:
        verify(attestation, expected_main_sha=args.expected_main_sha,
               expected_image_digest=args.expected_image_digest)
    except PromotionRejected as exc:
        print(f"PROMOTION REJECTED: {exc}", file=sys.stderr)
        return 1
    print("promotion attestation OK")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
