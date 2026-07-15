"""
Crea el manifest write-once de un `terraform plan` (plan de finalización,
Tarea 12 Paso 2). El manifest vincula el plan binario aprobado con su
contexto exacto para que el trigger `*-apply` verifique que aplica
EXACTAMENTE lo revisado. Un state drift invalida el hash y obliga a un
nuevo gate.

Campos (todos requeridos): root, backend bucket, state lineage/serial,
commit, image digest, release phase, provider-lock hash, plan hash, hash del
`terraform show` sanitizado, builder digest y GCS generation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from typing import Any, Dict

REQUIRED_FIELDS = (
    "root", "backend_bucket", "state_lineage", "state_serial", "commit",
    "image_digest", "release_phase", "provider_lock_hash", "plan_hash",
    "terraform_show_hash", "builder_digest", "gcs_generation", "plan_uri",
    "terraform_show_uri",
    "promotion_uri", "promotion_hash", "evidence_manifest_uri",
    "evidence_manifest_hash", "secret_version_manifest_uri",
    "secret_version_manifest_hash",
)

RELEASE_INPUT_PAIRS = (
    ("promotion_uri", "promotion_hash"),
    ("evidence_manifest_uri", "evidence_manifest_hash"),
    ("secret_version_manifest_uri", "secret_version_manifest_hash"),
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_GENERATION_URI_RE = re.compile(
    r"^gs://[a-z0-9][a-z0-9._-]*/[^#\r\n]+#([1-9][0-9]*)$"
)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def generation_from_uri(uri: str) -> str:
    match = _GENERATION_URI_RE.fullmatch(uri)
    if match is None:
        raise ValueError(
            "el URI debe ser gs://bucket/object#generation numérica"
        )
    return match.group(1)


def load_state_metadata(path: str) -> tuple[str, int]:
    with open(path) as fh:
        state = json.load(fh)
    lineage = state.get("lineage")
    serial = state.get("serial")
    if not isinstance(lineage, str) or not lineage.strip():
        raise ValueError("el state real no contiene lineage")
    if not isinstance(serial, int) or serial < 0:
        raise ValueError("el state real no contiene serial válido")
    return lineage, serial


def load_backend_bucket(path: str) -> str:
    with open(path) as fh:
        metadata = json.load(fh)
    backend = metadata.get("backend") or {}
    bucket = (backend.get("config") or {}).get("bucket")
    if backend.get("type") != "gcs" or not isinstance(bucket, str) \
            or not bucket.strip():
        raise ValueError("metadata de backend GCS inválida")
    return bucket


def build_manifest(fields: Dict[str, Any]) -> Dict[str, Any]:
    missing = [
        field for field in REQUIRED_FIELDS
        if field not in fields or fields[field] is None or fields[field] == ""
    ]
    if missing:
        raise ValueError(f"faltan campos obligatorios del manifest: {missing}")
    plan_generation = generation_from_uri(str(fields["plan_uri"]))
    generation_from_uri(str(fields["terraform_show_uri"]))
    if str(fields["gcs_generation"]) != plan_generation:
        raise ValueError("gcs_generation no coincide con plan_uri")
    production_root = str(fields["root"]).rstrip("/").endswith("production")
    for uri_field, hash_field in RELEASE_INPUT_PAIRS:
        uri = str(fields[uri_field])
        digest = str(fields[hash_field])
        if uri == "none" and digest == "none" and not production_root:
            continue
        generation_from_uri(uri)
        if _SHA256_RE.fullmatch(digest) is None:
            raise ValueError(f"{hash_field} debe ser un SHA-256 hexadecimal")
    if production_root and any(
        fields[uri_field] == "none" or fields[hash_field] == "none"
        for uri_field, hash_field in RELEASE_INPUT_PAIRS
    ):
        raise ValueError(
            "production requiere promotion, evidence y secret-version inputs"
        )
    # canónico y ordenado para hash reproducible
    manifest = {k: fields[k] for k in REQUIRED_FIELDS}
    manifest["manifest_hash"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return manifest


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Crea un plan manifest write-once")
    p.add_argument("--root", required=True)
    p.add_argument("--backend-metadata-file", required=True)
    p.add_argument("--state-file", required=True)
    p.add_argument("--commit", required=True)
    p.add_argument("--image-digest", required=True)
    p.add_argument("--release-phase", required=True)
    p.add_argument("--provider-lock", required=True, help="ruta a .terraform.lock.hcl")
    p.add_argument("--plan-file", required=True, help="ruta al .tfplan binario")
    p.add_argument("--show-file", required=True, help="terraform show sanitizado")
    p.add_argument("--plan-uri", required=True)
    p.add_argument("--show-uri", required=True)
    p.add_argument("--builder-digest", required=True)
    p.add_argument("--promotion-uri", required=True)
    p.add_argument("--promotion-hash", required=True)
    p.add_argument("--evidence-manifest-uri", required=True)
    p.add_argument("--evidence-manifest-hash", required=True)
    p.add_argument("--secret-version-manifest-uri", required=True)
    p.add_argument("--secret-version-manifest-hash", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)

    state_lineage, state_serial = load_state_metadata(args.state_file)
    plan_generation = generation_from_uri(args.plan_uri)

    manifest = build_manifest({
        "root": args.root,
        "backend_bucket": load_backend_bucket(args.backend_metadata_file),
        "state_lineage": state_lineage,
        "state_serial": state_serial,
        "commit": args.commit,
        "image_digest": args.image_digest,
        "release_phase": args.release_phase,
        "provider_lock_hash": _sha256_file(args.provider_lock),
        "plan_hash": _sha256_file(args.plan_file),
        "terraform_show_hash": _sha256_file(args.show_file),
        "builder_digest": args.builder_digest,
        "gcs_generation": plan_generation,
        "plan_uri": args.plan_uri,
        "terraform_show_uri": args.show_uri,
        "promotion_uri": args.promotion_uri,
        "promotion_hash": args.promotion_hash,
        "evidence_manifest_uri": args.evidence_manifest_uri,
        "evidence_manifest_hash": args.evidence_manifest_hash,
        "secret_version_manifest_uri": args.secret_version_manifest_uri,
        "secret_version_manifest_hash": args.secret_version_manifest_hash,
    })
    with open(args.out, "w") as fh:
        json.dump(manifest, fh, sort_keys=True, indent=2)
    print(manifest["manifest_hash"])
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
