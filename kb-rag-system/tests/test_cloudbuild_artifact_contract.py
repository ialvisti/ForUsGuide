"""Supply-chain contracts shared by runtime and E2E image builds."""

import importlib.util
import json
from pathlib import Path
import re
import sys

import pytest


KB_ROOT = Path(__file__).resolve().parent.parent


def test_image_tags_and_evidence_paths_use_the_full_commit_sha() -> None:
    for name in ("cloudbuild.yaml", "cloudbuild.e2e-image.yaml"):
        controller = (KB_ROOT / name).read_text(encoding="utf-8")
        assert "$SHORT_SHA" not in controller, name
        assert "$COMMIT_SHA" in controller, name


def test_secret_scans_are_fresh_and_reproduce_reviewed_filter_configuration() -> None:
    for name in (
        "cloudbuild.yaml", "cloudbuild.test-only.yaml",
        "ci/cloudbuild.verify-local.yaml",
    ):
        controller = (KB_ROOT / name).read_text(encoding="utf-8")
        assert "detect-secrets scan --all-files ." in controller
        assert controller.index("detect-secrets scan --all-files .") < (
            controller.index("python -m pytest -q")
        )
        assert "--no-verify" in controller
        assert "--baseline" not in controller
        for pattern in (
            r"\.venv/.*",
            r"\.pytest_cache/.*",
            r"\.mypy_cache/.*",
            r"\.ruff_cache/.*",
            r"^\.secrets\.baseline$",
            "__pycache__/.*",
            "rag-testing/stress_test_results.*",
        ):
            assert f"--exclude-files '{pattern}'" in controller
        assert "verify_secrets_baseline.py" in controller
        for required in (
            "external-input-secrets.json",
            "../PA",
            '"../External agents"',
            "../docs/verification/handle-ticket/11-incident-drill-template.md",
            "--require-empty",
        ):
            assert required in controller
        assert controller.index("external-input-secrets.json") < (
            controller.index("python -m pytest -q")
        )


def test_e2e_build_resolves_the_remote_digest_and_runs_all_gates() -> None:
    controller = (KB_ROOT / "cloudbuild.e2e-image.yaml").read_text(encoding="utf-8")

    assert "docker image inspect" not in controller
    assert "--metadata-file=/workspace/e2e-build-metadata.json" in controller
    assert "image_summary.fully_qualified_digest" in controller
    for step_id in (
        "smoke-e2e",
        "sbom-e2e",
        "scan-e2e",
        "publish-e2e-evidence",
    ):
        assert f"id: '{step_id}'" in controller
    assert "scripts/verify_container_scan.py" in controller
    assert "scripts/create_e2e_image_manifest.py" in controller
    assert "x-goog-if-generation-match:0" in controller


def test_runtime_build_resolves_the_registry_digest_not_local_docker_state() -> None:
    controller = (KB_ROOT / "cloudbuild.yaml").read_text(encoding="utf-8")

    assert "docker image inspect" not in controller
    assert "gcloud artifacts docker images describe" in controller
    assert "image_summary.fully_qualified_digest" in controller
    assert "scripts/upload_gcs_write_once.sh" in controller


def test_runtime_evidence_uses_the_ticket_ci_authorized_bucket_prefix() -> None:
    controller = (KB_ROOT / "cloudbuild.yaml").read_text(encoding="utf-8")
    platform_iam = (
        KB_ROOT.parent
        / "infra"
        / "terraform"
        / "live"
        / "platform"
        / "pipeline_iam.tf"
    ).read_text(encoding="utf-8")

    assert 'prefix = "runtime/"' in platform_iam
    assert controller.count("runtime/$COMMIT_SHA/") == 3
    assert "ci/$COMMIT_SHA/" not in controller


def test_runtime_evidence_upload_uses_create_only_gcs_api() -> None:
    controller = (KB_ROOT / "cloudbuild.yaml").read_text(encoding="utf-8")
    upload_steps = controller[controller.index("id: 'upload-sbom'"):]
    uploader = (KB_ROOT / "scripts" / "upload_gcs_write_once.sh").read_text(
        encoding="utf-8"
    )

    assert upload_steps.count("scripts/upload_gcs_write_once.sh") == 3
    assert "gcloud storage cp" not in upload_steps
    assert "gsutil " not in upload_steps
    assert "curl " in uploader
    assert "x-goog-if-generation-match: 0" in uploader
    assert "--upload-file" in uploader
    assert "gcloud auth print-access-token" in uploader
    for forbidden in ("storage objects describe", "storage ls", "gsutil"):
        assert forbidden not in uploader


