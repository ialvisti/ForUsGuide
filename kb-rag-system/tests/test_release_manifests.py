"""
Tests de los manifests de release (plan de finalización, Tarea 12 Paso 2).

Cubre positive / tampering / wrong-digest / wrong-SHA para el plan manifest y
la promotion attestation. Los scripts viven en scripts/ (no es un paquete):
se cargan por path.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


create_plan = _load("create_plan_manifest")
verify_plan = _load("verify_plan_manifest")
create_promo = _load("create_promotion_manifest")
verify_promo = _load("verify_promotion_manifest")


@pytest.fixture
def plan_files(tmp_path):
    plan = tmp_path / "plan.tfplan"
    plan.write_bytes(b"binary-plan-bytes")
    lock = tmp_path / ".terraform.lock.hcl"
    lock.write_text("provider lock")
    show = tmp_path / "show.txt"
    show.write_text("sanitized terraform show")
    return plan, lock, show


def _plan_fields(plan, lock, show, **over):
    base = dict(
        root="live/production",
        backend_bucket="rag-kb-system-tfstate-production-900340137010",
        state_lineage="lineage-1",
        state_serial="42",
        commit="abcdef1234567890",
        image_digest="us-central1-docker.pkg.dev/p/kb-rag/x@sha256:" + "a" * 64,
        release_phase="dark_no_traffic",
        provider_lock_hash=create_plan._sha256_file(str(lock)),
        plan_hash=create_plan._sha256_file(str(plan)),
        terraform_show_hash=create_plan._sha256_file(str(show)),
        builder_digest="gcr.io/builder@sha256:" + "b" * 64,
        gcs_generation="1700000000000001",
    )
    base.update(over)
    return base


class TestPlanManifest:

    def test_valid_manifest_verifies(self, plan_files):
        plan, lock, show = plan_files
        manifest = create_plan.build_manifest(_plan_fields(plan, lock, show))
        # no lanza
        verify_plan.verify(
            manifest, plan_file=str(plan),
            expected_plan_sha256=manifest["plan_hash"],
            current_state_serial="42",
            expected_commit="abcdef1234567890",
            expected_image_digest=manifest["image_digest"],
            expected_root="live/production",
            provider_lock=str(lock))

    def test_missing_field_rejected_at_build(self, plan_files):
        plan, lock, show = plan_files
        fields = _plan_fields(plan, lock, show)
        del fields["commit"]
        with pytest.raises(ValueError):
            create_plan.build_manifest(fields)

    def test_tampered_manifest_hash_rejected(self, plan_files):
        plan, lock, show = plan_files
        manifest = create_plan.build_manifest(_plan_fields(plan, lock, show))
        manifest["release_phase"] = "full"  # alterado sin recomputar hash
        with pytest.raises(verify_plan.ManifestMismatch):
            verify_plan.verify(
                manifest, plan_file=str(plan),
                expected_plan_sha256=manifest["plan_hash"],
                current_state_serial="42", expected_commit="abcdef1234567890",
                expected_image_digest=manifest["image_digest"],
                expected_root="live/production", provider_lock=str(lock))

    def test_state_drift_rejected(self, plan_files):
        plan, lock, show = plan_files
        manifest = create_plan.build_manifest(_plan_fields(plan, lock, show))
        with pytest.raises(verify_plan.ManifestMismatch, match="drift"):
            verify_plan.verify(
                manifest, plan_file=str(plan),
                expected_plan_sha256=manifest["plan_hash"],
                current_state_serial="43",  # el state avanzó → drift
                expected_commit="abcdef1234567890",
                expected_image_digest=manifest["image_digest"],
                expected_root="live/production", provider_lock=str(lock))

    def test_wrong_root_rejected(self, plan_files):
        plan, lock, show = plan_files
        manifest = create_plan.build_manifest(_plan_fields(plan, lock, show))
        with pytest.raises(verify_plan.ManifestMismatch, match="root"):
            verify_plan.verify(
                manifest, plan_file=str(plan),
                expected_plan_sha256=manifest["plan_hash"],
                current_state_serial="42", expected_commit="abcdef1234567890",
                expected_image_digest=manifest["image_digest"],
                expected_root="live/staging",  # root cruzado
                provider_lock=str(lock))

    def test_modified_plan_file_rejected(self, plan_files):
        plan, lock, show = plan_files
        manifest = create_plan.build_manifest(_plan_fields(plan, lock, show))
        plan.write_bytes(b"DIFFERENT-plan-bytes")  # el binario cambió
        with pytest.raises(verify_plan.ManifestMismatch):
            verify_plan.verify(
                manifest, plan_file=str(plan),
                expected_plan_sha256=manifest["plan_hash"],
                current_state_serial="42", expected_commit="abcdef1234567890",
                expected_image_digest=manifest["image_digest"],
                expected_root="live/production", provider_lock=str(lock))


# ---------------------------------------------------------------------------
# Filtro docs-only del trigger de `main` (plan Tarea 12 Paso 2). Los globs
# deben coincidir EXACTAMENTE con ignored_files en cloud_build.tf y con el
# allowlist del evidence-manifest.
# ---------------------------------------------------------------------------

import fnmatch

_IGNORED_GLOBS = [
    "docs/verification/**",
    "kb-rag-system/Development Docs/**",
    "**/README.md",
]


def _is_docs_only(changed_files):
    """True si TODO el diff cae en los globs ignorados (no debe construir)."""
    def _match(path):
        for g in _IGNORED_GLOBS:
            if g.endswith("/**"):
                prefix = g[:-3]
                if path == prefix or path.startswith(prefix + "/"):
                    return True
            elif fnmatch.fnmatch(path, g):
                return True
        return False
    return all(_match(f) for f in changed_files)


class TestMainTriggerDocsOnlyFilter:

    def test_docs_only_diff_does_not_build(self):
        changed = [
            "docs/verification/handle-ticket/16-production-dark-deploy.md",
            "kb-rag-system/Development Docs/HANDLE_TICKET_RUNBOOK.md",
            "kb-rag-system/README.md",
        ]
        assert _is_docs_only(changed), "un diff docs-only NO debe disparar build"

    def test_code_lock_or_iac_diff_builds(self):
        for changed in (
            ["kb-rag-system/api/main.py"],
            ["kb-rag-system/requirements.lock"],
            ["infra/terraform/live/production/main.tf"],
            ["kb-rag-system/data_pipeline/agent_prompts/extract_inquiries.md"],
        ):
            assert not _is_docs_only(changed), (
                f"{changed} SÍ debe disparar build (no es docs-only)")

    def test_globs_match_terraform_ignored_files(self):
        tf = (Path(__file__).resolve().parent.parent.parent
              / "infra" / "terraform" / "live" / "platform" / "cloud_build.tf")
        body = tf.read_text()
        for g in _IGNORED_GLOBS:
            assert g in body, f"glob {g} ausente en ignored_files de cloud_build.tf"


def _promo_fields(**over):
    base = dict(
        main_sha="cafebabe" * 5,
        image_digest="reg/img@sha256:" + "c" * 64,
        ci_provenance_hash="p" * 64,
        sbom_hash="s" * 64,
        scan_hash="n" * 64,
        staging_revision_hashes="rev-1,rev-2",
        e2e_hash="e" * 64,
        differential_hash="d" * 64,
        rollback_hash="r" * 64,
        g2_approval="APROBADO G2 ...",
        g4_approval="APROBADO G4 ...",
        g5_approval="APROBADO G5 ...",
    )
    base.update(over)
    return base


class TestPromotionAttestation:

    def test_valid_promotion_verifies(self):
        att = create_promo.build_promotion(_promo_fields())
        verify_promo.verify(att, expected_main_sha=att["main_sha"],
                            expected_image_digest=att["image_digest"])

    def test_wrong_sha_rejected(self):
        att = create_promo.build_promotion(_promo_fields())
        with pytest.raises(verify_promo.PromotionRejected, match="main_sha"):
            verify_promo.verify(att, expected_main_sha="deadbeef" * 5,
                                expected_image_digest=att["image_digest"])

    def test_wrong_digest_rejected(self):
        att = create_promo.build_promotion(_promo_fields())
        with pytest.raises(verify_promo.PromotionRejected, match="image_digest"):
            verify_promo.verify(att, expected_main_sha=att["main_sha"],
                                expected_image_digest="reg/img@sha256:" + "0" * 64)

    def test_tampered_attestation_rejected(self):
        att = create_promo.build_promotion(_promo_fields())
        att["scan_hash"] = "0" * 64  # alterado sin recomputar
        with pytest.raises(verify_promo.PromotionRejected):
            verify_promo.verify(att, expected_main_sha=att["main_sha"],
                                expected_image_digest=att["image_digest"])

    def test_missing_evidence_rejected_at_build(self):
        fields = _promo_fields()
        fields["e2e_hash"] = ""
        with pytest.raises(ValueError):
            create_promo.build_promotion(fields)
