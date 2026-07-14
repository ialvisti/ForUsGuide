"""
Verifica un plan manifest antes de `terraform apply` (Tarea 12 Paso 2).

El trigger `*-apply` NO regenera el plan: recibe el URI+SHA-256 del plan
aprobado, verifica que el manifest coincide con el plan binario, el commit,
el digest, el root, el lockfile y el state serial ACTUAL. Un mismatch (state
drift, digest distinto, root cruzado) aborta antes de `terraform`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from typing import Any, Dict

from create_plan_manifest import REQUIRED_FIELDS, _sha256_file  # noqa: E402


class ManifestMismatch(Exception):
    pass


def verify(manifest: Dict[str, Any], *, plan_file: str,
           expected_plan_sha256: str, current_state_serial: str,
           expected_commit: str, expected_image_digest: str,
           expected_root: str, provider_lock: str) -> None:
    # 0) el manifest no fue alterado
    body = {k: manifest.get(k) for k in REQUIRED_FIELDS}
    recomputed = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if recomputed != manifest.get("manifest_hash"):
        raise ManifestMismatch("manifest_hash no coincide (manifest alterado)")

    # 1) el plan binario es EXACTAMENTE el aprobado
    actual_plan_sha = _sha256_file(plan_file)
    if actual_plan_sha != expected_plan_sha256:
        raise ManifestMismatch("el .tfplan no coincide con el hash aprobado")
    if manifest["plan_hash"] != expected_plan_sha256:
        raise ManifestMismatch("plan_hash del manifest != hash aprobado")

    # 2) commit / digest / root exactos
    if manifest["commit"] != expected_commit:
        raise ManifestMismatch("commit distinto")
    if manifest["image_digest"] != expected_image_digest:
        raise ManifestMismatch("image_digest distinto")
    if manifest["root"] != expected_root:
        raise ManifestMismatch("root cruzado")

    # 3) state serial ACTUAL == el del plan (drift → nuevo gate)
    if str(manifest["state_serial"]) != str(current_state_serial):
        raise ManifestMismatch(
            "state drift: el serial actual difiere del planificado; "
            "regenerar plan y renovar gate")

    # 4) provider lock íntegro
    if manifest["provider_lock_hash"] != _sha256_file(provider_lock):
        raise ManifestMismatch("provider-lock hash distinto")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Verifica un plan manifest")
    p.add_argument("--manifest", required=True)
    p.add_argument("--plan-file", required=True)
    p.add_argument("--expected-plan-sha256", required=True)
    p.add_argument("--current-state-serial", required=True)
    p.add_argument("--expected-commit", required=True)
    p.add_argument("--expected-image-digest", required=True)
    p.add_argument("--expected-root", required=True)
    p.add_argument("--provider-lock", required=True)
    args = p.parse_args(argv)

    with open(args.manifest) as fh:
        manifest = json.load(fh)
    try:
        verify(manifest, plan_file=args.plan_file,
               expected_plan_sha256=args.expected_plan_sha256,
               current_state_serial=args.current_state_serial,
               expected_commit=args.expected_commit,
               expected_image_digest=args.expected_image_digest,
               expected_root=args.expected_root,
               provider_lock=args.provider_lock)
    except ManifestMismatch as exc:
        print(f"PLAN MANIFEST REJECTED: {exc}", file=sys.stderr)
        return 1
    print("plan manifest OK")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