def test_runtime_build_escapes_shell_only_variables_from_cloud_build_substitution() -> None:
    controller = (KB_ROOT / "cloudbuild.yaml").read_text(encoding="utf-8")

    for variable in ("DIGEST", "DIGEST_PART", "REPOSITORY", "SCAN_NAME", "TAG"):
        assert re.search(rf"(?<!\$)\${variable}\b", controller) is None, variable
    assert re.search(r"(?<!\$)\$\(", controller) is None


def test_runtime_sbom_uses_the_distroless_syft_entrypoint() -> None:
    controller = (KB_ROOT / "cloudbuild.yaml").read_text(encoding="utf-8")
    sbom_step = controller[controller.index("id: 'sbom'"):]
    sbom_step = sbom_step[:sbom_step.index("id: 'upload-sbom'")]

    assert "entrypoint: '/syft'" in sbom_step
    assert "entrypoint: sh" not in sbom_step
    assert "spdx-json=/workspace/sbom.spdx.json" in sbom_step
    assert "kb-rag-system:$COMMIT_SHA" in sbom_step


def test_release_controller_candidate_recipe_declares_verifier_and_is_verify_only() -> None:
    controller = (
        KB_ROOT / "ci" / "cloudbuild.release-controller.yaml"
    ).read_text(encoding="utf-8")

    assert (
        "serviceAccount: 'projects/rag-kb-system/serviceAccounts/"
        "ticket-controller-verify@rag-kb-system.iam.gserviceaccount.com'"
    ) in controller
    assert "ticket-controller-build" not in controller
    assert "id: 'verify-controller-source'" in controller
    assert "id: 'build-controller-candidate'" in controller
    assert "id: 'smoke-controller-candidate'" in controller
    assert "--require-hashes" in controller
    assert "requirements-dev.lock" in controller
    for test_file in (
        "tests/test_release_controller.py",
        "tests/test_release_manifests.py",
        "tests/test_terraform_pipeline_iam_contract.py",
        "tests/test_cloudbuild_artifact_contract.py",
        "tests/test_container_contract.py",
        "tests/test_deployment_contract.py",
        "tests/test_secrets_baseline.py",
    ):
        assert test_file in controller
    assert "ruff check" in controller
    for script in (
        "scripts/release_controller.py",
        "scripts/create_evidence_manifest.py",
        "scripts/verify_evidence_manifest.py",
        "scripts/run_staging_gate.py",
    ):
        assert script in controller
    for forbidden in (
        "id: 'push-controller'",
        "gcloud artifacts docker images scan",
        "gcloud artifacts docker images describe",
        "release_controller_digest.txt",
        "release_controller_scan.json",
        "images:",
        "us-central1-docker.pkg.dev/$PROJECT_ID/kb-rag/release-controller",
    ):
        assert forbidden not in controller


def test_dormant_controller_publisher_is_absent_from_candidate_recipes_and_triggers() -> None:
    for recipe in sorted(KB_ROOT.rglob("*.yaml")):
        assert "ticket-controller-build" not in recipe.read_text(encoding="utf-8")

    cloud_build = (
        KB_ROOT.parent / "infra" / "terraform" / "live" / "platform" /
        "cloud_build.tf"
    ).read_text(encoding="utf-8")
    trigger_section = cloud_build.split(
        'resource "google_cloudbuild_trigger"', 1,
    )[1]
    assert "ticket-controller-build" not in trigger_section
    assert "google_service_account.controller_builder" not in trigger_section


def test_verify_local_build_declares_exact_logging_only_verifier() -> None:
    controller = (
        KB_ROOT / "ci" / "cloudbuild.verify-local.yaml"
    ).read_text(encoding="utf-8")
    expected = (
        "serviceAccount: 'projects/rag-kb-system/serviceAccounts/"
        "ticket-controller-verify@rag-kb-system.iam.gserviceaccount.com'"
    )

    assert controller.count("serviceAccount:") == 1
    assert expected in controller
    for forbidden in (
        "kb-rag-runner",
        "900340137010-compute@",
        "@cloudbuild.gserviceaccount.com",
    ):
        assert forbidden not in controller


