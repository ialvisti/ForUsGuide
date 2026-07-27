"""Regression contracts for the production image and GCP release topology.

These tests intentionally inspect the deployable artifacts rather than their
documentation.  A comment saying that a control exists must never make the
gate pass when the corresponding Docker/Cloud Build/Terraform construct is
absent.
"""

from __future__ import annotations

import re
import importlib.util
import sys
from pathlib import Path

import pytest


KB_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = KB_ROOT.parent
TF_ROOT = REPO_ROOT / "infra" / "terraform"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tool_image(name: str) -> str:
    for raw_line in _read(KB_ROOT / "ci" / "tool-images.env").splitlines():
        if raw_line.startswith(f"{name}="):
            return raw_line.split("=", 1)[1]
    raise AssertionError(f"{name} is missing from ci/tool-images.env")


def test_router_keeps_api_key_in_dom_memory_only() -> None:
    router = _read(KB_ROOT / "ui" / "router.html")

    assert "localStorage" not in router
    assert "sessionStorage" not in router
    assert "API_KEY_STORAGE" not in router
    assert "const apiKey = document.getElementById('apiKey').value.trim();" in router


def test_runtime_dockerfile_uses_pinned_base_and_hashed_runtime_lock() -> None:
    dockerfile = _read(KB_ROOT / "Dockerfile")
    pinned_base = _tool_image("PYTHON_RUNTIME_BUILDER_IMAGE")

    assert f"FROM {pinned_base}" in dockerfile
    assert "COPY requirements.lock" in dockerfile
    assert "--require-hashes -r requirements.lock" in dockerfile
    assert "requirements.txt" not in dockerfile
    assert "pip install --no-cache-dir --upgrade pip" not in dockerfile


def test_runtime_dockerfile_finishes_on_a_pinned_distroless_nonroot_stage() -> None:
    dockerfile = _read(KB_ROOT / "Dockerfile")
    builder = _tool_image("PYTHON_RUNTIME_BUILDER_IMAGE")
    runtime = _tool_image("PYTHON_DISTROLESS_RUNTIME_IMAGE")

    assert f"FROM {builder} AS python-runtime" in dockerfile
    assert f"FROM {runtime}" in dockerfile
    assert "COPY --from=python-runtime /usr/local /usr/local" in dockerfile
    assert "ENTRYPOINT []" in dockerfile
    assert "USER 65532" in dockerfile
    assert dockerfile.index(f"FROM {runtime}") > dockerfile.index(
        "--require-hashes -r requirements.lock"
    )


def test_dev_lock_is_self_contained_for_a_fresh_python_image() -> None:
    """pip-tools bootstrap dependencies must not be hidden by the generator."""
    controller = _read(KB_ROOT / "ci" / "cloudbuild.generate-locks.yaml")
    dev_lock = _read(KB_ROOT / "requirements-dev.lock")

    assert "--allow-unsafe" in controller
    assert "--require-hashes" in controller
    assert "-r requirements-dev.lock" in controller
    assert "pip install --no-cache-dir 'pip-tools" not in controller
    assert "dir: 'kb-rag-system'" in controller
    assert "cp requirements.lock /workspace/requirements.lock" in controller
    assert "cp requirements-dev.lock /workspace/requirements-dev.lock" in controller
    assert "- 'requirements.lock'" in controller
    assert "- 'requirements-dev.lock'" in controller
    assert "python -m venv /tmp/lock-verification" in controller
    assert "/tmp/lock-verification/bin/python -m pip install" in controller  # noqa: S108
    assert re.search(r"(?m)^pip==", dev_lock)
    assert re.search(r"(?m)^setuptools==", dev_lock)
    assert re.search(r"(?m)^types-protobuf==", dev_lock)
    assert "packages were not pinned" not in dev_lock


def test_ci_container_scan_fails_closed_and_enforces_severity() -> None:
    cloudbuild = _read(KB_ROOT / "cloudbuild.yaml")

    assert "_PYTHON_IMAGE" not in cloudbuild
    assert "_SYFT_IMAGE" not in cloudbuild
    for image in re.findall(r"(?m)^\s*- name: ['\"]?([^'\"\n]+)", cloudbuild):
        assert "@sha256:" in image, f"CI builder is mutable: {image}"
    assert "gcloud artifacts docker images scan" in cloudbuild
    assert "|| true" not in cloudbuild
    assert "scripts/verify_container_scan.py" in cloudbuild
    assert (KB_ROOT / "scripts" / "verify_container_scan.py").is_file()


