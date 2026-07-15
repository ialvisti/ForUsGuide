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
from datetime import date
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


def test_runtime_dockerfile_uses_pinned_base_and_hashed_runtime_lock() -> None:
    dockerfile = _read(KB_ROOT / "Dockerfile")
    pinned_base = _tool_image("PYTHON_BASE_IMAGE")

    assert f"FROM {pinned_base}" in dockerfile
    assert "COPY requirements.lock" in dockerfile
    assert "--require-hashes -r requirements.lock" in dockerfile
    assert "requirements.txt" not in dockerfile
    assert "pip install --no-cache-dir --upgrade pip" not in dockerfile


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
            approvals_text="",
            today=date(2026, 7, 15),
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
            approvals_text="",
            today=date(2026, 7, 15),
        )

    approval = (
        f"APROBADO G5V {digest} CVE-2026-2000 security-owner=sec "
        "release-owner=release requester=requester exploitability=not-reachable "
        "expires=2026-07-30 compensating-control=network-deny"
    )
    verifier.verify(
        findings,
        digest=digest,
        approvals_text=approval,
        today=date(2026, 7, 15),
    )

    with pytest.raises(verifier.ScanRejected, match="G5V"):
        verifier.verify(
            findings,
            digest="registry/image@sha256:" + "c" * 64,
            approvals_text=approval,
            today=date(2026, 7, 15),
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


def test_environment_module_declares_database_scoped_iam() -> None:
    iam_tf = _read(TF_ROOT / "modules" / "ticket_environment" / "iam.tf")

    # google/google-beta 5.x has no per-database Firestore IAM resource. The
    # supported least-privilege shape is a non-authoritative project member
    # whose condition matches the database resource name exactly.
    for runtime in ("producer", "worker", "reconciler"):
        assert (
            f'resource "google_project_iam_member" "{runtime}_firestore"'
            in iam_tf
        )
    exact_database_condition = (
        'expression  = "resource.name == '
        '\\"${local.firestore_database_resource}\\""'
    )
    assert iam_tf.count(exact_database_condition) == 3
    assert "startsWith(" not in iam_tf


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
