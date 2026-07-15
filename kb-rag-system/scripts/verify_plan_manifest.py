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

from create_plan_manifest import (  # noqa: E402
    REQUIRED_FIELDS,
    _sha256_file,
    generation_from_uri,
    load_backend_bucket,
    load_state_metadata,
)


class ManifestMismatch(Exception):
    pass


def verify(
    manifest: Dict[str, Any],
    *,
    plan_file: str,
    show_file: str,
    expected_plan_sha256: str,
    expected_plan_uri: str,
    expected_show_uri: str,
    current_state_file: str,
    backend_metadata_file: str,
    expected_commit: str,
    expected_image_digest: str,
    expected_root: str,
    expected_backend_bucket: str,
    expected_release_phase: str,
    provider_lock: str,
    expected_provider_lock_sha256: str,
    expected_show_sha256: str,
    expected_controller_builder_digest: str,
    expected_promotion_uri: str,
    expected_promotion_hash: str,
    expected_evidence_manifest_uri: str,
    expected_evidence_manifest_hash: str,
    expected_secret_version_manifest_uri: str,
    expected_secret_version_manifest_hash: str,
) -> None:
    # 0) el manifest no fue alterado
    body = {k: manifest.get(k) for k in REQUIRED_FIELDS}
    recomputed = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if recomputed != manifest.get("manifest_hash"):
        raise ManifestMismatch("manifest_hash no coincide (manifest alterado)")
    if set(manifest) != set(REQUIRED_FIELDS) | {"manifest_hash"}:
        raise ManifestMismatch("campos desconocidos o ausentes en el manifest")

    # 1) el plan binario es EXACTAMENTE el aprobado
    actual_plan_sha = _sha256_file(plan_file)
    if actual_plan_sha != expected_plan_sha256:
        raise ManifestMismatch("el .tfplan no coincide con el hash aprobado")
    if manifest["plan_hash"] != expected_plan_sha256:
        raise ManifestMismatch("plan_hash del manifest != hash aprobado")
    try:
        plan_generation = generation_from_uri(expected_plan_uri)
        generation_from_uri(expected_show_uri)
    except ValueError as exc:
        raise ManifestMismatch(str(exc)) from exc
    if manifest["plan_uri"] != expected_plan_uri \
            or str(manifest["gcs_generation"]) != plan_generation:
        raise ManifestMismatch("URI/generation del plan distinto")
    if manifest["terraform_show_uri"] != expected_show_uri:
        raise ManifestMismatch("URI/generation del terraform show distinto")

    # 2) commit / digest / root / fase exactos
    if manifest["commit"] != expected_commit:
        raise ManifestMismatch("commit distinto")
    if manifest["image_digest"] != expected_image_digest:
        raise ManifestMismatch("image_digest distinto")
    if manifest["root"] != expected_root:
        raise ManifestMismatch("root cruzado")
    if manifest["release_phase"] != expected_release_phase:
        raise ManifestMismatch("release phase distinta")

    # 3) lineage + serial ACTUALES == los del plan (drift → nuevo gate)
    try:
        current_lineage, current_serial = load_state_metadata(current_state_file)
        current_backend = load_backend_bucket(backend_metadata_file)
    except ValueError as exc:
        raise ManifestMismatch(str(exc)) from exc
    if manifest["state_lineage"] != current_lineage:
        raise ManifestMismatch("state drift: lineage actual distinto")
    if str(manifest["state_serial"]) != str(current_serial):
        raise ManifestMismatch(
            "state drift: el serial actual difiere del planificado; "
            "regenerar plan y renovar gate")
    if current_backend != expected_backend_bucket \
            or manifest["backend_bucket"] != expected_backend_bucket:
        raise ManifestMismatch("backend bucket distinto")

    # 4) provider lock íntegro
    actual_lock_hash = _sha256_file(provider_lock)
    if actual_lock_hash != expected_provider_lock_sha256 \
            or manifest["provider_lock_hash"] != expected_provider_lock_sha256:
        raise ManifestMismatch("provider-lock hash distinto")

    # 5) el show regenerado desde el plan coincide con el aprobado
    actual_show_hash = _sha256_file(show_file)
    if actual_show_hash != expected_show_sha256 \
            or manifest["terraform_show_hash"] != expected_show_sha256:
        raise ManifestMismatch("terraform show hash distinto")
    if manifest["builder_digest"] != expected_controller_builder_digest:
        raise ManifestMismatch("controller builder digest distinto")

    # 6) production inputs are externally supplied, generation-qualified and
    # bound into the reviewed plan. A self-consistently rehashed manifest must
    # not be able to swap promotion/evidence/secret-version lineage.
    release_expectations = (
        ("promotion_uri", expected_promotion_uri,
         "promotion_hash", expected_promotion_hash),
        ("evidence_manifest_uri", expected_evidence_manifest_uri,
         "evidence_manifest_hash", expected_evidence_manifest_hash),
        ("secret_version_manifest_uri", expected_secret_version_manifest_uri,
         "secret_version_manifest_hash", expected_secret_version_manifest_hash),
    )
    for uri_field, expected_uri, hash_field, expected_hash in release_expectations:
        if manifest[uri_field] != expected_uri \
                or manifest[hash_field] != expected_hash:
            raise ManifestMismatch(f"{uri_field}/{hash_field} distintos")
        if expected_uri == "none" and expected_hash == "none" \
                and not expected_root.rstrip("/").endswith("production"):
            continue
        try:
            generation_from_uri(expected_uri)
        except ValueError as exc:
            raise ManifestMismatch(str(exc)) from exc


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Verifica un plan manifest")
    p.add_argument("--manifest", required=True)
    p.add_argument("--plan-file", required=True)
    p.add_argument("--show-file", required=True)
    p.add_argument("--expected-plan-sha256", required=True)
    p.add_argument("--expected-plan-uri", required=True)
    p.add_argument("--expected-show-uri", required=True)
    p.add_argument("--current-state-file", required=True)
    p.add_argument("--backend-metadata-file", required=True)
    p.add_argument("--expected-commit", required=True)
    p.add_argument("--expected-image-digest", required=True)
    p.add_argument("--expected-root", required=True)
    p.add_argument("--expected-backend-bucket", required=True)
    p.add_argument("--expected-release-phase", required=True)
    p.add_argument("--provider-lock", required=True)
    p.add_argument("--expected-provider-lock-sha256", required=True)
    p.add_argument("--expected-show-sha256", required=True)
    p.add_argument("--expected-controller-builder-digest", required=True)
    p.add_argument("--expected-promotion-uri", required=True)
    p.add_argument("--expected-promotion-hash", required=True)
    p.add_argument("--expected-evidence-manifest-uri", required=True)
    p.add_argument("--expected-evidence-manifest-hash", required=True)
    p.add_argument("--expected-secret-version-manifest-uri", required=True)
    p.add_argument("--expected-secret-version-manifest-hash", required=True)
    args = p.parse_args(argv)

    with open(args.manifest) as fh:
        manifest = json.load(fh)
    try:
        verify(manifest, plan_file=args.plan_file, show_file=args.show_file,
               expected_plan_sha256=args.expected_plan_sha256,
               expected_plan_uri=args.expected_plan_uri,
               expected_show_uri=args.expected_show_uri,
               current_state_file=args.current_state_file,
               backend_metadata_file=args.backend_metadata_file,
               expected_commit=args.expected_commit,
               expected_image_digest=args.expected_image_digest,
               expected_root=args.expected_root,
               expected_backend_bucket=args.expected_backend_bucket,
               expected_release_phase=args.expected_release_phase,
               provider_lock=args.provider_lock,
               expected_provider_lock_sha256=(
                   args.expected_provider_lock_sha256
               ),
               expected_show_sha256=args.expected_show_sha256,
               expected_controller_builder_digest=(
                   args.expected_controller_builder_digest
               ),
               expected_promotion_uri=args.expected_promotion_uri,
               expected_promotion_hash=args.expected_promotion_hash,
               expected_evidence_manifest_uri=(
                   args.expected_evidence_manifest_uri
               ),
               expected_evidence_manifest_hash=(
                   args.expected_evidence_manifest_hash
               ),
               expected_secret_version_manifest_uri=(
                   args.expected_secret_version_manifest_uri
               ),
               expected_secret_version_manifest_hash=(
                   args.expected_secret_version_manifest_hash
               ))
    except ManifestMismatch as exc:
        print(f"PLAN MANIFEST REJECTED: {exc}", file=sys.stderr)
        return 1
    print("plan manifest OK")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