def _load_scan_verifier():
    path = KB_ROOT / "scripts" / "verify_container_scan.py"
    spec = importlib.util.spec_from_file_location("verify_container_scan", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_container_scan_rejects_critical_without_exception(tmp_path: Path) -> None:
    verifier = _load_scan_verifier()
    digest = "registry/image@sha256:" + "a" * 64
    findings = [
        {"vulnerability": {"shortDescription": "CVE-2026-1000", "effectiveSeverity": "CRITICAL"}}
    ]

    with pytest.raises(verifier.ScanRejected, match="CRITICAL"):
        verifier.verify(
            findings,
            digest=digest,
        )


def test_container_scan_requires_digest_and_cve_scoped_g5v_for_high() -> None:
    verifier = _load_scan_verifier()
    digest = "registry/image@sha256:" + "b" * 64
    findings = [
        {"vulnerability": {"shortDescription": "CVE-2026-2000", "effectiveSeverity": "HIGH"}}
    ]

    with pytest.raises(verifier.ScanRejected, match="G5V"):
        verifier.verify(
            findings,
            digest=digest,
        )

    with pytest.raises(verifier.ScanRejected, match="G5V"):
        verifier.verify(
            findings,
            digest="registry/image@sha256:" + "c" * 64,
        )


def test_terraform_neutralizes_the_existing_main_deploy_trigger() -> None:
    cloud_build_tf = _read(TF_ROOT / "live" / "platform" / "cloud_build.tf")
    imports_tf = _read(TF_ROOT / "live" / "platform" / "imports.tf")

    assert 'owner = "ialvisti"' in cloud_build_tf
    assert 'name  = "ForUsGuide"' in cloud_build_tf
    assert 'name            = "deploy-kb-rag-system"' in cloud_build_tf
    assert "google_cloudbuild_trigger.main_canonical" in imports_tf
    assert "c2126528-7cd3-4063-9214-5eb82e9f76a6" in imports_tf


def test_terraform_declares_all_privileged_manual_triggers() -> None:
    cloud_build_tf = _read(TF_ROOT / "live" / "platform" / "cloud_build.tf")
    required_names = {
        "handle-ticket-platform-plan",
        "handle-ticket-platform-apply",
        "handle-ticket-staging-plan",
        "handle-ticket-staging-apply",
        "handle-ticket-production-plan",
        "handle-ticket-production-apply",
        "handle-ticket-staging-attest",
        "handle-ticket-evidence-manifest",
        "handle-ticket-test-only",
        "handle-ticket-e2e-image",
    }

    for trigger_name in required_names:
        assert trigger_name in cloud_build_tf, f"missing trigger {trigger_name}"
    assert cloud_build_tf.count("approval_required = true") >= 4


def test_environment_module_preserves_core_env_and_models_traffic() -> None:
    cloud_run_tf = _read(TF_ROOT / "modules" / "ticket_environment" / "cloud_run.tf")
    variables_tf = _read(TF_ROOT / "modules" / "ticket_environment" / "variables.tf")

    assert 'variable "producer_core_env"' in variables_tf
    assert "for_each = var.producer_core_env" in cloud_run_tf
    assert re.search(r"\btraffic\s*\{", cloud_run_tf)
    assert "dark_no_traffic" in cloud_run_tf


def test_platform_brokers_database_scoped_iam_for_environment_runtimes() -> None:
    iam_tf = _read(TF_ROOT / "live" / "platform" / "runtime_project_iam.tf")
    containers_tf = _read(TF_ROOT / "live" / "platform" / "environment_containers.tf")
    pipeline_iam = _read(TF_ROOT / "live" / "platform" / "pipeline_iam.tf")

    assert 'resource "google_firestore_database" "environment"' in containers_tf
    assert 'resource "google_project_iam_member" "runtime_firestore"' in iam_tf
    assert (
        'expression  = "resource.name == '
        '\\"projects/${var.project_id}/databases/${each.value.database}\\""'
    ) in iam_tf
    assert '"datastore.entities.get"' not in pipeline_iam
    assert '"roles/iam.securityAdmin"' not in pipeline_iam


def test_environment_module_declares_secret_containers_and_staging_e2e_job() -> None:
    module_text = "\n".join(
        _read(path) for path in (TF_ROOT / "modules" / "ticket_environment").glob("*.tf")
    )

    assert "google_secret_manager_secret" in module_text
    assert re.search(r'resource\s+"google_cloud_run_v2_job"\s+"e2e"', module_text)


def test_remote_verification_controller_cannot_deploy_or_publish() -> None:
    controller = KB_ROOT / "ci" / "cloudbuild.verify-local.yaml"
    assert controller.is_file()
    text = _read(controller)

    for required_step in (
        "python-gates",
        "terraform-gates",
        "firestore-emulator-contract",
        "firestore-emulator-tests",
        "container-build",
        "container-smoke",
    ):
        assert f"id: '{required_step}'" in text

    for forbidden in (
        "terraform apply",
        "gcloud run",
        "docker push",
        "gsutil ",
        "gcloud storage",
        "images:",
        "artifacts:",
    ):
        assert forbidden not in text

    image_references = re.findall(
        r"^\s*-\s+name:\s*'([^']+)'", text, re.MULTILINE
    )
    assert image_references
    assert all("@sha256:" in image for image in image_references)


def test_cloud_build_source_upload_is_default_deny() -> None:
    ignore_lines = [
        line.strip()
        for line in _read(REPO_ROOT / ".gcloudignore").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert ignore_lines[0] == "**"
    assert {
        "!.gcloudignore",
        "!kb-rag-system/",
        "!kb-rag-system/**",
        "!infra/",
        "!infra/terraform/",
        "!infra/terraform/**",
        "!PA/",
        "!PA/**/",
        "!docs/",
        "!docs/verification/",
        "!docs/verification/handle-ticket/",
        "!docs/verification/handle-ticket/11-incident-drill-template.md",
        "!External agents/",
        "!External agents/Inquiry Extraction & Required-Data Builder agent .md",
        "!External agents/Knowledge Question Inquiry Generator.md",
        "!External agents/Generate Response Body Builder.md",
        "!External agents/Forusbots field mapper.md",
    }.issubset(ignore_lines)
    reviewed_pa_json = {
        f"!{path.relative_to(REPO_ROOT).as_posix()}"
        for path in (REPO_ROOT / "PA").rglob("*.json")
    }
    assert reviewed_pa_json
    assert reviewed_pa_json.issubset(ignore_lines)
    assert "!PA/**" not in ignore_lines
    assert "!PA/**/*.json" not in ignore_lines
    assert "!docs/**" not in ignore_lines
    assert "!External agents/**" not in ignore_lines
    assert not any(
        line.startswith("!ticket-handler-planning") for line in ignore_lines
    )
    assert {
        "**/*.tfstate",
        "**/*.tfstate.*",
        "**/*.tfplan",
        "**/*.pem",
        "**/*.key",
        "**/*credentials*.json",
    }.issubset(ignore_lines)
    assert {
        "kb-rag-system/rag-testing/**",
        "!kb-rag-system/rag-testing/ground_truth.py",
        "!kb-rag-system/rag-testing/ticket_differential.py",
        "!kb-rag-system/rag-testing/ticket_differential_thresholds.json",
        "kb-rag-system/STRESS_TEST_COMPARISON_REPORT.md",
        "kb-rag-system/Development Docs/**",
        "!kb-rag-system/Development Docs/HANDLE_TICKET_RUNBOOK.md",
    }.issubset(ignore_lines)


def test_terraform_lock_controller_preserves_all_three_root_locks() -> None:
    controller = _read(KB_ROOT / "ci" / "cloudbuild.generate-terraform-locks.yaml")

    for root in ("platform", "staging", "production"):
        unique_artifact = f"terraform-locks/{root}.terraform.lock.hcl"
        assert re.search(
            rf'infra/terraform/live/{root}/\.terraform\.lock\.hcl"\s*\\\s*'
            rf'"{re.escape(unique_artifact)}"',
            controller,
        )
        assert f"- '{unique_artifact}'" in controller


def test_candidate_terraform_contains_no_execution_escape_hatches() -> None:
    terraform_text = "\n".join(_read(path) for path in TF_ROOT.rglob("*.tf"))
    implicit_inputs = [
        path.relative_to(TF_ROOT)
        for path in TF_ROOT.rglob("*")
        if path.is_file() and (
            path.name.endswith((".tf.json", ".tftest.json"))
            or path.name in {
                "override.tf", "override.tf.json",
                "terraform.tfvars", "terraform.tfvars.json",
            }
            or path.name.endswith((
                "_override.tf", "_override.tf.json",
                ".auto.tfvars", ".auto.tfvars.json",
            ))
        )
    ]
    assert implicit_inputs == []

    forbidden = (
        'resource "null_resource"',
        'resource "terraform_data"',
        "local-exec",
        "remote-exec",
    )
    for token in forbidden:
        assert token not in terraform_text


def test_plan_apply_build_configs_are_complete_and_pinned() -> None:
    plan_yaml = _read(KB_ROOT / "cloudbuild.terraform-plan.yaml")
    apply_yaml = _read(KB_ROOT / "cloudbuild.terraform-apply.yaml")

    assert "PINNED_AT_BOOTSTRAP" not in plan_yaml + apply_yaml
    assert "-out=/workspace/plan.tfplan" in plan_yaml
    assert "/workspace/current_state_serial.txt" in apply_yaml
    assert "terraform state pull" in apply_yaml
