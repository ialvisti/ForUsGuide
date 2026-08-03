"""Security contracts for the platform CI/CD identities.

These checks intentionally inspect the declarative Terraform rather than live
IAM.  Live positive/negative IAM probes remain gated by G1B; this suite makes
it impossible to bootstrap obviously non-functional or cross-purpose build
identities in the meantime.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from scripts.release_controller import (
    PLATFORM_CUSTOM_ROLE_PERMISSION_HASHES,
    PLATFORM_IAM_ROLE_POLICY,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
PLATFORM_ROOT = REPO_ROOT / "infra" / "terraform" / "live" / "platform"
LIVE_ROOT = REPO_ROOT / "infra" / "terraform" / "live"


def _read(name: str) -> str:
    return (PLATFORM_ROOT / name).read_text(encoding="utf-8")


def test_every_live_root_is_pinned_to_the_canonical_gcp_project() -> None:
    for root in ("platform", "staging", "production"):
        variables = (LIVE_ROOT / root / "variables.tf").read_text(encoding="utf-8")
        project = variables.split('variable "project_id"', 1)[1].split("\n}", 1)[0]
        assert 'condition     = var.project_id == "rag-kb-system"' in project
        assert "canonical project rag-kb-system" in project


def test_controller_custom_role_policy_hashes_match_platform_hcl() -> None:
    source = "\n".join(path.read_text() for path in PLATFORM_ROOT.glob("*.tf"))
    observed: dict[str, str] = {}
    pattern = re.compile(
        r'resource\s+"google_project_iam_custom_role"\s+"([^"]+)"\s*\{'
    )
    for match in pattern.finditer(source):
        depth = 1
        cursor = match.end()
        while depth and cursor < len(source):
            depth += (source[cursor] == "{") - (source[cursor] == "}")
            cursor += 1
        block = source[match.end():cursor - 1]
        permissions = re.search(r"permissions\s*=\s*\[(.*?)\]", block, re.DOTALL)
        assert permissions is not None
        values = sorted(re.findall(r'"([^"]+)"', permissions.group(1)))
        encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
        observed[match.group(1)] = hashlib.sha256(encoded).hexdigest()

    assert observed == PLATFORM_CUSTOM_ROLE_PERMISSION_HASHES


def test_controller_has_address_specific_role_policy_for_every_platform_binding() -> None:
    source = "\n".join(path.read_text() for path in PLATFORM_ROOT.glob("*.tf"))
    bindings = {
        (resource_type, name)
        for resource_type, name in re.findall(
            r'^resource "([^"]+_iam_member)" "([^"]+)"', source, re.MULTILINE,
        )
    }

    assert bindings == set(PLATFORM_IAM_ROLE_POLICY)


def test_platform_imports_the_existing_immutable_artifact_repository() -> None:
    main = _read("main.tf")
    imports = _read("imports.tf")

    assert 'repository_id = "kb-rag"' in main
    assert 'resource "google_artifact_registry_repository" "images"' in main
    assert 'format        = "DOCKER"' in main
    assert re.search(r"docker_config\s*\{\s*immutable_tags\s*=\s*true", main)
    assert "google_artifact_registry_repository.images" in imports
    assert "repositories/kb-rag" in imports


def test_runtime_builds_publish_only_to_the_existing_kb_rag_repository() -> None:
    cloud_build = _read("cloud_build.tf")
    controller = (REPO_ROOT / "kb-rag-system" / "scripts" /
                  "release_controller.py").read_text(encoding="utf-8")

    expected = (
        '${var.region}-docker.pkg.dev/${var.project_id}/'
        '${google_artifact_registry_repository.images.repository_id}/'
        'kb-rag-system:$COMMIT_SHA-$BUILD_ID'
    )
    assert cloud_build.count(expected) == 2
    assert 'google_artifact_registry_repository.images["runtime"]' not in cloud_build
    assert "kb-rag-runtime/kb-rag-system" not in controller
    assert 'f"us-central1-docker.pkg.dev/{project}/kb-rag/kb-rag-system"' in controller


def test_every_controller_step_receives_authenticated_cloud_build_substitutions() -> None:
    cloud_build = _read("cloud_build.tf")

    requested = cloud_build.count('requested_verify_option = "VERIFIED"')
    explicit_env = re.findall(
        r'env\s*=\s*\[\s*"BUILD_ID=\$BUILD_ID"\s*,\s*'
        r'"PROJECT_ID=\$PROJECT_ID"\s*,\s*'
        r'"COMMIT_SHA=\$COMMIT_SHA"\s*,?\s*\]',
        cloud_build,
    )
    assert len(explicit_env) == requested
    assert "automap_substitutions" not in cloud_build


def test_image_builders_are_explicit_and_can_scan_without_deploy() -> None:
    iam = _read("pipeline_iam.tf")
    cloud_build = _read("cloud_build.tf")

    assert 'resource "google_service_account" "controller_builder"' in cloud_build
    assert 'resource "google_artifact_registry_repository_iam_member" "image_writer"' in iam
    assert "repository = google_artifact_registry_repository.images.repository_id" in iam
    assert 'role       = "roles/artifactregistry.writer"' in iam
    assert 'resource "google_project_iam_member" "image_scanner"' in iam
    assert re.search(r'role\s*=\s*"roles/ondemandscanning\.admin"', iam)

    for identity in (
        "google_service_account.ci.email",
        "google_service_account.e2e_image[0].email",
        "google_service_account.controller_builder.email",
    ):
        assert identity in iam

    assert "/release-controller@sha256:" in iam
    assert "google_artifact_registry_repository.images.repository_id" in iam


def test_controller_candidate_verifier_has_no_publish_scan_or_state_authority() -> None:
    iam = _read("pipeline_iam.tf")
    cloud_build = _read("cloud_build.tf")
    imports = _read("imports.tf")

    assert 'resource "google_service_account" "controller_verifier"' in cloud_build
    assert (
        "controller-verifier = google_service_account.controller_verifier.email"
        in iam
    )
    assert (
        'resource "google_storage_bucket_iam_member" '
        '"controller_verifier_source_reader"' in iam
    )
    source_reader = iam.split(
        'resource "google_storage_bucket_iam_member" '
        '"controller_verifier_source_reader"',
        1,
    )[1].split("\n}", 1)[0]
    assert 'bucket = "${var.project_id}_cloudbuild"' in source_reader
    assert 'role   = "roles/storage.objectViewer"' in source_reader
    assert "google_service_account.controller_verifier.email" in source_reader
    assert "to = google_service_account.controller_verifier" in imports
    assert (
        'id = "projects/${var.project_id}/serviceAccounts/'
        'ticket-controller-verify@${var.project_id}.iam.gserviceaccount.com"'
        in imports
    )

    image_builders = iam.split("image_builders =", 1)[1].split(
        "builder_evidence_prefixes =", 1,
    )[0]
    evidence_builders = iam.split("builder_evidence_prefixes =", 1)[1].split(
        "gate_receipt_sas =", 1,
    )[0]
    state_pipelines = iam.split("state_pipelines =", 1)[1].split(
        "privileged_pipeline_sas =", 1,
    )[0]
    privileged = iam.split("privileged_pipeline_sas =", 1)[1].split(
        "controller_runtime_sas =", 1,
    )[0]
    for privileged_section in (
        image_builders, evidence_builders, state_pipelines, privileged,
    ):
        assert "controller_verifier" not in privileged_section

    assert "controller_builder" in image_builders


def test_platform_apply_cannot_act_as_bootstrap_verifier_or_publisher() -> None:
    iam = _read("pipeline_iam.tf")
    actas = iam.split("platform_apply_actas_sas =", 1)[1].split(
        "plan_pipelines =", 1,
    )[0]

    assert "local.controller_runtime_sas" in actas
    assert "local.build_execution_sas" not in actas
    assert "controller_verifier" not in actas
    assert "controller_builder" not in actas


def test_builders_have_logs_and_write_once_evidence_but_no_deploy_or_state() -> None:
    iam = _read("pipeline_iam.tf")

    assert 'resource "google_storage_bucket_iam_member" "builder_evidence_writer"' in iam
    assert 'role   = "roles/storage.objectCreator"' in iam
    assert 'resource "google_project_iam_member" "pipeline_logs"' in iam
    assert re.search(r'role\s*=\s*"roles/logging\.logWriter"', iam)

    builder_section = iam.split("image_builders =", 1)[1].split("state_pipelines =", 1)[0]
    for forbidden in (
        "roles/run.admin",
        "roles/iam.securityAdmin",
        "roles/storage.objectAdmin",
        "tfstate",
    ):
        assert forbidden not in builder_section


def test_gate_receipt_identities_are_get_only_and_have_no_artifact_or_state_access() -> None:
    iam = _read("pipeline_iam.tf")
    cloud_build = _read("cloud_build.tf")

    assert 'resource "google_service_account" "gate_receipt"' in cloud_build
    assert 'resource "google_cloudbuild_trigger" "gate_receipt"' in cloud_build
    assert 'approval_required = true' in cloud_build
    assert '"--approver-accounts", each.value.approver_accounts' in cloud_build
    gate_trigger = cloud_build.split(
        'resource "google_cloudbuild_trigger" "gate_receipt"', 1
    )[1].split('\nresource "', 1)[0]
    assert "_APPROVER_ACCOUNTS" not in gate_trigger
    assert "gate_receipt_sas =" in iam
    assert "google_service_account.gate_receipt" in iam
    assert 'resource "google_project_iam_member" "gate_receipt_approver"' in iam
    assert 'role     = "roles/cloudbuild.builds.approver"' in iam
    assert 'member   = "user:${each.value}"' in iam

    specs = cloud_build.split("gate_receipt_specs =", 1)[1].split(
        "\n  gate_receipt_scope_substitutions =", 1
    )[0]
    account_ids = re.findall(r'account_id\s*=\s*"([^"]+)"', specs)
    assert len(account_ids) == 22
    assert len(account_ids) == len(set(account_ids))
    assert all(6 <= len(account_id) <= 30 for account_id in account_ids)

    # Gate builds authenticate a human approval and the immutable Cloud Build
    # record.  They may execute the pinned controller and describe builds, but
    # they must never become a data plane, state, evidence, or deploy identity.
    for local_name in (
        "provenance_builders",
        "controller_runtime_sas",
        "build_execution_sas",
    ):
        block = iam.split(f"{local_name} =", 1)[1].split("\n\n", 1)[0]
        assert "local.gate_receipt_sas" in block

    assert iam.count("local.gate_receipt_sas") == 3
    for forbidden_local in (
        "state_pipelines",
        "builder_evidence_prefixes",
        "aux_evidence_writer",
        "plan_evidence_writer",
        "apply_evidence_reader",
        "apply_pipeline_roles",
        "image_builders",
    ):
        block = iam.split(f"{forbidden_local} =", 1)[1].split("\n\n", 1)[0] \
            if f"{forbidden_local} =" in iam else ""
        assert "local.gate_receipt_sas" not in block

    provenance_role = iam.split(
        'resource "google_project_iam_custom_role" "build_provenance_reader"', 1
    )[1].split("\n}", 1)[0]
    assert re.findall(r'"cloudbuild\.[^"]+"', provenance_role) == [
        '"cloudbuild.builds.get"'
    ]


def test_gate_receipts_cover_preproduction_evidence_and_scan_exception_quorums() -> None:
    cloud_build = _read("cloud_build.tf")
    variables = _read("variables.tf")

    expected_roles = {
        "g4-requester": ("G4", "requester"),
        "g4-n8n-owner": ("G4", "n8n-owner"),
        "g4-participant-plan-owner": ("G4", "participant-plan-owner"),
        "g4-forusbots-owner": ("G4", "forusbots-owner"),
        "g4-delivery-owner": ("G4", "delivery-owner"),
        "g5-maintainer": ("G5", "maintainer"),
        "g5-requester": ("G5", "requester"),
        "g5v-security-owner": ("G5V", "security-owner"),
        "g5v-release-owner": ("G5V", "release-owner"),
        "g5v-requester": ("G5V", "requester"),
    }
    for key, (gate, role) in expected_roles.items():
        block = cloud_build.split(f"    {key} = {{", 1)[1].split("\n    }", 1)[0]
        assert f'gate              = "{gate}"' in block
        assert f'approver_role     = "{role}"' in block
        assert f'var.gate_approver_accounts["{key}"]' in block
        assert f'"{key}",' in variables

    scope = cloud_build.split("gate_receipt_scope_substitutions =", 1)[1].split(
        "\n}\n\nresource", 1
    )[0]
    expected_scope = {
        "G4": {
            "_CANDIDATE_SHA", "_CONTROLLER_DIGEST", "_IMAGE_DIGEST",
            "_EVIDENCE_INPUTS_SHA256",
        },
        "G5": {
            "_CANDIDATE_SHA", "_CONTROLLER_DIGEST", "_IMAGE_DIGEST",
            "_EVIDENCE_INPUTS_SHA256", "_EVIDENCE_MANIFEST_URI",
            "_EVIDENCE_MANIFEST_SHA256",
        },
        "G5V": {
            "_CANDIDATE_SHA", "_CONTROLLER_DIGEST", "_IMAGE_DIGEST",
            "_VULNERABILITY_ID", "_SCAN_REPORT_SHA256",
        },
    }
    for gate, expected_keys in expected_scope.items():
        block = scope.split(f"    {gate} = tomap({{", 1)[1].split("\n    })", 1)[0]
        assert set(re.findall(r"(_[A-Z0-9_]+)\s*=", block)) == expected_keys

    for consumer in ("evidence_manifest", "staging_attest", "runtime_attest"):
        block = cloud_build.split(
            f'resource "google_cloudbuild_trigger" "{consumer}"', 1
        )[1].split("\n}", 1)[0]
        assert '_GATE_RECEIPTS = ""' in block
        assert '"--gate-receipts", "$_GATE_RECEIPTS"' in block


def test_platform_apply_actas_is_target_specific_for_managed_trigger_identities() -> None:
    iam = _read("pipeline_iam.tf")

    local_block = iam.split("platform_apply_actas_sas =", 1)[1].split("\n\n", 1)[0]
    assert "local.controller_runtime_sas" in local_block
    assert "local.build_execution_sas" not in local_block
    assert "ticket-scheduler-stg" in local_block
    assert "ticket-scheduler-prod" in local_block

    binding = iam.split(
        'resource "google_service_account_iam_member" '
        '"platform_apply_actas_scheduler"',
        1,
    )[1].split("\n}", 1)[0]
    assert "for_each           = local.platform_apply_actas_sas" in binding
    assert "service_account_id = each.value" in binding
    assert 'role               = "roles/iam.serviceAccountUser"' in binding
    assert (
        'member             = "serviceAccount:${google_service_account.'
        'apply_platform[0].email}"' in binding
    )

    # This permission must never become a project-wide grant. In particular,
    # apply-platform may act as the exact trigger/scheduler SAs only.
    project_bindings = re.findall(
        r'resource "google_project_iam_member" "[^"]+" \{(.*?)\n}',
        iam,
        re.DOTALL,
    )
    assert all(
        not (
            'roles/iam.serviceAccountUser' in block
            and "google_service_account.apply_platform" in block
        )
        for block in project_bindings
    )


def test_reconciler_scheduler_is_bound_to_an_attested_active_release_phase() -> None:
    containers = _read("environment_containers.tf")
    variables = _read("variables.tf")
    outputs = _read("outputs.tf")
    cloud_build = _read("cloud_build.tf")

    assert 'variable "environment_release_phase"' in variables
    scheduler = containers.split(
        'resource "google_cloud_scheduler_job" "environment"', 1
    )[1].split("\n}", 1)[0]
    assert "var.environment_release_phase[each.key]" in scheduler
    assert '"shadow", "knowledge_only", "full"' in scheduler
    assert "var.environment_run_resources[each.key]" in scheduler
    assert 'output "environment_release_phase"' in outputs

    plan = cloud_build.split(
        'resource "google_cloudbuild_trigger" "platform_plan"', 1
    )[1].split('\nresource "google_cloudbuild_trigger" "platform_apply"', 1)[0]
    for marker in (
        "_STAGING_RELEASE_PHASE", "_PRODUCTION_RELEASE_PHASE",
        '"--staging-release-phase"', '"--production-release-phase"',
    ):
        assert marker in plan

    for gate in ("G1B", "G2", "G6B", "G1C_PREPARE", "G1C_ENFORCE"):
        scope = cloud_build.split(f"    {gate} = tomap({{", 1)[1].split(
            "\n    })", 1
        )[0]
        assert "_PLATFORM_RELEASE_PHASES_SHA256" in scope


def test_queue_transition_can_only_list_tasks_on_the_two_ticket_queues() -> None:
    iam = _read("pipeline_iam.tf")

    broker = iam.split(
        'resource "google_project_iam_custom_role" "platform_queue_broker"', 1
    )[1].split("\n}", 1)[0]
    assert '"cloudtasks.queues.pause"' in broker
    assert '"cloudtasks.queues.resume"' in broker
    assert '"cloudtasks.tasks.list"' not in broker

    inspector = iam.split(
        'resource "google_project_iam_custom_role" '
        '"platform_queue_task_inspector"', 1
    )[1].split("\n}", 1)[0]
    assert re.findall(r'"cloudtasks\.[^"]+"', inspector) == [
        '"cloudtasks.tasks.list"',
    ]

    binding = iam.split(
        'resource "google_cloud_tasks_queue_iam_member" '
        '"platform_apply_queue_task_inspector"', 1
    )[1].split("\n}", 1)[0]
    assert (
        "for_each = var.cicd_bootstrap.enabled ? local.environment_queues : {}"
        in binding
    )
    assert "project  = var.project_id" in binding
    assert "location = var.region" in binding
    assert "name     = google_cloud_tasks_queue.environment[each.key].name" in binding
    assert "google_project_iam_custom_role.platform_queue_task_inspector[0].id" in binding
    assert "google_service_account.apply_platform[0].email" in binding
    assert "condition" not in binding
    assert (
        'resource "google_project_iam_member" '
        '"platform_apply_queue_task_inspector"'
    ) not in iam


def test_platform_requires_terraform_version_with_cross_variable_validation() -> None:
    versions = _read("versions.tf")

    assert 'required_version = ">= 1.9.0, < 1.10.0"' in versions


def test_apply_triggers_require_exact_post_plan_receipt_quorums() -> None:
    cloud_build = _read("cloud_build.tf")

    for resource in ("platform_apply", "staging_apply", "production_apply"):
        block = cloud_build.split(
            f'resource "google_cloudbuild_trigger" "{resource}"', 1
        )[1].split("\n}", 1)[0]
        assert '"--gate-receipts", "$_GATE_RECEIPTS"' in block
        assert 'approval_required = true' in block

    platform = cloud_build.split(
        'resource "google_cloudbuild_trigger" "platform_apply"', 1
    )[1].split("\n}", 1)[0]
    assert '"--prepare-smoke-uri", "$_PREPARE_SMOKE_URI"' in platform


def test_receipt_consumers_can_describe_build_and_trigger_but_not_mutate_them() -> None:
    iam = _read("pipeline_iam.tf")

    environment_reader = iam.split(
        'resource "google_project_iam_custom_role" "environment_plan_reader"', 1
    )[1].split("\n}", 1)[0]
    assert '"cloudbuild.builds.get"' in environment_reader
    for forbidden in (
        '"cloudbuild.builds.create"',
        '"cloudbuild.builds.list"',
        '"cloudbuild.builds.update"',
    ):
        assert forbidden not in environment_reader

    provenance = iam.split("provenance_builders =", 1)[1].split("\n\n", 1)[0]
    assert "google_service_account.evidence_manifest[0].email" in provenance
    assert "google_service_account.staging_attest[0].email" in provenance


def test_e2e_runtime_can_only_create_write_once_evidence_objects() -> None:
    iam = _read("pipeline_iam.tf")
    assert (
        'resource "google_storage_bucket_iam_member" '
        '"e2e_runtime_evidence_writer"' in iam
    )
    block = iam.split(
        'resource "google_storage_bucket_iam_member" '
        '"e2e_runtime_evidence_writer"',
        1,
    )[1].split("\n}", 1)[0]
    assert 'role   = "roles/storage.objectCreator"' in block
    assert 'google_service_account.runtime["ticket-e2e-stg"].email' in block
    assert "roles/storage.objectAdmin" not in block
    assert "/objects/handle-ticket/e2e/" in block


def test_every_evidence_writer_is_conditioned_to_an_exclusive_prefix() -> None:
    iam = _read("pipeline_iam.tf")

    expected = {
        "plan_evidence_writer": ("/objects/plans/${each.key}/",),
        "platform_apply_evidence_writer": ("/objects/platform-outputs/",),
        "builder_evidence_writer": ("/objects/${each.value.prefix}",),
        "e2e_runtime_evidence_writer": ("/objects/handle-ticket/e2e/",),
        "aux_evidence_writer": ("/objects/${each.value.prefix}",),
    }
    for resource, markers in expected.items():
        block = iam.split(
            f'resource "google_storage_bucket_iam_member" "{resource}"', 1
        )[1].split("\n}", 1)[0]
        assert 'role     = "roles/storage.objectCreator"' in block or (
            'role   = "roles/storage.objectCreator"' in block
        )
        assert "condition {" in block
        assert "resource.name.startsWith" in block
        for marker in markers:
            assert marker in block

    assert 'prefix = "runtime/"' in iam
    assert 'prefix = "e2e-images/"' in iam
    assert 'prefix = "promotions/"' in iam
    assert 'prefix = "evidence/"' in iam


def test_plan_and_apply_identities_receive_distinct_functional_roles() -> None:
    iam = _read("pipeline_iam.tf")

    assert 'resource "google_project_iam_custom_role" "platform_plan_reader"' in iam
    assert 'resource "google_project_iam_custom_role" "environment_plan_reader"' in iam
    assert 'resource "google_project_iam_member" "plan_functional"' in iam
    assert 'resource "google_project_iam_member" "apply_functional"' in iam

    # Plan refresh may inspect configuration, but it must not read Firestore
    # entities or mutate services.
    assert '"datastore.databases.getMetadata"' in iam
    assert '"datastore.entities.get"' not in iam
    assert '"datastore.entities.list"' not in iam
    assert '"run.operations.get"' in iam
    assert '"run.operations.list"' in iam
    platform_reader = iam.split(
        'resource "google_project_iam_custom_role" "platform_plan_reader"', 1
    )[1].split("\n}", 1)[0]
    for owned_container_read in (
        "cloudscheduler.jobs.get",
        "cloudtasks.queues.get",
        "cloudtasks.queues.getIamPolicy",
        "datastore.databases.getMetadata",
        "run.jobs.get",
        "run.jobs.getIamPolicy",
        "run.services.get",
        "run.services.getIamPolicy",
        "secretmanager.secrets.get",
        "secretmanager.secrets.getIamPolicy",
    ):
        assert f'"{owned_container_read}"' in platform_reader
    environment_reader = iam.split(
        'resource "google_project_iam_custom_role" "environment_plan_reader"', 1
    )[1].split("\n}", 1)[0]
    for stale_permission in (
        "cloudscheduler.jobs.get",
        "cloudtasks.queues.get",
        "iam.roles.get",
        "iam.serviceAccounts.get",
        "run.jobs.getIamPolicy",
        "run.services.getIamPolicy",
    ):
        assert f'"{stale_permission}"' not in environment_reader

    for required_role in (
        "roles/serviceusage.serviceUsageAdmin",
        "roles/artifactregistry.admin",
        "roles/datastore.indexAdmin",
        "roles/logging.configWriter",
        "roles/monitoring.editor",
    ):
        assert required_role in iam
    for required_broker in (
        "environment_run_creator",
        "platform_queue_broker",
        "platform_scheduler_broker",
        "platform_run_iam_broker",
    ):
        assert required_broker in iam
    assert "environment_secret_container_admin" in iam

    assert (
        'resource "google_artifact_registry_repository_iam_member" '
        '"environment_apply_runtime_reader"' in iam
    )

    for forbidden_role in ('"roles/owner"', '"roles/editor"', '"roles/storage.admin"'):
        assert forbidden_role not in iam


def test_environment_apply_uses_temporary_creator_then_direct_resource_iam() -> None:
    iam = _read("pipeline_iam.tf")
    runtime_iam = _read("runtime_project_iam.tf")
    containers = _read("environment_containers.tf")
    apply_roles = iam.split("apply_pipeline_roles =", 1)[1].split(
        "environment_apply_boundaries =", 1
    )[0]
    assert apply_roles.count("roles = local.environment_apply_residual_project_roles") == 2
    for role in (
        "roles/run.admin",
        "roles/cloudtasks.queueAdmin",
        "roles/iam.serviceAccountAdmin",
        "roles/iam.roleAdmin",
    ):
        for environment in ("staging", "production"):
            block = apply_roles.split(f"    {environment} = {{", 1)[1].split(
                "    }", 1
            )[0]
            assert role not in block

    creator = iam.split(
        'resource "google_project_iam_custom_role" "environment_run_creator"', 1
    )[1].split("\n}", 1)[0]
    assert '"run.jobs.create"' in creator
    assert '"run.services.create"' in creator
    permissions = set(re.findall(r'"(run\.[^"]+)"', creator))
    assert permissions == {"run.jobs.create", "run.services.create"}
    assert 'resource "google_project_iam_member" "environment_run_creator"' in iam
    assert 'var.environment_handoff_phase[environment] == "bootstrap"' in iam

    assert (
        'resource "google_cloud_run_v2_service_iam_member" '
        '"environment_apply_developer"' in runtime_iam
    )
    assert (
        'resource "google_cloud_run_v2_job_iam_member" '
        '"environment_apply_developer"' in runtime_iam
    )
    assert runtime_iam.count('role     = "roles/run.developer"') == 2
    assert 'resource "google_cloud_tasks_queue" "environment"' in containers
    assert 'resource "google_cloud_scheduler_job" "environment"' in containers
    assert 'resource "google_project_iam_custom_role" "ticket_queue_enqueuer"' in containers
    assert 'resource "google_service_account_iam_member" "environment_apply_signer_iam"' not in iam
    assert "environment_service_account_iam_admin" not in iam
    for signer_binding in (
        "runtime_producer_actas_signer",
        "runtime_reconciler_actas_signer",
        "tasks_agent_signs_as_runtime_signer",
    ):
        assert signer_binding in runtime_iam

    assert "ticket-jobs-staging" in containers
    assert "ticket-jobs-prod" in containers
    assert "ticketQueueEnqueuer${title(each.key)}" in containers
    assert "ticket-staging" in iam
    assert re.search(r'database\s*=\s*"\(default\)"', runtime_iam)
    assert "ticket-task-signer-stg" in runtime_iam
    assert "ticket-task-signer-prod" in runtime_iam

    # Las SAs runtime las crea platform. El apply de environment sólo gestiona
    # policy mediante el riesgo residual explícito; no recibe SA Admin.
    assert 'role     = "roles/iam.serviceAccountAdmin"' not in iam.split(
        "# --- Apply permissions", 1
    )[1]


def test_platform_owns_queue_scheduler_and_runtime_resource_iam() -> None:
    iam = _read("pipeline_iam.tf")
    runtime_iam = _read("runtime_project_iam.tf")
    containers = _read("environment_containers.tf")
    module_dir = REPO_ROOT / "infra" / "terraform" / "modules" / "ticket_environment"
    module_tf = "\n".join(
        path.read_text(encoding="utf-8") for path in module_dir.glob("*.tf")
    )

    queue_role = containers.split(
        'resource "google_project_iam_custom_role" "ticket_queue_enqueuer"', 1
    )[1].split("\n}", 1)[0]
    assert set(re.findall(r'"(cloudtasks\.[a-z]+\.[a-zA-Z]+)"', queue_role)) == {
        "cloudtasks.tasks.create",
        "cloudtasks.tasks.get",
        "cloudtasks.queues.get",
    }
    assert 'resource "google_cloud_tasks_queue" "environment"' in containers
    assert 'resource "google_cloud_scheduler_job" "environment"' in containers
    assert (
        'resource "google_cloud_tasks_queue_iam_member" '
        '"runtime_producer_queue"' in runtime_iam
    )
    assert (
        'resource "google_cloud_tasks_queue_iam_member" '
        '"runtime_reconciler_queue"' in runtime_iam
    )
    assert 'resource "google_cloud_tasks_queue"' not in module_tf
    assert 'resource "google_cloud_scheduler_job"' not in module_tf
    assert 'resource "google_cloud_tasks_queue_iam_member"' not in module_tf

    # resource.name conditions are unsupported for inherited Run, Tasks,
    # Scheduler and service-account IAM grants. These are child IAM resources
    # or the temporary create-only handoff, never misleading conditions.
    for stale_binding in (
        "environment_apply_run_admin",
        "environment_apply_queue_admin",
        "environment_apply_scheduler",
        "environment_apply_role_admin",
    ):
        assert stale_binding not in iam
    assert 'role     = "roles/run.developer"' in runtime_iam
    assert "roles/run.admin" not in iam
    assert "roles/cloudtasks.queueAdmin" not in iam


def test_handoff_state_is_exported_for_controller_attestation() -> None:
    variables = _read("variables.tf")
    outputs = _read("outputs.tf")
    assert 'variable "environment_handoff_phase"' in variables
    assert 'variable "environment_container_phase"' in variables
    assert 'variable "environment_run_resources"' in variables
    assert 'output "environment_handoff_phase"' in outputs
    assert 'output "environment_container_phase"' in outputs
    assert 'output "environment_run_resources"' in outputs
    assert 'output "environment_secret_ids"' in outputs


def test_staging_observer_has_get_only_run_and_prefix_scoped_evidence() -> None:
    iam = _read("pipeline_iam.tf")
    role = iam.split(
        'resource "google_project_iam_custom_role" '
        '"staging_observer_run_reader"',
        1,
    )[1].split("\n}", 1)[0]
    assert set(re.findall(r'"(run\.[a-z]+\.[a-zA-Z]+)"', role)) == {
        "run.jobs.get",
        "run.services.get",
    }
    assert "run.jobs.list" not in role
    assert "run.services.list" not in role
    assert (
        'resource "google_storage_bucket_iam_member" '
        '"staging_observer_evidence_writer"' in iam
    )
    assert '"staging-observations/"' in iam
    assert '"rollback-observations/"' in iam
    reader = iam.split(
        'resource "google_storage_bucket_iam_member" '
        '"staging_observer_e2e_reader"',
        1,
    )[1].split("\n}", 1)[0]
    assert 'role   = "roles/storage.objectViewer"' in reader
    assert "/objects/handle-ticket/e2e/" in reader


def test_residual_project_iam_risk_is_explicit_and_controller_guarded() -> None:
    iam = _read("pipeline_iam.tf")
    controller = (REPO_ROOT / "kb-rag-system/scripts/release_controller.py").read_text()

    assert "environment_apply_residual_project_roles" in iam
    assert "residual excluye explícitamente" in iam
    assert '"roles/iam.securityAdmin"' not in iam
    assert '"roles/secretmanager.admin"' not in iam
    for marker in (
        "ticket-staging",
        "(default)",
        "ticket-producer-stg",
        "ticket-worker-prod",
    ):
        assert marker in controller


def test_environment_secret_admin_cannot_read_or_mutate_versions() -> None:
    iam = _read("pipeline_iam.tf")
    variables = _read("variables.tf")
    assert 'variable "environment_secret_ids"' in variables
    assert 'resource "google_project_iam_custom_role" "environment_secret_container_admin"' in iam
    role = iam.split(
        'resource "google_project_iam_custom_role" '
        '"environment_secret_container_admin"', 1
    )[1].split("\n}", 1)[0]
    assert '"secretmanager.secrets.getIamPolicy"' in role
    assert '"secretmanager.secrets.setIamPolicy"' in role
    assert '"secretmanager.secrets.create"' not in role
    assert '"secretmanager.secrets.delete"' not in role
    for forbidden in (
        "secretmanager.versions.access",
        "secretmanager.versions.add",
        "secretmanager.versions.destroy",
        "secretmanager.versions.disable",
        "secretmanager.versions.enable",
    ):
        assert forbidden not in role
    binding = iam.split(
        'resource "google_project_iam_member" "environment_apply_secret_admin"', 1
    )[1].split("\n}", 1)[0]
    assert "data.google_project.current.number" in binding
    assert "/secrets/${each.value.secret_id}" in binding
    assert "condition {" in binding


def test_custom_roles_use_supported_cloud_build_permissions() -> None:
    iam = _read("pipeline_iam.tf")
    platform_reader = iam.split(
        'resource "google_project_iam_custom_role" "platform_plan_reader"', 1
    )[1].split("\n}", 1)[0]

    # Cloud Build trigger reads are authorized by builds.get/list.  The
    # superficially plausible cloudbuild.triggers.get permission does not
    # exist in queryTestablePermissions and would make role creation fail.
    assert '"cloudbuild.triggers.get"' not in iam
    assert '"cloudbuild.builds.get"' in platform_reader
    assert '"cloudbuild.builds.list"' in platform_reader
    assert '"resourcemanager.projects.list"' not in iam


def test_provenance_tool_calls_have_minimal_build_get_permission() -> None:
    iam = _read("pipeline_iam.tf")
    controller = (REPO_ROOT / "kb-rag-system/scripts/release_controller.py").read_text()

    assert '"gcloud", "builds", "describe"' in controller
    assert (
        'resource "google_project_iam_custom_role" '
        '"build_provenance_reader"' in iam
    )
    role = iam.split(
        'resource "google_project_iam_custom_role" "build_provenance_reader"', 1
    )[1].split("\n}", 1)[0]
    assert 'permissions = ["cloudbuild.builds.get"]' in role
    assert "cloudbuild.builds.list" not in role
    assert "cloudbuild.builds.create" not in role
    assert (
        'resource "google_project_iam_member" "build_provenance_reader"' in iam
    )
    assert "google_service_account.ci.email" in iam
    assert "google_service_account.e2e_image[0].email" in iam


def test_environment_applies_can_manage_only_the_existing_rag_bucket_iam() -> None:
    iam = _read("pipeline_iam.tf")

    assert (
        'resource "google_project_iam_custom_role" '
        '"environment_bucket_iam_admin"' in iam
    )
    role = iam.split(
        'resource "google_project_iam_custom_role" '
        '"environment_bucket_iam_admin"',
        1,
    )[1].split("\n}", 1)[0]
    assert '"storage.buckets.get"' in role
    assert '"storage.buckets.getIamPolicy"' in role
    assert '"storage.buckets.setIamPolicy"' in role
    assert "storage.objects." not in role

    assert (
        'resource "google_storage_bucket_iam_member" '
        '"staging_apply_rag_bucket_iam"' in iam
    )
    binding = iam.split(
        'resource "google_storage_bucket_iam_member" '
        '"staging_apply_rag_bucket_iam"',
        1,
    )[1].split("\n}", 1)[0]
    assert re.search(r'bucket\s*=\s*"rag-kb-system-kb-articles"', binding)
    assert "for_each = local.environment_apply_boundaries" in binding
    assert "each.value.email" in binding

    reader_role = iam.split(
        'resource "google_project_iam_custom_role" '
        '"environment_bucket_iam_reader"',
        1,
    )[1].split("\n}", 1)[0]
    assert '"storage.buckets.get"' in reader_role
    assert '"storage.buckets.getIamPolicy"' in reader_role
    assert "storage.buckets.setIamPolicy" not in reader_role
    assert "storage.objects." not in reader_role

    plan_binding = iam.split(
        'resource "google_storage_bucket_iam_member" '
        '"staging_plan_rag_bucket_reader"',
        1,
    )[1].split("\n}", 1)[0]
    assert re.search(r'bucket\s*=\s*"rag-kb-system-kb-articles"', plan_binding)
    assert "for_each = local.environment_plan_bucket_readers" in plan_binding
    assert "each.value" in plan_binding


def test_apply_for_each_keys_never_depend_on_computed_custom_role_ids() -> None:
    iam = _read("pipeline_iam.tf")

    role_map = iam.split("apply_pipeline_roles =", 1)[1].split(
        "apply_functional_grants =", 1
    )[0]
    assert "google_project_iam_custom_role." not in role_map
    for binding in ("platform_apply_storage", "platform_apply_queue_broker"):
        assert f'resource "google_project_iam_member" "{binding}"' in iam


def test_production_apply_has_no_staging_state_and_requires_release_group() -> None:
    iam = _read("pipeline_iam.tf")
    variables = _read("variables.tf")

    assert 'variable "production_release_group_email"' in variables
    assert 'resource "google_service_account_iam_member" "production_release_group"' in iam
    assert 'role               = "roles/iam.serviceAccountUser"' in iam
    assert 'resource "google_project_iam_member" "production_release_approver"' in iam
    assert 'role    = "roles/cloudbuild.builds.approver"' in iam

    production_state = re.search(
        r"production\s*=\s*\{(?P<body>.*?)\n\s*\}", iam, re.DOTALL
    )
    assert production_state is not None
    assert "tfstate-production" in production_state.group("body")
    assert "tfstate-staging" not in production_state.group("body")


def test_platform_apply_can_create_one_outputs_manifest_but_not_overwrite() -> None:
    iam = _read("pipeline_iam.tf")

    assert (
        'resource "google_storage_bucket_iam_member" '
        '"platform_apply_evidence_writer"' in iam
    )
    block = iam.split(
        'resource "google_storage_bucket_iam_member" '
        '"platform_apply_evidence_writer"',
        1,
    )[1].split("\n}", 1)[0]
    assert 'role   = "roles/storage.objectCreator"' in block
    assert "google_service_account.apply_platform[0].email" in block
    assert "roles/storage.objectAdmin" not in block


def test_platform_plan_reads_only_generation_bound_environment_inputs() -> None:
    iam = _read("pipeline_iam.tf")
    main = _read("main.tf")

    marker = (
        'resource "google_storage_bucket_iam_member" '
        '"platform_plan_environment_inputs_reader"'
    )
    assert marker in iam
    block = iam.split(marker, 1)[1].split("\n}", 1)[0]
    assert "bucket = google_storage_bucket.evidence.name" in block
    assert 'name                        = "rag-kb-system-ticket-evidence-900340137010"' in main
    assert 'role   = "roles/storage.objectViewer"' in block
    assert "google_service_account.plan_platform[0].email" in block
    assert 'resource.name.startsWith(' in block
    assert "/objects/environment-inputs/" in block
    assert "roles/storage.objectAdmin" not in block
    assert "roles/storage.admin" not in block

    aux_reader = iam.split(
        'resource "google_storage_bucket_iam_member" "aux_evidence_reader"',
        1,
    )[1].split("\n}", 1)[0]
    assert "plan_platform" not in aux_reader


def test_every_trigger_uses_a_user_managed_identity_never_compute_default() -> None:
    cloud_build = _read("cloud_build.tf")
    iam = _read("pipeline_iam.tf")
    combined = cloud_build + iam

    trigger_blocks = re.findall(
        r'resource "google_cloudbuild_trigger" "[^"]+" \{(.*?)(?=\n\})',
        cloud_build,
        re.DOTALL,
    )
    assert trigger_blocks
    assert all("service_account =" in block for block in trigger_blocks)
    assert "-compute@developer.gserviceaccount.com" not in combined
    assert "@cloudbuild.gserviceaccount.com" not in combined

    # The Cloud Build service *agent* may mint short-lived credentials for the
    # explicit user-managed SAs; that is not a default execution identity.
    assert 'resource "google_service_account_iam_member" "cloud_build_executes_as"' in iam
    assert 'role               = "roles/iam.serviceAccountTokenCreator"' in iam
