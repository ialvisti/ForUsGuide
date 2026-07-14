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
import sys
from typing import Any, Dict

REQUIRED_FIELDS = (
    "root", "backend_bucket", "state_lineage", "state_serial", "commit",
    "image_digest", "release_phase", "provider_lock_hash", "plan_hash",
    "terraform_show_hash", "builder_digest", "gcs_generation",
)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(fields: Dict[str, Any]) -> Dict[str, Any]:
    missing = [f for f in REQUIRED_FIELDS if not fields.get(f)]
    if missing:
        raise ValueError(f"faltan campos obligatorios del manifest: {missing}")
    # canónico y ordenado para hash reproducible
    manifest = {k: fields[k] for k in REQUIRED_FIELDS}
    manifest["manifest_hash"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return manifest


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Crea un plan manifest write-once")
    p.add_argument("--root", required=True)
    p.add_argument("--backend-bucket", required=True)
    p.add_argument("--state-lineage", required=True)
    p.add_argument("--state-serial", required=True)
    p.add_argument("--commit", required=True)
    p.add_argument("--image-digest", required=True)
    p.add_argument("--release-phase", required=True)
    p.add_argument("--provider-lock", required=True, help="ruta a .terraform.lock.hcl")
    p.add_argument("--plan-file", required=True, help="ruta al .tfplan binario")
    p.add_argument("--show-file", required=True, help="terraform show sanitizado")
    p.add_argument("--builder-digest", required=True)
    p.add_argument("--gcs-generation", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)

    manifest = build_manifest({
        "root": args.root,
        "backend_bucket": args.backend_bucket,
        "state_lineage": args.state_lineage,
        "state_serial": args.state_serial,
        "commit": args.commit,
        "image_digest": args.image_digest,
        "release_phase": args.release_phase,
        "provider_lock_hash": _sha256_file(args.provider_lock),
        "plan_hash": _sha256_file(args.plan_file),
        "terraform_show_hash": _sha256_file(args.show_file),
        "builder_digest": args.builder_digest,
        "gcs_generation": args.gcs_generation,
    })
    with open(args.out, "w") as fh:
        json.dump(manifest, fh, sort_keys=True, indent=2)
    print(manifest["manifest_hash"])
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
