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
import re
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


def test_evidence_manifest_scripts_exist():
    assert (_SCRIPTS / "create_evidence_manifest.py").is_file()
    assert (_SCRIPTS / "verify_evidence_manifest.py").is_file()


create_evidence = _load("create_evidence_manifest")
verify_evidence = _load("verify_evidence_manifest")
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


@pytest.fixture
def terraform_metadata(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"lineage": "lineage-1", "serial": 42}))
    backend = tmp_path / "backend.json"
    backend.write_text(json.dumps({
        "backend": {
            "type": "gcs",
            "config": {
                "bucket": "rag-kb-system-tfstate-production-900340137010",
            },
        },
    }))
    return state, backend


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
        plan_uri="gs://evidence/plans/build/plan.tfplan#1700000000000001",
        terraform_show_uri="gs://evidence/plans/build/show.txt#1700000000000002",
        promotion_uri="gs://evidence/promotion.json#401",
        promotion_hash="c" * 64,
        evidence_manifest_uri="gs://evidence/evidence.json#402",
        evidence_manifest_hash="d" * 64,
        secret_version_manifest_uri="gs://evidence/secrets.json#403",
        secret_version_manifest_hash="e" * 64,
    )
    base.update(over)
    return base


def _rehash(body, required_fields, hash_field):
    canonical = {key: body.get(key) for key in required_fields}
    body[hash_field] = hashlib.sha256(json.dumps(
        canonical, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def _verify_plan_manifest(manifest, plan, lock, show, **over):
    state = plan.parent / "current-state.json"
    state.write_text(json.dumps({
        "lineage": over.pop("current_lineage", "lineage-1"),
        "serial": over.pop("current_serial", 42),
    }))
    backend = plan.parent / "current-backend.json"
    backend.write_text(json.dumps({
        "backend": {
            "type": "gcs",
            "config": {
                "bucket": over.pop(
                    "current_backend",
                    "rag-kb-system-tfstate-production-900340137010",
                ),
            },
        },
    }))
    expected = dict(
        expected_plan_sha256=create_plan._sha256_file(str(plan)),
        expected_plan_uri=(
            "gs://evidence/plans/build/plan.tfplan#1700000000000001"
        ),
        expected_show_uri=(
            "gs://evidence/plans/build/show.txt#1700000000000002"
        ),
        expected_commit="abcdef1234567890",
        expected_image_digest=(
            "us-central1-docker.pkg.dev/p/kb-rag/x@sha256:" + "a" * 64
        ),
        expected_root="live/production",
        expected_backend_bucket=(
            "rag-kb-system-tfstate-production-900340137010"
        ),
        expected_release_phase="dark_no_traffic",
        expected_provider_lock_sha256=create_plan._sha256_file(str(lock)),
        expected_show_sha256=create_plan._sha256_file(str(show)),
        expected_controller_builder_digest=(
            "gcr.io/builder@sha256:" + "b" * 64
        ),
        expected_promotion_uri="gs://evidence/promotion.json#401",
        expected_promotion_hash="c" * 64,
        expected_evidence_manifest_uri="gs://evidence/evidence.json#402",
        expected_evidence_manifest_hash="d" * 64,
        expected_secret_version_manifest_uri="gs://evidence/secrets.json#403",
        expected_secret_version_manifest_hash="e" * 64,
    )
    expected.update(over)
    verify_plan.verify(
        manifest,
        plan_file=str(plan),
        show_file=str(show),
        current_state_file=str(state),
        backend_metadata_file=str(backend),
        provider_lock=str(lock),
        **expected,
    )


class TestPlanManifest:

    def test_valid_manifest_verifies_every_external_expectation(
            self, plan_files, terraform_metadata):
        plan, lock, show = plan_files
        state, backend = terraform_metadata
        manifest = create_plan.build_manifest(_plan_fields(plan, lock, show))

        verify_plan.verify(
            manifest,
            plan_file=str(plan),
            show_file=str(show),
            expected_plan_sha256=create_plan._sha256_file(str(plan)),
            expected_plan_uri=(
                "gs://evidence/plans/build/plan.tfplan#1700000000000001"
            ),
            expected_show_uri=(
                "gs://evidence/plans/build/show.txt#1700000000000002"
            ),
            current_state_file=str(state),
            backend_metadata_file=str(backend),
            expected_commit="abcdef1234567890",
            expected_image_digest=manifest["image_digest"],
            expected_root="live/production",
            expected_backend_bucket=(
                "rag-kb-system-tfstate-production-900340137010"
            ),
            expected_release_phase="dark_no_traffic",
            provider_lock=str(lock),
            expected_provider_lock_sha256=create_plan._sha256_file(str(lock)),
            expected_show_sha256=create_plan._sha256_file(str(show)),
            expected_controller_builder_digest=(
                "gcr.io/builder@sha256:" + "b" * 64
            ),
            expected_promotion_uri="gs://evidence/promotion.json#401",
            expected_promotion_hash="c" * 64,
            expected_evidence_manifest_uri=(
                "gs://evidence/evidence.json#402"
            ),
            expected_evidence_manifest_hash="d" * 64,
            expected_secret_version_manifest_uri=(
                "gs://evidence/secrets.json#403"
            ),
            expected_secret_version_manifest_hash="e" * 64,
        )

    def test_valid_manifest_verifies(self, plan_files):
        plan, lock, show = plan_files
        manifest = create_plan.build_manifest(_plan_fields(plan, lock, show))
        _verify_plan_manifest(manifest, plan, lock, show)

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
            _verify_plan_manifest(manifest, plan, lock, show)

    def test_production_manifest_binds_promotion_evidence_and_secret_inputs(
            self, plan_files):
        plan, lock, show = plan_files
        release_inputs = {
            "promotion_uri": "gs://evidence/promotion.json#401",
            "promotion_hash": "c" * 64,
            "evidence_manifest_uri": "gs://evidence/evidence.json#402",
            "evidence_manifest_hash": "d" * 64,
            "secret_version_manifest_uri": "gs://evidence/secrets.json#403",
            "secret_version_manifest_hash": "e" * 64,
        }
        manifest = create_plan.build_manifest(
            _plan_fields(plan, lock, show, **release_inputs),
        )

        _verify_plan_manifest(
            manifest, plan, lock, show,
            **{f"expected_{key}": value
               for key, value in release_inputs.items()},
        )

    @pytest.mark.parametrize(
        "changes",
        [
            {"promotion_uri": "gs://evidence/promotion.json#999"},
            {"promotion_hash": "0" * 64},
            {"evidence_manifest_uri": "gs://evidence/evidence.json#999"},
            {"evidence_manifest_hash": "1" * 64},
            {
                "secret_version_manifest_uri":
                    "gs://evidence/secrets.json#999",
            },
            {"secret_version_manifest_hash": "2" * 64},
        ],
    )
    def test_self_consistent_release_input_swap_is_rejected(
            self, plan_files, changes):
        plan, lock, show = plan_files
        manifest = create_plan.build_manifest(_plan_fields(plan, lock, show))
        manifest.update(changes)
        _rehash(manifest, create_plan.REQUIRED_FIELDS, "manifest_hash")

        with pytest.raises(verify_plan.ManifestMismatch):
            _verify_plan_manifest(manifest, plan, lock, show)

    def test_production_rejects_generationless_release_input(
            self, plan_files):
        plan, lock, show = plan_files
        fields = _plan_fields(
            plan, lock, show,
            promotion_uri="gs://evidence/promotion.json",
        )

        with pytest.raises(ValueError, match="generation"):
            create_plan.build_manifest(fields)


@pytest.fixture
def evidence_files(tmp_path):
    files = {}
    for index, name in enumerate((
        "ci_provenance", "sbom", "scan", "staging_revisions", "e2e",
        "differential", "rollback",
    ), start=1):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps({"artifact": name, "version": index}))
        files[name] = path
    return files


def _evidence_fields(files, **over):
    fields = dict(
        evidence_sha="1" * 40,
        main_sha="2" * 40,
        image_digest="reg/img@sha256:" + "3" * 64,
        controller_builder_digest="python@sha256:" + "4" * 64,
        g2_approval_hash="5" * 64,
        g4_approval_hash="6" * 64,
        g5_approval_hash="7" * 64,
    )
    for index, (name, path) in enumerate(files.items(), start=101):
        fields[f"{name}_uri"] = (
            f"gs://release-evidence/{name}.json#{index}"
        )
        fields[f"{name}_hash"] = hashlib.sha256(path.read_bytes()).hexdigest()
    fields.update(over)
    return fields


class TestEvidenceManifest:

    def test_valid_evidence_verifies_artifacts_and_expected_lineage(
            self, evidence_files):
        manifest = create_evidence.build_manifest(
            _evidence_fields(evidence_files),
        )

        verify_evidence.verify(
            manifest,
            expected_manifest_hash=manifest["manifest_hash"],
            expected_evidence_sha="1" * 40,
            expected_main_sha="2" * 40,
            expected_image_digest="reg/img@sha256:" + "3" * 64,
            expected_controller_builder_digest="python@sha256:" + "4" * 64,
            artifact_files={
                name: str(path) for name, path in evidence_files.items()
            },
        )

    def test_approval_hashes_come_from_real_gate_rows(self, tmp_path):
        approvals = tmp_path / "approvals.md"
        approvals.write_text(
            "| Gate | Texto exacto | Usuario | Rol | Fecha | Alcance | Evidencia |\n"
            "|---|---|---|---|---|---|---|\n"
            "| G2 | APROBADO G2 staging exacto | alice | owner | t | a | e |\n"
            "| G4 | APROBADO G4 e2e exacto | bob | owner | t | a | e |\n"
            "| G5 | APROBADO G5 merge exacto | carol | owner | t | a | e |\n"
        )

        hashes = create_evidence.load_approval_hashes(str(approvals))

        assert set(hashes) == {
            "g2_approval_hash", "g4_approval_hash", "g5_approval_hash",
        }
        assert all(len(value) == 64 for value in hashes.values())

    def test_generationless_artifact_uri_is_rejected(self, evidence_files):
        fields = _evidence_fields(
            evidence_files,
            e2e_uri="gs://release-evidence/e2e.json",
        )
        with pytest.raises(ValueError, match="generation"):
            create_evidence.build_manifest(fields)

    def test_verifier_rejects_self_consistent_generationless_artifact(
            self, evidence_files):
        manifest = create_evidence.build_manifest(
            _evidence_fields(evidence_files),
        )
        manifest["rollback_uri"] = "gs://release-evidence/rollback.json"
        _rehash(
            manifest, create_evidence.REQUIRED_FIELDS, "manifest_hash",
        )

        with pytest.raises(verify_evidence.EvidenceMismatch, match="generation"):
            verify_evidence.verify(
                manifest,
                expected_manifest_hash=manifest["manifest_hash"],
                expected_evidence_sha=manifest["evidence_sha"],
                expected_main_sha=manifest["main_sha"],
                expected_image_digest=manifest["image_digest"],
                expected_controller_builder_digest=(
                    manifest["controller_builder_digest"]
                ),
            )

    def test_artifact_file_hash_mismatch_is_rejected(self, evidence_files):
        manifest = create_evidence.build_manifest(
            _evidence_fields(evidence_files),
        )
        evidence_files["rollback"].write_text("tampered")
        with pytest.raises(verify_evidence.EvidenceMismatch, match="rollback"):
            verify_evidence.verify(
                manifest,
                expected_manifest_hash=manifest["manifest_hash"],
                expected_evidence_sha=manifest["evidence_sha"],
                expected_main_sha=manifest["main_sha"],
                expected_image_digest=manifest["image_digest"],
                expected_controller_builder_digest=(
                    manifest["controller_builder_digest"]
                ),
                artifact_files={
                    name: str(path) for name, path in evidence_files.items()
                },
            )

    def test_cli_creates_real_evidence_manifest(
            self, evidence_files, tmp_path):
        approvals = tmp_path / "approvals.md"
        approvals.write_text(
            "| Gate | Texto exacto | Usuario | Rol | Fecha | Alcance | Evidencia |\n"
            "|---|---|---|---|---|---|---|\n"
            "| G2 | APROBADO G2 staging | alice | owner | t | a | e |\n"
            "| G4 | APROBADO G4 e2e | bob | owner | t | a | e |\n"
            "| G5 | APROBADO G5 merge | carol | owner | t | a | e |\n"
        )
        out = tmp_path / "evidence_manifest.json"
        argv = [
            "--evidence-sha", "1" * 40,
            "--main-sha", "2" * 40,
            "--image-digest", "reg/img@sha256:" + "3" * 64,
            "--controller-builder-digest", "python@sha256:" + "4" * 64,
            "--approvals-file", str(approvals),
            "--out", str(out),
        ]
        fields = _evidence_fields(evidence_files)
        for name, path in evidence_files.items():
            argv.extend([
                f"--{name.replace('_', '-')}-uri", fields[f"{name}_uri"],
                f"--{name.replace('_', '-')}-file", str(path),
            ])

        assert create_evidence.main(argv) == 0
        created = json.loads(out.read_text())
        assert created["manifest_hash"]
        assert created["rollback_uri"].endswith("#107")

    def test_verify_cli_rejects_wrong_expected_manifest_hash(
            self, evidence_files, tmp_path):
        manifest = create_evidence.build_manifest(
            _evidence_fields(evidence_files),
        )
        path = tmp_path / "evidence.json"
        path.write_text(json.dumps(manifest))
        argv = [
            "--manifest", str(path),
            "--expected-manifest-hash", "0" * 64,
            "--expected-evidence-sha", manifest["evidence_sha"],
            "--expected-main-sha", manifest["main_sha"],
            "--expected-image-digest", manifest["image_digest"],
            "--expected-controller-builder-digest",
            manifest["controller_builder_digest"],
        ]

        assert verify_evidence.main(argv) == 1


class TestPlanManifestExternalExpectations:

    def test_state_drift_rejected(self, plan_files):
        plan, lock, show = plan_files
        manifest = create_plan.build_manifest(_plan_fields(plan, lock, show))
        with pytest.raises(verify_plan.ManifestMismatch, match="drift"):
            _verify_plan_manifest(
                manifest, plan, lock, show,
                current_serial=43,  # el state avanzó → drift
            )

    def test_wrong_root_rejected(self, plan_files):
        plan, lock, show = plan_files
        manifest = create_plan.build_manifest(_plan_fields(plan, lock, show))
        with pytest.raises(verify_plan.ManifestMismatch, match="root"):
            _verify_plan_manifest(
                manifest, plan, lock, show,
                expected_root="live/staging",  # root cruzado
            )

    def test_modified_plan_file_rejected(self, plan_files):
        plan, lock, show = plan_files
        manifest = create_plan.build_manifest(_plan_fields(plan, lock, show))
        plan.write_bytes(b"DIFFERENT-plan-bytes")  # el binario cambió
        with pytest.raises(verify_plan.ManifestMismatch):
            _verify_plan_manifest(
                manifest, plan, lock, show,
                expected_plan_sha256=manifest["plan_hash"],
            )

    @pytest.mark.parametrize(
        ("changes", "message"),
        [
            ({"backend_bucket": "other-state-bucket"}, "backend"),
            ({"release_phase": "full"}, "phase"),
            ({"state_lineage": "other-lineage"}, "lineage"),
            ({"state_serial": "99"}, "serial"),
            ({
                "plan_uri": "gs://evidence/plans/build/plan.tfplan#999",
                "gcs_generation": "999",
            }, "generation"),
            ({"terraform_show_hash": "0" * 64}, "show"),
            ({"provider_lock_hash": "0" * 64}, "provider-lock"),
            ({
                "builder_digest": "gcr.io/other@sha256:" + "0" * 64,
            }, "builder"),
        ],
    )
    def test_self_consistent_tampering_loses_to_external_expectations(
            self, plan_files, changes, message):
        plan, lock, show = plan_files
        manifest = create_plan.build_manifest(_plan_fields(plan, lock, show))
        manifest.update(changes)
        _rehash(manifest, create_plan.REQUIRED_FIELDS, "manifest_hash")

        with pytest.raises(verify_plan.ManifestMismatch, match=message):
            _verify_plan_manifest(manifest, plan, lock, show)


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


def _promotion(evidence_files):
    evidence = create_evidence.build_manifest(_evidence_fields(evidence_files))
    evidence_uri = "gs://release-evidence/evidence.json#301"
    builder = "python@sha256:" + "8" * 64
    attestation = create_promo.build_promotion_from_evidence(
        evidence,
        evidence_manifest_uri=evidence_uri,
        controller_builder_digest=builder,
    )
    return evidence, evidence_uri, builder, attestation


def _verify_promotion(attestation, evidence, evidence_uri, builder, **over):
    expected = dict(
        expected_attestation_hash=attestation["attestation_hash"],
        expected_evidence_manifest_uri=evidence_uri,
        expected_evidence_manifest_hash=evidence["manifest_hash"],
        expected_main_sha=evidence["main_sha"],
        expected_image_digest=evidence["image_digest"],
        expected_controller_builder_digest=builder,
        expected_evidence_controller_builder_digest=(
            evidence["controller_builder_digest"]
        ),
    )
    expected.update(over)
    verify_promo.verify(
        attestation,
        evidence_manifest=evidence,
        **expected,
    )


class TestPromotionAttestation:

    def test_promotion_copies_and_verifies_exact_evidence_lineage(
            self, evidence_files):
        evidence = create_evidence.build_manifest(
            _evidence_fields(evidence_files),
        )
        evidence_uri = "gs://release-evidence/evidence.json#301"
        builder = "python@sha256:" + "8" * 64

        att = create_promo.build_promotion_from_evidence(
            evidence,
            evidence_manifest_uri=evidence_uri,
            controller_builder_digest=builder,
        )

        verify_promo.verify(
            att,
            evidence_manifest=evidence,
            expected_attestation_hash=att["attestation_hash"],
            expected_evidence_manifest_uri=evidence_uri,
            expected_evidence_manifest_hash=evidence["manifest_hash"],
            expected_main_sha=evidence["main_sha"],
            expected_image_digest=evidence["image_digest"],
            expected_controller_builder_digest=builder,
            expected_evidence_controller_builder_digest=(
                evidence["controller_builder_digest"]
            ),
        )

    def test_valid_promotion_verifies(self, evidence_files):
        evidence, uri, builder, att = _promotion(evidence_files)
        _verify_promotion(att, evidence, uri, builder)

    def test_wrong_sha_rejected(self, evidence_files):
        evidence, uri, builder, att = _promotion(evidence_files)
        with pytest.raises(verify_promo.PromotionRejected, match="main_sha"):
            _verify_promotion(
                att, evidence, uri, builder,
                expected_main_sha="deadbeef" * 5,
            )

    def test_wrong_digest_rejected(self, evidence_files):
        evidence, uri, builder, att = _promotion(evidence_files)
        with pytest.raises(verify_promo.PromotionRejected, match="image_digest"):
            _verify_promotion(
                att, evidence, uri, builder,
                expected_image_digest="reg/img@sha256:" + "0" * 64,
            )

    def test_tampered_attestation_rejected(self, evidence_files):
        evidence, uri, builder, att = _promotion(evidence_files)
        att["scan_hash"] = "0" * 64  # alterado sin recomputar
        with pytest.raises(verify_promo.PromotionRejected):
            _verify_promotion(att, evidence, uri, builder)

    def test_missing_evidence_rejected_at_build(self, evidence_files):
        fields = _evidence_fields(evidence_files)
        fields["e2e_hash"] = ""
        with pytest.raises(ValueError):
            create_evidence.build_manifest(fields)

    def test_self_consistent_promotion_tampering_rejected_by_evidence(
            self, evidence_files):
        evidence, uri, builder, att = _promotion(evidence_files)
        att["e2e_hash"] = "0" * 64
        _rehash(att, create_promo.REQUIRED_FIELDS, "attestation_hash")

        with pytest.raises(verify_promo.PromotionRejected, match="e2e_hash"):
            _verify_promotion(
                att, evidence, uri, builder,
                expected_attestation_hash=att["attestation_hash"],
            )

    def test_self_consistent_evidence_tampering_rejected_by_expected_hash(
            self, evidence_files):
        evidence, uri, builder, _att = _promotion(evidence_files)
        expected_evidence_hash = evidence["manifest_hash"]
        evidence["rollback_uri"] = (
            "gs://release-evidence/rollback.json#999"
        )
        _rehash(evidence, create_evidence.REQUIRED_FIELDS, "manifest_hash")
        att = create_promo.build_promotion_from_evidence(
            evidence,
            evidence_manifest_uri=uri,
            controller_builder_digest=builder,
        )

        with pytest.raises(verify_promo.PromotionRejected, match="evidence"):
            _verify_promotion(
                att, evidence, uri, builder,
                expected_evidence_manifest_hash=expected_evidence_hash,
            )

    def test_generationless_evidence_uri_rejected(self, evidence_files):
        evidence = create_evidence.build_manifest(
            _evidence_fields(evidence_files),
        )
        with pytest.raises(ValueError, match="generation"):
            create_promo.build_promotion_from_evidence(
                evidence,
                evidence_manifest_uri="gs://release-evidence/evidence.json",
                controller_builder_digest="python@sha256:" + "8" * 64,
            )

    def test_promotion_builder_rejects_generationless_artifact_in_evidence(
            self, evidence_files):
        evidence = create_evidence.build_manifest(
            _evidence_fields(evidence_files),
        )
        evidence["e2e_uri"] = "gs://release-evidence/e2e.json"
        _rehash(evidence, create_evidence.REQUIRED_FIELDS, "manifest_hash")

        with pytest.raises(ValueError, match="generation"):
            create_promo.build_promotion_from_evidence(
                evidence,
                evidence_manifest_uri=(
                    "gs://release-evidence/evidence.json#301"
                ),
                controller_builder_digest="python@sha256:" + "8" * 64,
            )


class TestPrivilegedBuildSource:

    _ROOT = Path(__file__).resolve().parent.parent
    _PYTHON = (
        "python:3.12-slim@sha256:"
        "64695412729fbe8cf054511723820c82bbe5a077d4a6b4070cd4a7225d3422ce"
    )
    _TERRAFORM = (
        "hashicorp/terraform:1.9@sha256:"
        "18f9986038bbaf02cf49db9c09261c778161c51dcc7fb7e355ae8938459428cd"
    )
    _CLOUDSDK = (
        "gcr.io/google.com/cloudsdktool/google-cloud-cli@sha256:"
        "38132a268745db5a1dc2ebfecfe6f935d75de281dddc6922f0fe3780c5552b81"
    )
    _GIT = (
        "gcr.io/cloud-builders/git@sha256:"
        "739e7bda8456932210aaf911d44b8ce91eeca84934b851c340a77efc4dd948ec"
    )

    def _read(self, name):
        return (self._ROOT / name).read_text()

    def test_all_controller_builders_are_immutable_and_not_substitutable(self):
        for filename in (
            "cloudbuild.terraform-plan.yaml",
            "cloudbuild.terraform-apply.yaml",
            "cloudbuild.staging-attest.yaml",
            "cloudbuild.evidence-manifest.yaml",
            "cloudbuild.test-only.yaml",
            "cloudbuild.e2e-image.yaml",
        ):
            body = self._read(filename)
            assert "PINNED_AT_BOOTSTRAP" not in body
            assert "_TERRAFORM_IMAGE" not in body
            assert "_PYTHON_IMAGE" not in body
            for image in re.findall(r"(?m)^\s*- name: ['\"]?([^'\"\n]+)", body):
                assert "@sha256:" in image, f"{filename}: builder mutable {image}"

    def test_plan_uses_real_state_backend_plan_path_and_object_generations(self):
        body = self._read("cloudbuild.terraform-plan.yaml")
        assert self._TERRAFORM in body
        assert self._CLOUDSDK in body
        assert self._PYTHON in body
        assert "-out=/workspace/plan.tfplan" in body
        assert "terraform state pull > /workspace/prior_state.json" in body
        assert ".terraform/terraform.tfstate" in body
        assert "--state-file /workspace/prior_state.json" in body
        assert "--backend-metadata-file /workspace/backend_metadata.json" in body
        assert "--plan-uri \"$(cat /workspace/plan_uri.txt)\"" in body
        assert "--show-uri \"$(cat /workspace/show_uri.txt)\"" in body
        assert "gcloud storage objects describe" in body
        assert body.index("id: 'upload-plan-show'") < body.index("id: 'manifest'")
        for placeholder in ("echo unknown", "echo 0", '"pending"'):
            assert placeholder not in body

    def test_apply_rechecks_drift_show_and_only_applies_binary_plan(self):
        body = self._read("cloudbuild.terraform-apply.yaml")
        assert self._TERRAFORM in body
        assert self._CLOUDSDK in body
        assert "terraform state pull > /workspace/current_state_serial.txt" in body
        assert "terraform show -no-color /workspace/plan.tfplan" in body
        assert "--current-state-file /workspace/current_state_serial.txt" in body
        for option in (
            "--expected-plan-uri", "--expected-show-uri",
            "--expected-backend-bucket", "--expected-release-phase",
            "--expected-provider-lock-sha256", "--expected-show-sha256",
            "--expected-controller-builder-digest",
        ):
            assert option in body
        assert "terraform plan" not in body
        assert body.count("terraform apply") == 1
        assert "terraform apply -input=false -auto-approve /workspace/plan.tfplan" in body

    def test_production_plan_and_apply_require_promotion_evidence(self):
        for filename in (
            "cloudbuild.terraform-plan.yaml",
            "cloudbuild.terraform-apply.yaml",
        ):
            body = self._read(filename)
            for token in (
                "_PROMOTION_URI", "_PROMOTION_HASH",
                "_EVIDENCE_MANIFEST_URI", "_EVIDENCE_MANIFEST_HASH",
                "verify_promotion_manifest.py",
            ):
                assert token in body, f"{filename}: falta {token}"
            assert "#<generation>" in body or "#generation" in body

    def test_evidence_build_creates_manifest_before_write_once_upload(self):
        body = self._read("cloudbuild.evidence-manifest.yaml")
        assert self._GIT in body
        assert self._CLOUDSDK in body
        assert self._PYTHON in body
        assert "scripts/create_evidence_manifest.py" in body
        assert "scripts/verify_evidence_manifest.py" in body
        assert "--approvals-file" in body
        for artifact in (
            "CI_PROVENANCE", "SBOM", "SCAN", "STAGING_REVISIONS",
            "E2E", "DIFFERENTIAL", "ROLLBACK",
        ):
            assert f"_{artifact}_URI" in body
        assert body.index("scripts/create_evidence_manifest.py") \
            < body.index("evidence_manifest.json \\")

    def test_staging_attestation_fetches_verifies_creates_then_uploads(self):
        body = self._read("cloudbuild.staging-attest.yaml")
        assert self._CLOUDSDK in body
        assert self._PYTHON in body
        assert "verify_evidence_manifest.py" in body
        assert "create_promotion_manifest.py" in body
        assert "_EVIDENCE_MANIFEST_HASH" in body
        assert body.index("id: 'fetch-evidence'") < body.index("id: 'attest'")
        assert body.index("id: 'attest'") < body.index("id: 'upload-promotion'")

    def test_test_only_compares_detect_secrets_candidate_fail_closed(self):
        body = self._read("cloudbuild.test-only.yaml")
        assert self._PYTHON in body
        assert self._CLOUDSDK in body
        assert "> /workspace/candidate-secrets.baseline" in body
        assert "scripts/verify_secrets_baseline.py" in body
        assert "--approved .secrets.baseline" in body
        assert "--candidate /workspace/candidate-secrets.baseline" in body