def test_verify_local_build_denies_adc_and_metadata_to_every_step_and_smoke() -> None:
    controller = (
        KB_ROOT / "ci" / "cloudbuild.verify-local.yaml"
    ).read_text(encoding="utf-8")

    step_count = controller.count("\n  - name:")
    assert step_count == 9
    assert controller.count(
        "GOOGLE_APPLICATION_CREDENTIALS=/workspace/ci-no-google-credentials.json"
    ) >= step_count + 3
    assert controller.count("GCE_METADATA_HOST=127.0.0.1:9") >= step_count + 3
    smoke = controller[controller.index("id: 'container-smoke'"):]
    assert "--env" in smoke
    assert "GOOGLE_APPLICATION_CREDENTIALS=/workspace/ci-no-google-credentials.json" in smoke
    assert "GCE_METADATA_HOST=127.0.0.1:9" in smoke
    assert "scripts/container_smoke.py:/opt/container-smoke.py:ro" in smoke
    assert "'/opt/container-smoke.py'" in smoke
    runtime_smoke = smoke[:smoke.index("id: 'ci-image-build'")]
    assert "--workdir=/app" in runtime_smoke
    assert "PYTHONPATH=/app:/opt/python" in runtime_smoke
    e2e_smoke = controller[controller.index("id: 'e2e-image-build-smoke'"):]
    e2e_smoke = e2e_smoke[:e2e_smoke.index("id: 'release-controller-build-smoke'")]
    assert "--workdir=/app" in e2e_smoke
    assert "PYTHONPATH=/app" in e2e_smoke
    for forbidden in ("docker push", "gcloud run deploy", "gcloud builds submit"):
        assert forbidden not in controller
    assert "x-goog-if-generation-match" not in controller
    for step_id in (
        "container-build", "ci-image-build", "e2e-image-build-smoke",
        "release-controller-build-smoke",
    ):
        assert f"id: '{step_id}'" in controller


def test_runtime_smoke_is_mounted_because_admin_scripts_are_not_in_image() -> None:
    controller = (KB_ROOT / "cloudbuild.yaml").read_text(encoding="utf-8")
    smoke = controller[controller.index("id: 'container-smoke'"):]
    assert "--network=none" in smoke
    assert "scripts/container_smoke.py:/opt/container-smoke.py:ro" in smoke
    assert "'/opt/container-smoke.py'" in smoke
    assert "'scripts/container_smoke.py'" not in smoke
    smoke = smoke[:smoke.index("id: 'push'")]
    assert "--workdir=/app" in smoke
    assert "PYTHONPATH=/app:/opt/python" in smoke


def test_e2e_image_smoke_imports_application_from_app_workdir() -> None:
    controller = (KB_ROOT / "cloudbuild.e2e-image.yaml").read_text(encoding="utf-8")
    smoke = controller[controller.index("id: 'smoke-e2e'"):]
    smoke = smoke[:smoke.index("id: 'sbom-e2e'")]
    assert "--workdir=/app" in smoke
    assert "PYTHONPATH=/app" in smoke


def _load_e2e_manifest_module():
    path = KB_ROOT / "scripts" / "create_e2e_image_manifest.py"
    spec = importlib.util.spec_from_file_location("create_e2e_image_manifest", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_e2e_manifest_binds_passing_scan_sbom_sha_and_digest(tmp_path: Path) -> None:
    module = _load_e2e_manifest_module()
    image_digest = "registry/e2e@sha256:" + "a" * 64
    sbom = tmp_path / "sbom.json"
    sbom.write_text(json.dumps({"spdxVersion": "SPDX-2.3", "packages": [{}]}))
    scan = tmp_path / "scan-policy.json"
    scan.write_text(json.dumps({
        "digest": image_digest,
        "severity_counts": {"HIGH": 1, "MEDIUM": 2},
    }))

    manifest = module.build_manifest(
        commit_sha="b" * 40,
        image_digest=image_digest,
        sbom_path=sbom,
        scan_policy_path=scan,
    )

    assert manifest["status"] == "passed"
    assert manifest["main_sha"] == "b" * 40
    assert manifest["image_digest"] == image_digest
    assert len(manifest["sbom_sha256"]) == 64
    assert len(manifest["scan_policy_sha256"]) == 64


def test_e2e_manifest_rejects_wrong_digest_or_critical_scan(tmp_path: Path) -> None:
    module = _load_e2e_manifest_module()
    image_digest = "registry/e2e@sha256:" + "a" * 64
    sbom = tmp_path / "sbom.json"
    sbom.write_text(json.dumps({"spdxVersion": "SPDX-2.3", "packages": [{}]}))
    scan = tmp_path / "scan-policy.json"
    scan.write_text(json.dumps({
        "digest": "registry/e2e@sha256:" + "c" * 64,
        "severity_counts": {},
    }))
    with pytest.raises(ValueError, match="digest"):
        module.build_manifest(
            commit_sha="b" * 40,
            image_digest=image_digest,
            sbom_path=sbom,
            scan_policy_path=scan,
        )

    scan.write_text(json.dumps({
        "digest": image_digest,
        "severity_counts": {"CRITICAL": 1},
    }))
    with pytest.raises(ValueError, match="CRITICAL"):
        module.build_manifest(
            commit_sha="b" * 40,
            image_digest=image_digest,
            sbom_path=sbom,
            scan_policy_path=scan,
        )
