#!/usr/bin/env python3
"""Immutable, fail-closed controller for the handle-ticket release pipeline.

The image containing this module is the trust boundary. Candidate revisions
provide Terraform/application source only; no script or build YAML from the
candidate is executed with a privileged service account.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlsplit

SCRIPT_DIR = str(Path(__file__).resolve().parent)
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
from create_evidence_manifest import (  # noqa: E402
    validate_artifact,
    validate_semantic_review_bindings,
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$")
GENERATION_URI_RE = re.compile(
    r"^gs://[a-z0-9][a-z0-9._-]*/[^#\r\n]+#([1-9][0-9]*)$"
)
PLAN_URI_RE = re.compile(
    r"^gs://[a-z0-9][a-z0-9._-]*/plans/"
    r"(platform|staging|production)/[^/#]+/plan\.tfplan#[1-9][0-9]*$"
)
TRUSTED_REPOSITORY = "https://github.com/ialvisti/ForUsGuide"
TRUSTED_PROJECT_ID = "rag-kb-system"
GATE_SERVICE_ACCOUNT_IDS = {
    "g1b-gcp-owner": "ticket-g1b-gcp",
    "g1b-release-owner": "ticket-g1b-release",
    "g2-gcp-owner": "ticket-g2-gcp",
    "g6b-gcp-owner": "ticket-g6b-gcp",
    "g6b-release-owner": "ticket-g6b-release",
    "g6b-forusbots-owner": "ticket-g6b-forusbots",
    "g1c-prepare-gcp-owner": "ticket-g1cp-gcp",
    "g1c-prepare-api-owner": "ticket-g1cp-api",
    "g1c-prepare-operations": "ticket-g1cp-ops",
    "g1c-enforce-gcp-owner": "ticket-g1ce-gcp",
    "g1c-enforce-api-owner": "ticket-g1ce-api",
    "g1c-enforce-operations": "ticket-g1ce-ops",
    "g4-requester": "ticket-g4-requester",
    "g4-n8n-owner": "ticket-g4-n8n",
    "g4-participant-plan-owner": "ticket-g4-participant",
    "g4-forusbots-owner": "ticket-g4-forusbots",
    "g4-delivery-owner": "ticket-g4-delivery",
    "g5-maintainer": "ticket-g5-maintainer",
    "g5-requester": "ticket-g5-requester",
    "g5v-security-owner": "ticket-g5v-security",
    "g5v-release-owner": "ticket-g5v-release",
    "g5v-requester": "ticket-g5v-requester",
}
PLATFORM_CONTROLLER_RUNTIME_SERVICE_ACCOUNT_IDS = {
    "platform-plan": "ticket-plan-platform",
    "platform-apply": "ticket-apply-platform",
    "staging-plan": "ticket-plan-staging",
    "staging-apply": "ticket-apply-staging",
    "production-plan": "ticket-plan-production",
    "production-apply": "ticket-apply-production",
    "staging-attest": "ticket-stg-attest",
    "staging-observer": "ticket-staging-observer",
    "evidence-manifest": "ticket-evidence",
    "test-only": "ticket-test-only",
    "e2e-image": "ticket-e2e-image",
    "runtime-attest": "ticket-runtime-attest",
    "runtime-image": "ticket-ci",
    **{
        f"gate-{gate}": account_id
        for gate, account_id in GATE_SERVICE_ACCOUNT_IDS.items()
    },
}
PLATFORM_APPLY_ACTAS_SERVICE_ACCOUNT_IDS = {
    "scheduler-staging": "ticket-scheduler-stg",
    "scheduler-production": "ticket-scheduler-prod",
    **{
        f"build-{purpose}": account_id
        for purpose, account_id in (
            PLATFORM_CONTROLLER_RUNTIME_SERVICE_ACCOUNT_IDS.items()
        )
    },
}

ENVIRONMENTS = ("platform", "staging", "production")
RELEASE_PHASES = (
    "infra_only", "dark_no_traffic", "dark_100", "shadow",
    "knowledge_only", "full",
)
FIRESTORE_PHASES = ("disabled", "prepare", "enforce")
ALLOWED_PROVIDER_SOURCES = {"hashicorp/google", "hashicorp/google-beta"}
ALLOWED_TERRAFORM_RESOURCE_TYPES = frozenset({
    "google_artifact_registry_repository",
    "google_artifact_registry_repository_iam_member",
    "google_cloud_run_v2_job",
    "google_cloud_run_v2_job_iam_member",
    "google_cloud_run_v2_service",
    "google_cloud_run_v2_service_iam_member",
    "google_cloud_scheduler_job",
    "google_cloud_tasks_queue",
    "google_cloud_tasks_queue_iam_member",
    "google_cloudbuild_trigger",
    "google_firestore_database",
    "google_firestore_field",
    "google_firestore_index",
    "google_logging_metric",
    "google_monitoring_alert_policy",
    "google_monitoring_dashboard",
    "google_project_iam_custom_role",
    "google_project_iam_member",
    "google_project_service",
    "google_secret_manager_secret",
    "google_secret_manager_secret_iam_member",
    "google_service_account",
    "google_service_account_iam_member",
    "google_storage_bucket",
    "google_storage_bucket_iam_member",
})
ALLOWED_TERRAFORM_DATA_BLOCKS = {
    "live/platform/pipeline_iam.tf": {
        ("google_project", "current", "project_id=var.project_id"),
    },
}
ALLOWED_TERRAFORM_IMPORT_BLOCKS = {
    "live/platform/imports.tf": (
        """
        to = google_artifact_registry_repository.images
        id = "projects/${var.project_id}/locations/${var.region}/repositories/kb-rag"
        """,
        """
        for_each = var.enable_legacy_trigger_neutralization ? {
          legacy = "projects/rag-kb-system/locations/global/triggers/c2126528-7cd3-4063-9214-5eb82e9f76a6"
        } : {}
        to = google_cloudbuild_trigger.main_canonical[each.key]
        id = each.value
        """,
        """
        for_each = (
          var.firestore_scope_migration.enabled &&
          var.firestore_scope_migration.phase == "prepare" &&
          var.firestore_scope_migration.import_legacy
        ) ? {
          legacy = "${var.project_id} roles/datastore.user serviceAccount:kb-rag-runner@${var.project_id}.iam.gserviceaccount.com"
        } : {}
        to = google_project_iam_member.kb_rag_runner_firestore_legacy[each.key]
        id = each.value
        """,
    ),
    "live/platform/environment_containers.tf": (
        """
        for_each = var.environment_container_phase.production == "managed" ? {
          production = "projects/rag-kb-system/databases/(default)"
        } : {}
        to = google_firestore_database.environment[each.key]
        id = each.value
        """,
        """
        for_each = local.existing_environment_secret_containers
        to = google_secret_manager_secret.environment[each.key]
        id = "projects/${var.project_id}/secrets/${each.value}"
        """,
    ),
    "live/production/imports.tf": (
        """
        to = module.production.google_cloud_run_v2_service.producer[0]
        id = "projects/rag-kb-system/locations/us-central1/services/kb-rag-system"
        """,
        """
        to = module.production.google_monitoring_alert_policy.legacy_high_error_rate[0]
        id = "projects/rag-kb-system/alertPolicies/15030298849808887870"
        """,
    ),
}
TERRAFORM_FILESYSTEM_FUNCTIONS = frozenset({
    "file",
    "filebase64",
    "filebase64sha256",
    "filebase64sha512",
    "fileexists",
    "filemd5",
    "fileset",
    "filesha1",
    "filesha256",
    "filesha512",
    "pathexpand",
    "templatefile",
})
EVIDENCE_NAMES = (
    "ci_provenance", "sbom", "scan", "staging_revisions", "e2e",
    "differential", "semantic_review", "rollback",
)
EVIDENCE_COPY_FIELDS = tuple(
    field for name in EVIDENCE_NAMES for field in (f"{name}_uri", f"{name}_hash")
)
EVIDENCE_FIELDS = {
    "evidence_sha", "main_sha", "image_digest", "controller_builder_digest",
    *EVIDENCE_COPY_FIELDS, "artifact_claims", "manifest_hash",
}
PROMOTION_FIELDS = {
    "main_sha", "image_digest", "evidence_manifest_uri",
    "evidence_manifest_hash", "evidence_controller_builder_digest",
    "controller_builder_digest", "evidence_sha", *EVIDENCE_COPY_FIELDS,
    "artifact_claims", "attestation_hash",
}
DOCS_ONLY_GLOBS = (
    "docs/verification/**",
    "kb-rag-system/Development Docs/**",
    "**/README.md",
)
E2E_REQUIRED = (
    "Dockerfile.e2e", "Dockerfile.e2e.dockerignore", "requirements.lock",
    "requirements-dev.lock", "pytest.ini", "pyproject.toml", "api",
    "data_pipeline", "scripts", "tests", "tests/e2e",
    "scripts/container_smoke.py",
    "rag-testing/ticket_differential.py",
    "rag-testing/ticket_differential_thresholds.json",
)
RUNTIME_SERVICE_ACCOUNTS = {
    "staging": (
        "ticket-producer-stg", "ticket-worker-stg",
        "ticket-reconciler-stg", "ticket-task-signer-stg",
        "ticket-scheduler-stg",
    ),
    "production": (
        "ticket-producer-prod", "ticket-worker-prod", "ticket-reconciler-prod",
        "ticket-task-signer-prod", "ticket-scheduler-prod",
    ),
}
PLATFORM_RUNTIME_SERVICE_ACCOUNTS = (
    *RUNTIME_SERVICE_ACCOUNTS["staging"], "ticket-e2e-stg",
    *RUNTIME_SERVICE_ACCOUNTS["production"],
)
PLATFORM_MANAGED_RUNTIME_IAM_INVENTORY = {
    environment: frozenset({
        *{
            ("runtime_firestore", f"{environment}-{role}")
            for role in ("producer", "worker", "reconciler")
        },
        *{
            ("runtime_vertex", f"{environment}-{role}")
            for role in ("producer", "worker")
        },
        ("runtime_producer_queue", environment),
        ("runtime_reconciler_queue", environment),
        ("runtime_producer_actas_signer", environment),
        ("runtime_reconciler_actas_signer", environment),
        ("tasks_agent_signs_as_runtime_signer", environment),
        *({
            ("runtime_telemetry", "production-producer-logging"),
            ("runtime_telemetry", "production-producer-monitoring"),
        } if environment == "production" else set()),
    })
    for environment in ("staging", "production")
}
ENVIRONMENT_TFVARS = {
    "staging": {
        "producer_core_env",
        "secret_containers", "e2e_job",
        "e2e_secret_containers", "producer_baseline_revision",
        "producer_baseline_tag", "producer_candidate_tag",
        "secret_version_refs", "notification_channels",
    },
    "production": {
        "producer_core_env", "secret_version_refs",
        "producer_baseline_revision", "producer_candidate_tag",
        "producer_ingress", "producer_max_instances", "producer_min_instances",
        "producer_concurrency", "producer_timeout", "producer_cpu",
        "producer_memory", "producer_cpu_idle", "producer_startup_cpu_boost",
        "producer_port", "producer_startup_probe", "producer_liveness_probe",
        "notification_channels", "producer_invoker_members", "secret_containers",
    },
}
LLM_ROUTE_ENV_KEYS = {
    "LLM_ROUTE_CLASSIFY", "LLM_ROUTE_DECOMPOSE", "LLM_ROUTE_GR_OUTCOME",
    "LLM_ROUTE_GR_RESPONSE", "LLM_ROUTE_KNOWLEDGE", "LLM_ROUTE_REQUIRED_DATA",
    "LLM_ROUTE_EXTRACT_INQUIRIES", "LLM_ROUTE_KB_QUESTION_SYNTHESIS",
    "LLM_ROUTE_FORUSBOTS_FIELD_MAP", "LLM_ROUTE_GR_BODY_BUILD",
    "LLM_ROUTE_TICKET_FIELD_EXTRACT",
}
CORE_ENV_KEYS = {
    "ENABLE_EXECUTION_LOGGING", "FORUSBOTS_BASE_URL", "GCS_BUCKET",
    "INDEX_NAME", *LLM_ROUTE_ENV_KEYS, "LOG_LEVEL", "NAMESPACE", "OPENAI_MODEL",
    "OPENAI_REASONING_EFFORT", "TICKET_LLM_PRICING_JSON", "USE_VERTEX_AI",
}
WORKER_SECRET_REF_KEYS = {
    "FORUSBOTS_AUTH_TOKEN", "OPENAI_API_KEY", "PINECONE_API_KEY",
}
PRODUCTION_SECRET_REF_KEYS = {
    "API_KEY", *WORKER_SECRET_REF_KEYS,
}
STAGING_SECRET_REF_KEYS = {
    *PRODUCTION_SECRET_REF_KEYS, "TICKET_FAULT_SIGNING_SECRET",
}
RELEASE_PHASE_HANDLER_MODES = {
    "infra_only": "disabled",
    "dark_no_traffic": "disabled",
    "dark_100": "disabled",
    "shadow": "shadow",
    "knowledge_only": "knowledge_only",
    "full": "full",
}
ACTIVE_TICKET_RELEASE_PHASES = {"shadow", "knowledge_only", "full"}
SECRET_REF_RE = re.compile(
    r"^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/secrets/[^/]+/versions/[1-9][0-9]*$"
)
E2E_SECRET_ENV_KEYS = {
    "E2E_API_KEY", "E2E_DIFFERENTIAL_LEGACY_API_KEY",
    "E2E_WRONG_PRINCIPAL_API_KEY",
    "E2E_WRONG_TENANT_API_KEY", "E2E_RATE_LIMIT_API_KEY",
    "E2E_FAULT_SIGNING_SECRET", "E2E_N8N_CONTRACT_TOKEN",
    "E2E_FORUSBOTS_LOOKUP_TOKEN", "E2E_DELIVERY_LOOKUP_TOKEN",
    "E2E_GCP_AUDIT_TOKEN", "PINECONE_API_KEY",
}
E2E_NONSECRET_ENV_KEYS = {
    "E2E_PRINCIPAL_ID", "E2E_TENANT_ID", "E2E_PARTICIPANT_ID", "E2E_PLAN_ID",
    "E2E_MISMATCHED_PARTICIPANT_ID", "E2E_MISMATCHED_PLAN_ID",
    "E2E_COMPANY_NAME", "E2E_RECORD_KEEPER",
    "E2E_PARTICIPANT_PLAN_CONTRACT_VERSION", "E2E_N8N_CONTRACT_URL",
    "E2E_N8N_CONTRACT_VERSION", "E2E_FORUSBOTS_CONTRACT_VERSION",
    "E2E_FORUSBOTS_LOOKUP_URL", "E2E_DELIVERY_CONTRACT_VERSION",
    "E2E_DELIVERY_LOOKUP_URL", "E2E_GCP_AUDIT_CONTRACT_URL",
    "E2E_GCP_AUDIT_CONTRACT_VERSION", "E2E_TTL_SENTINEL_REFERENCE",
    "E2E_PRODUCTION_NEGATIVE_ATTESTATION", "E2E_PINECONE_INDEX",
    "E2E_PINECONE_NAMESPACE", "E2E_PINECONE_DIMENSION",
    "E2E_DIFFERENTIAL_LEGACY_URL", "E2E_DIFFERENTIAL_LEGACY_AUDIENCE",
    "E2E_DIFFERENTIAL_EVIDENCE_URI",
    "E2E_MAIN_SHA", "E2E_EVIDENCE_URI",
}
ENVIRONMENT_RESOURCE_NAMES = {
    "google_storage_bucket_iam_member": {"producer_core_objects"},
    "google_logging_metric": {
        "poll_not_found", "poll_gone", "terminal_total", "terminal_incorrect",
        "reconciler_run", "reconciler_fenced_leases", "reconciler_errors",
        "deadline_terminalized", "manual_reconciliation", "forusbots_failure",
        "pinecone_circuit_open", "queue_delay", "jobs_active",
        "jobs_oldest_age", "step_latency", "result_count", "forusbots_count",
        "forusbots_circuit", "pinecone_retry", "pinecone_circuit",
        "llm_parse", "llm_fallback", "llm_tokens", "llm_cost", "n8n_poll",
    },
    "google_monitoring_alert_policy": {
        "legacy_high_error_rate", "ticket_poll_not_found", "ticket_poll_gone",
        "ticket_terminal_incorrect_ratio", "ticket_queue_backlog", "worker_5xx_ratio",
        "producer_auth_failure_ratio", "ticket_lease_fencing",
        "ticket_reconciler_health", "ticket_forusbots_reconciliation",
        "ticket_pinecone_circuit", "ticket_task_delivery_deadline",
        "ticket_billable_time_budget",
        "ticket_oldest_active_job", "ticket_llm_cost_budget",
    },
    "google_monitoring_dashboard": {"ticket_operations"},
    "google_secret_manager_secret_iam_member": {
        "runtime_accessor", "e2e_runtime_accessor",
    },
    "google_firestore_field": {
        "payload_ttl", "control_ttl", "receipt_ttl", "rate_window_ttl",
        "ticket_execution_ttl", "core_execution_ttl",
    },
    "google_firestore_index": {
        "jobs_principal_state", "jobs_state_lease", "jobs_outbox",
        "jobs_state_created_at",
    },
    "google_cloud_run_v2_service": {"producer", "worker"},
    "google_cloud_run_v2_job": {"reconciler", "e2e"},
}


def _runtime_secret_contract(
    environment: str, release_phase: str,
) -> tuple[set[str], dict[str, set[str]], dict[str, set[str]]]:
    """Return the exact manifest, runtime injection, and accessor contract."""
    if environment not in {"staging", "production"}:
        raise ControllerRejected("runtime secret environment is invalid")
    if release_phase not in RELEASE_PHASE_HANDLER_MODES:
        raise ControllerRejected("runtime secret release phase is invalid")
    if release_phase == "infra_only":
        return set(), {"producer": set(), "worker": set()}, {}

    producer_base = {"API_KEY", "OPENAI_API_KEY", "PINECONE_API_KEY"}
    producer_active = set(producer_base)
    if environment == "staging" \
            and release_phase not in ACTIVE_TICKET_RELEASE_PHASES:
        inventory = {"API_KEY", *WORKER_SECRET_REF_KEYS}
        service_keys = {
            "producer": producer_base,
            "worker": set(WORKER_SECRET_REF_KEYS),
        }
    elif environment == "staging":
        inventory = set(STAGING_SECRET_REF_KEYS)
        service_keys = {
            "producer": {
                *producer_active, "TICKET_FAULT_SIGNING_SECRET",
            },
            "worker": {
                *WORKER_SECRET_REF_KEYS, "TICKET_FAULT_SIGNING_SECRET",
            },
        }
    else:
        inventory = set(PRODUCTION_SECRET_REF_KEYS)
        service_keys = {
            "producer": producer_active,
            "worker": set(WORKER_SECRET_REF_KEYS),
        }
    accessor_roles = {
        key: {
            role for role, keys in service_keys.items() if key in keys
        }
        for key in inventory
    }
    return inventory, service_keys, accessor_roles
PLATFORM_RESOURCE_NAMES = {
    "google_artifact_registry_repository": {"images"},
    "google_artifact_registry_repository_iam_member": {
        "controller_reader", "test_only_runtime_reader",
        "environment_apply_runtime_reader", "image_writer",
    },
    "google_cloudbuild_trigger": {
        "ci", "main_canonical", "platform_plan", "platform_apply",
        "staging_plan", "staging_apply", "production_plan", "production_apply",
        "staging_attest", "evidence_manifest", "test_only", "e2e_image",
        "runtime_attest",
        "staging_observe", "rollback_observe", "gate_receipt",
    },
    "google_firestore_database": {"environment"},
    "google_cloud_tasks_queue": {"environment"},
    "google_cloud_tasks_queue_iam_member": {
        "runtime_producer_queue", "runtime_reconciler_queue",
        "platform_apply_queue_task_inspector",
    },
    "google_cloud_scheduler_job": {"environment"},
    "google_cloud_run_v2_service_iam_member": {
        "environment_apply_developer", "task_signer_invokes_worker",
        "n8n_invokes_producer", "e2e_invokes_staging_producer",
        "production_preserved_invoker",
    },
    "google_cloud_run_v2_job_iam_member": {
        "environment_apply_developer", "scheduler_runs_reconciler",
    },
    "google_project_iam_custom_role": {
        "platform_storage_admin", "environment_plan_reader", "platform_plan_reader",
        "platform_secret_container_broker", "platform_scheduler_broker",
        "platform_queue_broker", "platform_run_iam_broker",
        "platform_queue_task_inspector",
        "environment_run_creator",
        "staging_observer_run_reader",
        "build_provenance_reader", "platform_firestore_database_broker",
        "environment_bucket_iam_reader", "terraform_plan_lock",
        "environment_secret_container_admin", "environment_bucket_iam_admin",
        "platform_project_iam_broker",
        "ticket_queue_enqueuer",
    },
    "google_project_iam_member": {
        "image_scanner", "platform_apply_secret_broker",
        "pipeline_logs", "plan_functional", "environment_apply_metadata_reader",
        "platform_apply_iam_broker",
        "production_release_approver", "build_provenance_reader",
        "kb_rag_runner_firestore_legacy", "runtime_firestore",
        "environment_apply_index_admin",
        "apply_functional", "kb_rag_runner_firestore_scoped",
        "platform_apply_storage", "platform_apply_firestore_broker",
        "runtime_vertex", "runtime_telemetry",
        "environment_apply_secret_admin", "environment_run_creator",
        "platform_apply_queue_broker", "platform_apply_run_iam_broker",
        "platform_apply_scheduler_broker",
        "staging_observer_run_reader", "gate_receipt_approver",
    },
    "google_project_service": {"enabled"},
    "google_secret_manager_secret": {"environment"},
    "google_service_account": {
        "e2e_image", "controller_builder", "controller_verifier",
        "apply_staging", "plan_platform",
        "evidence_manifest", "runtime", "apply_platform", "ci",
        "apply_production", "plan_staging", "runtime_attest", "plan_production",
        "staging_attest", "test_only",
        "staging_observer", "gate_receipt",
    },
    "google_service_account_iam_member": {
        "environment_apply_actas",
        "cloud_build_executes_as", "production_release_group",
        "runtime_producer_actas_signer", "runtime_reconciler_actas_signer",
        "tasks_agent_signs_as_runtime_signer",
        "platform_apply_actas_scheduler",
    },
    "google_storage_bucket": {"evidence"},
    "google_storage_bucket_iam_member": {
        "aux_evidence_reader", "plan_evidence_writer", "plan_state_lock",
        "apply_evidence_reader", "aux_evidence_writer", "apply_state_admin",
        "e2e_runtime_evidence_writer", "platform_apply_evidence_writer",
        "staging_apply_rag_bucket_iam", "builder_evidence_writer",
        "plan_state_viewer",
        "staging_observer_evidence_writer", "staging_observer_e2e_reader",
        "staging_plan_rag_bucket_reader",
        "controller_verifier_source_reader",
    },
}
PLATFORM_IAM_ROLE_POLICY = {
    ("google_artifact_registry_repository_iam_member", "controller_reader"):
        {"roles/artifactregistry.reader"},
    ("google_artifact_registry_repository_iam_member", "test_only_runtime_reader"):
        {"roles/artifactregistry.reader"},
    ("google_artifact_registry_repository_iam_member", "environment_apply_runtime_reader"):
        {"roles/artifactregistry.reader"},
    ("google_artifact_registry_repository_iam_member", "image_writer"):
        {"roles/artifactregistry.writer"},
    ("google_cloud_tasks_queue_iam_member", "runtime_producer_queue"):
        {"projects/{project}/roles/ticketQueueEnqueuerStaging",
         "projects/{project}/roles/ticketQueueEnqueuerProduction"},
    ("google_cloud_tasks_queue_iam_member", "runtime_reconciler_queue"):
        {"projects/{project}/roles/ticketQueueEnqueuerStaging",
         "projects/{project}/roles/ticketQueueEnqueuerProduction"},
    ("google_cloud_tasks_queue_iam_member", "platform_apply_queue_task_inspector"):
        {"projects/{project}/roles/ticketTfPlatformQueueTaskInspector"},
    ("google_cloud_run_v2_service_iam_member", "environment_apply_developer"):
        {"roles/run.developer"},
    ("google_cloud_run_v2_job_iam_member", "environment_apply_developer"):
        {"roles/run.developer"},
    ("google_cloud_run_v2_service_iam_member", "task_signer_invokes_worker"):
        {"roles/run.invoker"},
    ("google_cloud_run_v2_job_iam_member", "scheduler_runs_reconciler"):
        {"roles/run.invoker"},
    ("google_cloud_run_v2_service_iam_member", "n8n_invokes_producer"):
        {"roles/run.invoker"},
    ("google_cloud_run_v2_service_iam_member", "e2e_invokes_staging_producer"):
        {"roles/run.invoker"},
    ("google_cloud_run_v2_service_iam_member", "production_preserved_invoker"):
        {"roles/run.invoker"},
    ("google_project_iam_member", "plan_functional"):
        {"projects/{project}/roles/ticketTfPlatformPlanRead",
         "projects/{project}/roles/ticketTfEnvironmentPlanRead"},
    ("google_project_iam_member", "build_provenance_reader"):
        {"projects/{project}/roles/ticketBuildProvenanceRead"},
    ("google_project_iam_member", "staging_observer_run_reader"):
        {"projects/{project}/roles/ticketStagingObserverRunRead"},
    ("google_project_iam_member", "apply_functional"): {
        "roles/serviceusage.serviceUsageAdmin", "roles/artifactregistry.admin",
        "roles/iam.serviceAccountAdmin", "roles/iam.roleAdmin",
        "roles/cloudbuild.builds.editor",
        "roles/logging.configWriter", "roles/monitoring.editor",
        "roles/serviceusage.serviceUsageConsumer",
    },
    ("google_project_iam_member", "environment_run_creator"):
        {"projects/{project}/roles/ticketTfEnvironmentRunCreate"},
    ("google_project_iam_member", "environment_apply_index_admin"):
        {"roles/datastore.indexAdmin"},
    ("google_project_iam_member", "environment_apply_secret_admin"):
        {"projects/{project}/roles/ticketTfEnvironmentSecretAdmin"},
    ("google_project_iam_member", "platform_apply_storage"):
        {"projects/{project}/roles/ticketTfPlatformStorageAdmin"},
    ("google_project_iam_member", "platform_apply_iam_broker"):
        {"projects/{project}/roles/ticketTfPlatformIamBroker"},
    ("google_project_iam_member", "platform_apply_firestore_broker"):
        {"projects/{project}/roles/ticketTfPlatformFirestore"},
    ("google_project_iam_member", "platform_apply_secret_broker"):
        {"projects/{project}/roles/ticketTfPlatformSecrets"},
    ("google_project_iam_member", "platform_apply_queue_broker"):
        {"projects/{project}/roles/ticketTfPlatformQueues"},
    ("google_project_iam_member", "platform_apply_scheduler_broker"):
        {"projects/{project}/roles/ticketTfPlatformScheduler"},
    ("google_project_iam_member", "platform_apply_run_iam_broker"):
        {"projects/{project}/roles/ticketTfPlatformRunIam"},
    ("google_project_iam_member", "environment_apply_metadata_reader"):
        {"projects/{project}/roles/ticketTfEnvironmentPlanRead"},
    ("google_project_iam_member", "image_scanner"):
        {"roles/ondemandscanning.admin"},
    ("google_project_iam_member", "production_release_approver"):
        {"roles/cloudbuild.builds.approver"},
    ("google_project_iam_member", "gate_receipt_approver"):
        {"roles/cloudbuild.builds.approver"},
    ("google_project_iam_member", "pipeline_logs"): {"roles/logging.logWriter"},
    ("google_project_iam_member", "kb_rag_runner_firestore_legacy"):
        {"roles/datastore.user"},
    ("google_project_iam_member", "kb_rag_runner_firestore_scoped"):
        {"roles/datastore.user"},
    ("google_project_iam_member", "runtime_firestore"): {"roles/datastore.user"},
    ("google_project_iam_member", "runtime_vertex"): {"roles/aiplatform.user"},
    ("google_project_iam_member", "runtime_telemetry"):
        {"roles/logging.logWriter", "roles/monitoring.metricWriter"},
    ("google_service_account_iam_member", "environment_apply_actas"):
        {"roles/iam.serviceAccountUser"},
    ("google_service_account_iam_member", "platform_apply_actas_scheduler"):
        {"roles/iam.serviceAccountUser"},
    ("google_service_account_iam_member", "cloud_build_executes_as"):
        {"roles/iam.serviceAccountTokenCreator"},
    ("google_service_account_iam_member", "production_release_group"):
        {"roles/iam.serviceAccountUser"},
    ("google_service_account_iam_member", "runtime_producer_actas_signer"):
        {"roles/iam.serviceAccountUser"},
    ("google_service_account_iam_member", "runtime_reconciler_actas_signer"):
        {"roles/iam.serviceAccountUser"},
    ("google_service_account_iam_member", "tasks_agent_signs_as_runtime_signer"):
        {"roles/iam.serviceAccountTokenCreator"},
    ("google_storage_bucket_iam_member", "staging_apply_rag_bucket_iam"):
        {"projects/{project}/roles/ticketTfEnvironmentBucketIam"},
    ("google_storage_bucket_iam_member", "staging_plan_rag_bucket_reader"):
        {"projects/{project}/roles/ticketTfEnvironmentBucketRead"},
    ("google_storage_bucket_iam_member", "plan_state_viewer"):
        {"roles/storage.objectViewer"},
    ("google_storage_bucket_iam_member", "plan_state_lock"):
        {"projects/{project}/roles/ticketTerraformPlanLock"},
    ("google_storage_bucket_iam_member", "apply_state_admin"):
        {"roles/storage.objectAdmin"},
    ("google_storage_bucket_iam_member", "plan_evidence_writer"):
        {"roles/storage.objectCreator"},
    ("google_storage_bucket_iam_member", "apply_evidence_reader"):
        {"roles/storage.objectViewer"},
    ("google_storage_bucket_iam_member", "platform_apply_evidence_writer"):
        {"roles/storage.objectCreator"},
    ("google_storage_bucket_iam_member", "builder_evidence_writer"):
        {"roles/storage.objectCreator"},
    ("google_storage_bucket_iam_member", "e2e_runtime_evidence_writer"):
        {"roles/storage.objectCreator"},
    ("google_storage_bucket_iam_member", "aux_evidence_writer"):
        {"roles/storage.objectCreator"},
    ("google_storage_bucket_iam_member", "aux_evidence_reader"):
        {"roles/storage.objectViewer"},
    ("google_storage_bucket_iam_member", "staging_observer_evidence_writer"):
        {"roles/storage.objectCreator"},
    ("google_storage_bucket_iam_member", "staging_observer_e2e_reader"):
        {"roles/storage.objectViewer"},
    ("google_storage_bucket_iam_member", "controller_verifier_source_reader"):
        {"roles/storage.objectViewer"},
}
PLATFORM_CUSTOM_ROLE_PERMISSION_HASHES = {
    "platform_plan_reader": "6469b93f55fb5584e60bc3b618109605a05b9fa7996c7b6a9ff0dcd92d8d8973",
    "environment_plan_reader": "259db6f0e92627a3c5c8db7ba53065e4b2b0613f146f4c9c01b5d4a8a09f0093",
    "build_provenance_reader": "5a0938d19dfff471dde0c569cefbd8349644317c01576c9c32daec9b6b664b85",
    "platform_storage_admin": "57c57f39c9add2559577ee1e3c807edf59152d7d341efa40b08f860602c1061c",
    "platform_firestore_database_broker": "7c25e9e01fd48da11ee48cafb5f446e457c6ebdbe715702e5fbfd92f80261004",
    "platform_scheduler_broker": "26c7b14d6e9fca72cdc4f31c831164514abaca16946e307c9c99849142a6734d",
    "platform_queue_broker": "c767e55d9cfbb8c916435fd8a918e1b0b517405a7adeaaedf0dff1db2013c979",
    "platform_queue_task_inspector": "fda3f364f0792ea157f2eaf7879950cd7a4ec11ab569d95f85a0737fa2945f5e",
    "platform_run_iam_broker": "b9d235f5b1b4e68af8a122247e0f5b4d2cce73ca393565f8de5bf6611e14f05b",
    "environment_run_creator": "8fce42c9be3cd167e4d0215e037701286e8dc41a43e28214370b2c4330e5e9f6",
    "environment_bucket_iam_admin": "465ffc4450b6c3f3d2289ee2674dc02962063db32c3be7d4cac17db2b719e79b",
    "platform_project_iam_broker": "6e0c7cd5fabf9673914f00ebedbb18ea573f13b63e45d7650503000657e693da",
    "platform_secret_container_broker": "6b48c08d41bca3da7cb971ea60a39a75540b5a7e9be76215991849375651bf11",
    "environment_secret_container_admin": "e58ca9414b9fab76373efab6f9b3b54a75cf385cbb03add06ab06300f297275b",
    "environment_bucket_iam_reader": "c6babb702e21935e8ca1718d4739395b0d4f1165b988ad9ffa773d5656d060c2",
    "terraform_plan_lock": "b996701c2870616421bfa7648507759f21929e321fe199fb0a68eccf021cb88d",
    "ticket_queue_enqueuer": "b9f9a14828112960b41df51bbb02c51822ceecad6a0a7510b7cb18e1779e0682",
    "staging_observer_run_reader": "7da848e5ba4916b8a6e3dae582940dd9cd8823ed70fb7656fb38e67800eaa4bc",
}


class ControllerRejected(RuntimeError):
    """An input or observed artifact violated a release invariant."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require_sha(value: str, label: str = "SHA") -> str:
    if SHA_RE.fullmatch(value or "") is None:
        raise ControllerRejected(f"{label} must be a full 40-character SHA")
    return value


def _require_hash(value: str, label: str = "SHA-256") -> str:
    if HASH_RE.fullmatch(value or "") is None:
        raise ControllerRejected(f"{label} must be 64 lowercase hex characters")
    return value


def _require_digest(value: str, label: str = "image digest") -> str:
    if DIGEST_RE.fullmatch(value or "") is None:
        raise ControllerRejected(f"{label} must use repository@sha256:digest")
    return value


def _require_https_origin(value: str, label: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ControllerRejected(f"{label} must be a reviewed exact origin") from exc
    normalized = value.rstrip("/")
    reviewed_legacy_http = normalized == "http://35.224.156.104:10000"
    if parsed.scheme not in {"http", "https"} or not parsed.hostname \
            or parsed.username is not None or parsed.password is not None \
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment \
            or (port is not None and not 1 <= port <= 65_535) \
            or parsed.scheme == "http" and not reviewed_legacy_http:
        raise ControllerRejected(f"{label} must be a reviewed exact origin")
    return normalized


def _validate_reviewed_llm_pricing(raw: str, route_models: Iterable[str]) -> None:
    """Match the immutable environment module's exact pricing contract."""
    if len(raw.encode("utf-8")) > 32_768:
        raise ControllerRejected("LLM pricing JSON exceeds its reviewed bound")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        pricing = json.loads(raw, object_pairs_hook=unique_object)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ControllerRejected("LLM pricing JSON is invalid") from exc
    if not isinstance(pricing, dict) or set(pricing) != {
        "pricing_as_of", "source", "models",
    } or pricing["pricing_as_of"] != "2026-07-21" \
            or pricing["source"] != "openai-google-official-public-pricing":
        raise ControllerRejected("LLM pricing manifest is outside the reviewed contract")

    expected_keys = {
        f"{'openai' if model.startswith('gpt-') else 'gemini'}:{model}"
        for model in route_models
    }
    if any(key.startswith("openai:") for key in expected_keys):
        expected_keys.add("gemini:gemini-2.5-pro")
    if any(key.startswith("gemini:") for key in expected_keys):
        expected_keys.add("openai:gpt-5.5")
    models = pricing["models"]
    if not isinstance(models, dict) or set(models) != expected_keys:
        raise ControllerRejected("LLM pricing model coverage is not exact")
    rate_keys = {"input_usd_per_million", "output_usd_per_million"}
    for rates in models.values():
        if not isinstance(rates, dict) or set(rates) != rate_keys:
            raise ControllerRejected("LLM pricing rates have an invalid schema")
        for rate in rates.values():
            if isinstance(rate, bool) or not isinstance(rate, (int, float)) \
                    or not math.isfinite(float(rate)) or not 0 <= rate <= 500:
                raise ControllerRejected("LLM pricing rate is outside reviewed bounds")


def _run_resource_list(value: str, *, environment: str) -> list[str]:
    allowed = {
        "staging": {
            "services/kb-rag-system-staging",
            "services/kb-rag-ticket-worker-staging",
            "jobs/ticket-reconciler-staging", "jobs/ticket-e2e-staging",
        },
        "production": {
            "services/kb-rag-system", "services/kb-rag-ticket-worker",
            "jobs/ticket-reconciler-prod",
        },
    }[environment]
    resources = [] if value == "" else value.split(",")
    if len(resources) != len(set(resources)) or not set(resources).issubset(allowed):
        raise ControllerRejected(f"invalid {environment} Run resource inventory")
    return sorted(resources)


def _account_set(value: str, *, gate: str) -> set[str]:
    accounts = set() if not value else set(value.split(","))
    if any(
        not account or account.strip() != account
        or re.fullmatch(r"[A-Za-z0-9._%+:-]+@[A-Za-z0-9.-]+", account) is None
        for account in accounts
    ):
        raise ControllerRejected(f"{gate} approver account allowlist is invalid")
    return accounts


def _receipt_build_ids(value: str) -> dict[str, str]:
    pairs = [] if value == "" else value.split(",")
    result: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ControllerRejected("gate receipt build IDs are invalid")
        key, build_id = pair.split("=", 1)
        if not re.fullmatch(r"[a-z0-9-]+", key) \
                or not re.fullmatch(r"[A-Za-z0-9-]{8,128}", build_id) \
                or key in result:
            raise ControllerRejected("gate receipt build IDs are invalid")
        result[key] = build_id
    return result


def _platform_pipeline_inputs(args, controller_digest: str) -> dict[str, Any]:
    """Normalize the exact post-G1B declarative pipeline inputs."""
    cicd_digest = args.cicd_bootstrap_controller_digest
    if cicd_digest:
        _require_digest(cicd_digest, "CI/CD bootstrap controller")
        if cicd_digest != controller_digest:
            raise ControllerRejected(
                "CI/CD bootstrap controller must equal the executing controller"
            )
    enabled = bool(cicd_digest)
    try:
        accounts = json.loads(args.gate_approver_accounts_json)
    except json.JSONDecodeError as exc:
        raise ControllerRejected("gate approver accounts JSON is invalid") from exc
    expected_keys = set(GATE_SERVICE_ACCOUNT_IDS)
    if not isinstance(accounts, dict) or (
        enabled and set(accounts) != expected_keys
    ) or (not enabled and accounts):
        raise ControllerRejected("gate approver accounts do not match bootstrap phase")
    normalized_accounts: dict[str, list[str]] = {}
    for key, raw_accounts in accounts.items():
        if not isinstance(raw_accounts, list) or not raw_accounts:
            raise ControllerRejected("gate approver account lists must be nonempty")
        normalized = sorted(set(raw_accounts))
        if len(normalized) != len(raw_accounts) or any(
            not isinstance(account, str)
            or account != account.lower().strip()
            or account.endswith(".gserviceaccount.com")
            or re.fullmatch(
                r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}",
                account,
            ) is None
            for account in normalized
        ):
            raise ControllerRejected("gate approver account allowlist is invalid")
        normalized_accounts[key] = normalized
    email = args.production_release_group_email
    if cicd_digest:
        if not isinstance(email, str) or email != email.lower().strip() \
                or re.fullmatch(
                    r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}",
                    email,
                ) is None:
            raise ControllerRejected("CI/CD bootstrap requires an exact release group")
    elif email:
        raise ControllerRejected("release group is only valid for CI/CD bootstrap")
    legacy = args.enable_legacy_trigger_neutralization == "true"
    if legacy and not cicd_digest:
        raise ControllerRejected("legacy trigger neutralization requires CI/CD bootstrap")
    return {
        "cicd_bootstrap": {
            "enabled": bool(cicd_digest),
            "release_controller_image_digest": cicd_digest,
        },
        "gate_approver_accounts": normalized_accounts,
        "production_release_group_email": email,
        "enable_legacy_trigger_neutralization": legacy,
    }


def _require_generation_uri(value: str, label: str = "artifact URI") -> str:
    if GENERATION_URI_RE.fullmatch(value or "") is None:
        raise ControllerRejected(f"{label} must include an immutable generation")
    return value


def _manifest(body: Mapping[str, Any], hash_field: str) -> dict[str, Any]:
    result = dict(body)
    result[hash_field] = _sha256(_canonical_json(result))
    return result


def _verify_manifest(body: Mapping[str, Any], hash_field: str) -> None:
    expected = body.get(hash_field)
    if not isinstance(expected, str) or HASH_RE.fullmatch(expected) is None:
        raise ControllerRejected(f"missing or invalid {hash_field}")
    unsigned = {key: value for key, value in body.items() if key != hash_field}
    if _sha256(_canonical_json(unsigned)) != expected:
        raise ControllerRejected(f"{hash_field} does not match manifest bytes")


def _safe_relative_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ControllerRejected(f"symlink rejected: {path.relative_to(root)}")
        if path.is_file():
            yield path


def _strip_hcl_comments(text: str) -> str:
    """Remove HCL comments without treating comment markers in strings as code."""
    result: list[str] = []
    index = 0
    quoted = False
    escaped = False
    while index < len(text):
        char = text[index]
        if quoted:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            index += 1
            continue
        if char == '"':
            quoted = True
            result.append(char)
            index += 1
            continue
        if char == "#" or text.startswith("//", index):
            newline = text.find("\n", index)
            if newline == -1:
                break
            result.append("\n")
            index = newline + 1
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end == -1:
                raise ControllerRejected("Terraform block comment is unbalanced")
            comment = text[index:end + 2]
            result.append(" " + ("\n" * comment.count("\n")))
            index = end + 2
            continue
        result.append(char)
        index += 1
    if quoted:
        raise ControllerRejected("Terraform quoted string is unbalanced")
    return "".join(result)


def _compact_hcl(text: str) -> str:
    """Remove formatting whitespace while preserving quoted string bytes."""
    compact: list[str] = []
    quoted = False
    escaped = False
    for char in _strip_hcl_comments(text):
        if quoted:
            compact.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            compact.append(char)
        elif not char.isspace():
            compact.append(char)
    return "".join(compact)


def _hcl_block_matches(
    text: str, header: str, label: str,
) -> list[tuple[tuple[str, ...], str]]:
    """Extract selected HCL blocks, accounting for nested braces and strings."""
    bodies: list[tuple[tuple[str, ...], str]] = []
    for match in re.finditer(header, text):
        depth = 1
        quoted = False
        escaped = False
        index = match.end()
        while index < len(text) and depth:
            char = text[index]
            if escaped:
                escaped = False
            elif char == "\\" and quoted:
                escaped = True
            elif char == '"':
                quoted = not quoted
            elif not quoted and char == "{":
                depth += 1
            elif not quoted and char == "}":
                depth -= 1
            index += 1
        if depth:
            raise ControllerRejected(f"Terraform {label} block is unbalanced")
        bodies.append((match.groups(), text[match.end():index - 1]))
    return bodies


def _hcl_run_bodies(text: str) -> list[str]:
    """Extract run blocks conservatively while ignoring braces inside strings."""
    policy_text = _strip_hcl_comments(text)
    return [
        body for _groups, body in _hcl_block_matches(
            policy_text, r'\brun\s+"[^"]+"\s*\{', "test run",
        )
    ]


def _validate_candidate_hcl_policy(relative: Path, text: str) -> str:
    """Validate all candidate HCL execution surfaces before Terraform starts."""
    policy_text = _strip_hcl_comments(text)
    if re.search(r'\bephemeral\s+"', policy_text):
        raise ControllerRejected(
            f"unapproved Terraform resource type in {relative}: ephemeral"
        )
    for match in re.finditer(
        r'\bresource\s+"([^"]+)"\s+"[^"]+"\s*\{', policy_text,
    ):
        resource_type = match.group(1)
        if resource_type not in ALLOWED_TERRAFORM_RESOURCE_TYPES:
            raise ControllerRejected(
                f"unapproved Terraform resource type in {relative}: "
                f"{resource_type}"
            )

    allowed_data = ALLOWED_TERRAFORM_DATA_BLOCKS.get(relative.as_posix(), set())
    for groups, body in _hcl_block_matches(
        policy_text,
        r'\bdata\s+"([^"]+)"\s+"([^"]+)"\s*\{',
        "data source",
    ):
        data_type, data_name = groups
        data_block = (data_type, data_name, _compact_hcl(body))
        if data_block not in allowed_data:
            raise ControllerRejected(
                f"unapproved Terraform data source in {relative}: "
                f"{data_type}.{data_name}"
            )

    imports = [
        _compact_hcl(body) for _groups, body in _hcl_block_matches(
            policy_text, r"\bimport\s*\{", "import",
        )
    ]
    if len(imports) != len(set(imports)):
        raise ControllerRejected(
            f"duplicate Terraform import block rejected: {relative}"
        )
    allowed_imports = {
        _compact_hcl(body) for body in ALLOWED_TERRAFORM_IMPORT_BLOCKS.get(
            relative.as_posix(), (),
        )
    }
    if any(body not in allowed_imports for body in imports):
        raise ControllerRejected(
            f"unapproved Terraform import block rejected: {relative}"
        )
    if re.search(r"\bnonsensitive\s*\(", policy_text):
        raise ControllerRejected(
            f"Terraform output declassification rejected: {relative}"
        )
    function_names = "|".join(
        sorted(map(re.escape, TERRAFORM_FILESYSTEM_FUNCTIONS), key=len, reverse=True)
    )
    if re.search(rf"\b(?:{function_names})\s*\(", policy_text):
        raise ControllerRejected(
            f"Terraform filesystem function rejected: {relative}"
        )
    if re.search(
        r"\b(?:[A-Za-z0-9_]*(?:custom_endpoint|endpoint_override)|"
        r"universe_domain)\s*=",
        policy_text,
    ):
        raise ControllerRejected(
            f"Terraform provider endpoint override rejected: {relative}"
        )
    return policy_text


def validate_terraform_tree(candidate_root: Path) -> None:
    """Reject candidate-controlled Terraform execution and remote code loading."""
    terraform_root = candidate_root / "infra" / "terraform"
    if not terraform_root.is_dir():
        raise ControllerRejected("candidate has no infra/terraform tree")
    terraform_root = terraform_root.resolve()
    tf_files = list(terraform_root.rglob("*.tf"))
    if not tf_files:
        raise ControllerRejected("candidate has no Terraform files")
    allowed_backend_files = {
        Path("live") / environment / "backend.tf"
        for environment in ENVIRONMENTS
    }
    for path in _safe_relative_files(terraform_root):
        relative = path.relative_to(terraform_root)
        name = path.name
        implicit_input = (
            name.endswith((".tf.json", ".tftest.json"))
            or name in {
                "override.tf", "override.tf.json",
                "terraform.tfvars", "terraform.tfvars.json",
            }
            or name.endswith((
                "_override.tf", "_override.tf.json",
                ".auto.tfvars", ".auto.tfvars.json",
            ))
        )
        if implicit_input:
            raise ControllerRejected(
                f"candidate Terraform implicit input rejected: {relative}"
            )
        if ".terraform" in relative.parts \
                or path.name.endswith((".tfplan", ".tfstate", ".tfstate.backup")):
            raise ControllerRejected(
                f"candidate Terraform artifact/cache rejected: {relative}"
            )
        if path.name.endswith(".tftest.hcl"):
            test_text = path.read_text(encoding="utf-8")
            policy_text = _validate_candidate_hcl_policy(
                relative, test_text,
            )
            forbidden = (
                r'\bdata\s+"external"', r'\bprovisioner\s+"',
                r'\b(?:local-exec|remote-exec)\b',
                r'\b(?:override_provider|provider)\s+"',
            )
            if any(re.search(pattern, policy_text) for pattern in forbidden):
                raise ControllerRejected(
                    f"Terraform test execution escape hatch rejected: {relative}"
                )
            for source_value in re.findall(
                r'\bsource\s*=\s*"([^"]+)"', policy_text,
            ):
                if source_value in ALLOWED_PROVIDER_SOURCES:
                    continue
                raise ControllerRejected(
                    f"unapproved Terraform test source: {source_value}"
                )
            runs = _hcl_run_bodies(test_text)
            if any(
                re.search(r"\bcommand\s*=\s*plan\b", body) is None
                for body in runs
            ):
                mocked = set(re.findall(r'\bmock_provider\s+"([^"]+)"', test_text))
                if mocked != {"google", "google-beta"}:
                    raise ControllerRejected(
                        "apply-capable Terraform tests require only the complete "
                        f"reviewed mock provider set: {relative}"
                    )
            continue
        if path.suffix != ".tf":
            continue
        text = path.read_text(encoding="utf-8")
        policy_text = _validate_candidate_hcl_policy(relative, text)
        backends = re.findall(r'\bbackend\s+"([^"]+)"', policy_text)
        if relative in allowed_backend_files:
            if backends != ["gcs"]:
                raise ControllerRejected(
                    f"reviewed gcs backend declaration missing: {relative}"
                )
        elif backends:
            raise ControllerRejected(
                f"unapproved Terraform backend declaration: {relative}"
            )
        forbidden = (
            r'\bdata\s+"external"',
            r'\bresource\s+"null_resource"',
            r'\bresource\s+"terraform_data"',
            r'\bprovisioner\s+"',
            r'\b(?:local-exec|remote-exec)\b',
            r'\b(?:access_token|credentials|impersonate_service_account)\s*=',
            r'\b(?:[A-Za-z0-9_]*(?:custom_endpoint|endpoint_override)|'
            r'universe_domain)\s*=',
        )
        if any(re.search(pattern, policy_text) for pattern in forbidden):
            raise ControllerRejected(
                f"Terraform execution escape hatch rejected in {path}"
            )
        for source in re.findall(r'\bsource\s*=\s*"([^"]+)"', policy_text):
            if source in ALLOWED_PROVIDER_SOURCES:
                continue
            if source.startswith(("./", "../")):
                resolved_source = (path.parent / source).resolve()
                if resolved_source.is_relative_to(terraform_root):
                    continue
            raise ControllerRejected(f"unapproved Terraform source: {source}")
    for environment in ENVIRONMENTS:
        lock = terraform_root / "live" / environment / ".terraform.lock.hcl"
        if not lock.is_file() or not lock.read_text(encoding="utf-8").strip():
            raise ControllerRejected(f"missing provider lock for {environment}")
        lock_text = lock.read_text(encoding="utf-8")
        providers = set(re.findall(r'provider\s+"registry\.terraform\.io/([^"]+)"', lock_text))
        if not providers or not providers.issubset(ALLOWED_PROVIDER_SOURCES):
            raise ControllerRejected(
                f"provider lock for {environment} is empty or not allowlisted"
            )


def _sanitize_terraform_show(text: str) -> str:
    suspicious = re.compile(
        r'(?i)(?:secret(?:_data)?|password|private_key|access_token|api_key)\s*='
        r'\s*"(?!\(sensitive value\))'
    )
    if suspicious.search(text):
        raise ControllerRejected("terraform show contains an unredacted secret")
    if "-----BEGIN PRIVATE KEY-----" in text:
        raise ControllerRejected("terraform show contains private key material")
    return text


def _json_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for child in value.values():
            yield from _json_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _json_strings(child)


def _json_values_for_key(value: Any, target: str) -> Iterable[Any]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key == target:
                yield child
            yield from _json_values_for_key(child, target)
    elif isinstance(value, list):
        for child in value:
            yield from _json_values_for_key(child, target)


def _walk_json(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _validate_environment_plan(
    plan: Any, *, environment: str, project_id: str,
    tfvars: Mapping[str, Any], image_digest: str, release_phase: str,
) -> None:
    """Constrain privileged environment plans to their reviewed resource slice."""
    if not isinstance(plan, dict) or not isinstance(plan.get("resource_changes"), list):
        raise ControllerRejected("Terraform JSON plan has invalid shape")
    if release_phase not in RELEASE_PHASE_HANDLER_MODES \
            or environment == "production" and release_phase == "infra_only":
        raise ControllerRejected("Terraform environment release phase is invalid")
    expected_secret_inventory, service_secret_keys, _ = _runtime_secret_contract(
        environment, release_phase,
    )
    manifest_secret_refs = tfvars.get("secret_version_refs", {})
    if not isinstance(manifest_secret_refs, Mapping) \
            or set(manifest_secret_refs) != expected_secret_inventory:
        raise ControllerRejected(
            "Terraform plan secret inventory differs from the release phase"
        )
    expected_shadow_rate = 100 if release_phase == "shadow" else 0
    if tfvars.get("shadow_sample_rate", expected_shadow_rate) != expected_shadow_rate:
        raise ControllerRejected(
            "Terraform shadow sample input differs from the release phase"
        )
    suffix = "stg" if environment == "staging" else "prod"
    producer_service = "kb-rag-system-staging" if environment == "staging" \
        else "kb-rag-system"
    worker_service = "kb-rag-ticket-worker-staging" if environment == "staging" \
        else "kb-rag-ticket-worker"
    reconciler_job = "ticket-reconciler-staging" if environment == "staging" \
        else "ticket-reconciler-prod"
    e2e_job = "ticket-e2e-staging"
    database = "ticket-staging" if environment == "staging" else "(default)"
    queue = "ticket-jobs-staging" if environment == "staging" else "ticket-jobs-prod"
    allowed_emails = {
        f"{account_id}@{project_id}.iam.gserviceaccount.com"
        for account_id in RUNTIME_SERVICE_ACCOUNTS[environment]
    }
    if environment == "staging":
        allowed_emails.add(f"ticket-e2e-stg@{project_id}.iam.gserviceaccount.com")
    else:
        allowed_emails.update({
            f"kb-rag-runner@{project_id}.iam.gserviceaccount.com",
            f"kb-rag-client@{project_id}.iam.gserviceaccount.com",
        })
    allowed_emails.add(
        "service-900340137010@gcp-sa-cloudtasks.iam.gserviceaccount.com"
    )
    runtime_sas = tfvars.get("runtime_service_accounts", {})
    producer_email = (
        runtime_sas.get("ticket-producer-stg") if environment == "staging"
        else runtime_sas.get("ticket-producer-prod")
    )
    worker_email = runtime_sas.get(f"ticket-worker-{suffix}")
    reconciler_email = runtime_sas.get(f"ticket-reconciler-{suffix}")
    signer_email = runtime_sas.get(f"ticket-task-signer-{suffix}")
    scheduler_email = runtime_sas.get(f"ticket-scheduler-{suffix}")
    n8n_email = f"kb-rag-client@{project_id}.iam.gserviceaccount.com"
    e2e_email = f"ticket-e2e-stg@{project_id}.iam.gserviceaccount.com"
    exact_members = {
        "producer_queue": producer_email,
        "producer_actas_signer": producer_email,
        "signer_invokes_worker": signer_email,
        "tasks_agent_signs_as_signer": (
            "service-900340137010@gcp-sa-cloudtasks.iam.gserviceaccount.com"
        ),
        "reconciler_queue": reconciler_email,
        "reconciler_actas_signer": reconciler_email,
        "scheduler_runs_reconciler": scheduler_email,
        "n8n_invokes_producer": n8n_email,
        "producer_preserved_invokers": (
            f"kb-rag-client@{project_id}.iam.gserviceaccount.com"
            if environment == "production" else None
        ),
        "e2e_invokes_producer": e2e_email if environment == "staging" else None,
        "producer_vertex": producer_email,
        "worker_vertex": worker_email,
        "producer_core_objects": producer_email,
        "producer_firestore": producer_email,
        "worker_firestore": worker_email,
        "reconciler_firestore": reconciler_email,
    }
    secret_ids = set(tfvars.get("secret_containers", {}).get("ids", {}).values())
    e2e_secret_ids = set(tfvars.get("e2e_secret_containers", {}).values())
    expected_module = f"module.{environment}."
    address_re = re.compile(
        rf"^{re.escape(expected_module)}(google(?:-beta)?_[a-z0-9_]+)\."
        r"([a-z0-9_]+)(?:\[[^\]]+\])?$"
    )
    for raw_change in plan["resource_changes"]:
        if not isinstance(raw_change, dict) or raw_change.get("mode") != "managed":
            raise ControllerRejected("Terraform plan contains a non-managed resource")
        address = raw_change.get("address")
        match = address_re.fullmatch(address) if isinstance(address, str) else None
        if match is None or match.group(2) not in ENVIRONMENT_RESOURCE_NAMES.get(
            match.group(1), set(),
        ):
            raise ControllerRejected(f"unapproved Terraform resource address: {address}")
        resource_type, resource_name = match.groups()
        if release_phase == "infra_only" and resource_type in {
            "google_cloud_run_v2_service", "google_cloud_run_v2_job",
        }:
            raise ControllerRejected(
                "Terraform infra_only plan may not create runtime compute"
            )
        if raw_change.get("type") != resource_type or raw_change.get("name") != resource_name:
            raise ControllerRejected("Terraform resource address/type metadata mismatch")
        change = raw_change.get("change")
        if not isinstance(change, dict):
            raise ControllerRejected("Terraform resource change is missing")
        actions = change.get("actions")
        if not isinstance(actions, list) or not actions \
                or not set(actions).issubset({"no-op", "read", "create", "update", "delete"}):
            raise ControllerRejected("Terraform resource actions are invalid")
        if "delete" in actions and resource_type in {
            "google_cloud_run_v2_service", "google_cloud_run_v2_job",
            "google_cloud_tasks_queue", "google_firestore_database",
            "google_secret_manager_secret",
        }:
            raise ControllerRejected("Terraform plan may not delete critical runtime resources")
        values = [value for value in (change.get("before"), change.get("after"))
                  if isinstance(value, dict)]
        for value in values:
            project = value.get("project")
            if project not in {None, project_id}:
                raise ControllerRejected("Terraform resource targets another project")
            for email in re.findall(
                r"[a-z0-9-]+@[a-z][a-z0-9-]+\.iam\.gserviceaccount\.com",
                "\n".join(_json_strings(value)),
            ):
                if email not in allowed_emails:
                    raise ControllerRejected(
                        f"Terraform plan contains a cross-environment principal: {email}"
                    )
            member = value.get("member")
            if member is not None and member not in {
                f"serviceAccount:{email}" for email in allowed_emails
            }:
                raise ControllerRejected("Terraform IAM member is outside the root allowlist")
            if resource_name in exact_members:
                expected_member = exact_members[resource_name]
                if expected_member is None or member != f"serviceAccount:{expected_member}":
                    raise ControllerRejected(
                        "Terraform IAM member does not match its resource address"
                    )
            expected_roles: dict[str, set[str]] = {
                "signer_invokes_worker": {"roles/run.invoker"},
                "n8n_invokes_producer": {"roles/run.invoker"},
                "producer_preserved_invokers": {"roles/run.invoker"},
                "e2e_invokes_producer": {"roles/run.invoker"},
                "scheduler_runs_reconciler": {"roles/run.invoker"},
                "producer_actas_signer": {"roles/iam.serviceAccountUser"},
                "reconciler_actas_signer": {"roles/iam.serviceAccountUser"},
                "tasks_agent_signs_as_signer": {"roles/iam.serviceAccountTokenCreator"},
                "producer_vertex": {"roles/aiplatform.user"},
                "worker_vertex": {"roles/aiplatform.user"},
                "producer_firestore": {"roles/datastore.user"},
                "worker_firestore": {"roles/datastore.user"},
                "reconciler_firestore": {"roles/datastore.user"},
                "producer_core_objects": {"roles/storage.objectViewer"},
                "runtime_accessor": {"roles/secretmanager.secretAccessor"},
                "e2e_runtime_accessor": {"roles/secretmanager.secretAccessor"},
            }
            if resource_name in expected_roles \
                    and value.get("role") not in expected_roles[resource_name]:
                raise ControllerRejected("Terraform IAM role is outside the root allowlist")
            if resource_name in {"producer_queue", "reconciler_queue"}:
                role = value.get("role")
                if role is not None and not str(role).endswith(
                    f"/roles/ticketQueueEnqueuer{environment.title()}"
                ):
                    raise ControllerRejected("Terraform queue IAM role is cross-environment")
            if resource_type == "google_cloud_run_v2_service" \
                    and value.get("name") not in {producer_service, worker_service}:
                raise ControllerRejected("Terraform Cloud Run service name is cross-environment")
            if resource_type == "google_cloud_run_v2_job" \
                    and value.get("name") not in ({reconciler_job, e2e_job}
                                                   if environment == "staging"
                                                   else {reconciler_job}):
                raise ControllerRejected("Terraform Cloud Run job name is cross-environment")
            if resource_type == "google_cloud_tasks_queue" \
                    and value.get("name") != queue:
                raise ControllerRejected("Terraform queue name is cross-environment")
            if resource_type == "google_cloud_scheduler_job" \
                    and value.get("name") != f"{reconciler_job}-tick":
                raise ControllerRejected("Terraform scheduler name is cross-environment")
            if resource_type.startswith("google_firestore_"):
                planned_database = value.get(
                    "name" if resource_type == "google_firestore_database" else "database"
                )
                if planned_database != database:
                    raise ControllerRejected("Terraform database is cross-environment")
            if resource_type == "google_secret_manager_secret":
                if value.get("secret_id") not in secret_ids:
                    raise ControllerRejected("Terraform secret ID is outside the manifest")
            if resource_type == "google_secret_manager_secret_iam_member":
                planned_secret = str(value.get("secret_id", "")).rsplit("/", 1)[-1]
                expected_secret_ids = (
                    e2e_secret_ids if resource_name == "e2e_runtime_accessor"
                    else secret_ids
                )
                if planned_secret not in expected_secret_ids:
                    raise ControllerRejected("Terraform secret IAM is outside the manifest")
                if resource_name == "e2e_runtime_accessor" \
                        and member != f"serviceAccount:{e2e_email}":
                    raise ControllerRejected("Terraform E2E secret member is not exact")
                if resource_name == "runtime_accessor":
                    secret_keys = {
                        secret_id: key for key, secret_id in tfvars.get(
                            "secret_containers", {},
                        ).get("ids", {}).items()
                    }
                    key = secret_keys.get(planned_secret)
                    role_members = {
                        "producer": producer_email, "worker": worker_email,
                    }
                    expected_secret_members = {
                        f"serviceAccount:{role_members[role]}"
                        for role in tfvars.get("secret_containers", {}).get(
                            "accessor_roles", {},
                        ).get(key, [])
                    }
                    if member not in expected_secret_members:
                        raise ControllerRejected(
                            "Terraform runtime secret member is not exact"
                        )
            if resource_type == "google_storage_bucket_iam_member" \
                    and value.get("bucket") != "rag-kb-system-kb-articles":
                raise ControllerRejected("Terraform bucket IAM is cross-environment")
            if resource_type == "google_project_iam_custom_role" \
                    and value.get("role_id") != f"ticketQueueEnqueuer{environment.title()}":
                raise ControllerRejected("Terraform custom role ID is cross-environment")
            if resource_type == "google_project_iam_custom_role" \
                    and set(value.get("permissions", [])) != {
                        "cloudtasks.tasks.create", "cloudtasks.tasks.get",
                        "cloudtasks.queues.get",
                    }:
                raise ControllerRejected("Terraform queue role permissions are not exact")
            if resource_type == "google_project_iam_member" \
                    and value.get("role") == "roles/datastore.user":
                expected_database = f"projects/{project_id}/databases/{database}"
                expressions = [
                    text for text in _json_strings(value.get("condition", []))
                    if "resource.name" in text
                ]
                if expressions != [f'resource.name == "{expected_database}"']:
                    raise ControllerRejected("Terraform Firestore IAM database condition differs")
        if environment == "production" and resource_name == "e2e_invokes_producer":
            raise ControllerRejected("Terraform production plan contains E2E IAM")
        after = change.get("after")
        if isinstance(after, dict) and resource_type in {
            "google_cloud_run_v2_service", "google_cloud_run_v2_job",
        }:
            images = list(_json_values_for_key(after.get("template", {}), "image"))
            expected_image = image_digest
            if resource_type == "google_cloud_run_v2_job" and resource_name == "e2e":
                expected_image = str(tfvars.get("e2e_job", {}).get("image_digest", ""))
            if images != [expected_image]:
                raise ControllerRejected("Terraform runtime image is not the approved digest")
            expected_service_accounts = {
                "producer": producer_email,
                "worker": worker_email,
                "reconciler": reconciler_email,
                "e2e": e2e_email,
            }
            service_accounts = list(_json_values_for_key(after.get("template", {}),
                                                         "service_account"))
            if service_accounts != [expected_service_accounts[resource_name]]:
                raise ControllerRejected(
                    "Terraform runtime service account differs from the root contract"
                )
            env_lists = list(_json_values_for_key(after.get("template", {}), "env"))
            env_entries = [entry for entries in env_lists if isinstance(entries, list)
                           for entry in entries if isinstance(entry, dict)]
            app_roles = [entry.get("value") for entry in env_entries
                         if entry.get("name") == "APP_ROLE"]
            if app_roles != [resource_name]:
                raise ControllerRejected("Terraform APP_ROLE is not exclusive")
            if resource_name in {"producer", "worker", "reconciler"}:
                handler_modes = [
                    entry.get("value") for entry in env_entries
                    if entry.get("name") == "TICKET_HANDLER_MODE"
                ]
                if handler_modes != [RELEASE_PHASE_HANDLER_MODES[release_phase]]:
                    raise ControllerRejected(
                        "Terraform runtime handler mode differs from the release phase"
                    )
            if resource_name == "producer":
                shadow_rates = [
                    entry.get("value") for entry in env_entries
                    if entry.get("name") == "TICKET_SHADOW_SAMPLE_RATE"
                ]
                expected_rate = "1" if release_phase == "shadow" else "0"
                if shadow_rates != [expected_rate]:
                    raise ControllerRejected(
                        "Terraform runtime shadow sample differs from the release phase"
                    )
            expected_secret_refs: Mapping[str, str]
            if resource_name in {"producer", "worker"}:
                expected_secret_refs = {
                    key: manifest_secret_refs[key]
                    for key in service_secret_keys[resource_name]
                }
            elif resource_name == "e2e":
                expected_secret_refs = tfvars.get("e2e_job", {}).get(
                    "secret_version_refs", {},
                )
            else:
                expected_secret_refs = {}
            secret_entries = {
                str(entry.get("name")): entry
                for entry in env_entries if entry.get("value_source") not in (None, [])
            }
            if set(secret_entries) != set(expected_secret_refs):
                raise ControllerRejected("Terraform runtime secret env set differs")
            for key, ref in expected_secret_refs.items():
                secret_refs = list(_json_values_for_key(
                    secret_entries[key].get("value_source", {}), "secret",
                ))
                versions = list(_json_values_for_key(
                    secret_entries[key].get("value_source", {}), "version",
                ))
                expected_secret, expected_version = ref.rsplit("/versions/", 1)
                if secret_refs != [expected_secret] or versions != [expected_version]:
                    raise ControllerRejected(
                        "Terraform runtime secret ref differs from the manifest"
                    )
            if resource_type == "google_cloud_run_v2_service":
                expected_ingress = (
                    tfvars.get("producer_ingress", "INGRESS_TRAFFIC_ALL")
                    if resource_name == "producer"
                    else "INGRESS_TRAFFIC_INTERNAL_ONLY"
                )
                if after.get("ingress") != expected_ingress:
                    raise ControllerRejected("Terraform runtime ingress is cross-environment")
                traffic = after.get("traffic")
                if not isinstance(traffic, list) or any(
                    not isinstance(target, dict) for target in traffic
                ):
                    raise ControllerRejected("Terraform runtime traffic is invalid")
                normalized_traffic = [{
                    "type": target.get("type"),
                    "revision": target.get("revision") or "",
                    "percent": target.get("percent"),
                    "tag": target.get("tag") or "",
                } for target in traffic]
                if resource_name == "worker":
                    expected_traffic = [{
                        "type": "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST",
                        "revision": "", "percent": 100, "tag": "",
                    }]
                else:
                    baseline_revision = tfvars.get("producer_baseline_revision")
                    baseline_tag = tfvars.get("producer_baseline_tag", "baseline")
                    candidate_tag = tfvars.get("producer_candidate_tag", "candidate")
                    baseline_target = {
                        "type": "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION",
                        "revision": baseline_revision,
                        "percent": 100 if release_phase == "dark_no_traffic" else 0,
                        "tag": baseline_tag,
                    }
                    candidate_target = {
                        "type": "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST",
                        "revision": "",
                        "percent": 0 if release_phase == "dark_no_traffic" else 100,
                        "tag": candidate_tag,
                    }
                    if release_phase == "dark_no_traffic":
                        expected_traffic = [baseline_target, candidate_target]
                    elif environment == "staging" and tfvars.get(
                        "e2e_job", {},
                    ).get("enabled") is True:
                        expected_traffic = [baseline_target, candidate_target]
                    else:
                        expected_traffic = [candidate_target]
                if normalized_traffic != expected_traffic:
                    raise ControllerRejected(
                        "Terraform runtime traffic differs from the release phase"
                    )


def _validate_platform_plan(
    plan: Any, *, project_id: str,
    handoff: Optional[Mapping[str, Any]] = None,
    secret_inventory: Optional[Mapping[str, Any]] = None,
    container_phases: Optional[Mapping[str, str]] = None,
) -> None:
    if not isinstance(plan, dict) or not isinstance(plan.get("resource_changes"), list):
        raise ControllerRejected("Terraform platform JSON plan has invalid shape")
    address_re = re.compile(
        r"^(google(?:-beta)?_[a-z0-9_]+)\.([a-z0-9_]+)(?:\[[^\]]+\])?$"
    )
    handoff = dict(handoff or {
        "phases": {"staging": "disabled", "production": "disabled"},
        "resources": {"staging": [], "production": []},
    })
    phases = handoff.get("phases")
    resources = handoff.get("resources")
    closed_inventory = {
        "staging": {
            "services/kb-rag-system-staging",
            "services/kb-rag-ticket-worker-staging",
            "jobs/ticket-reconciler-staging",
            "jobs/ticket-e2e-staging",
        },
        "production": {
            "services/kb-rag-system", "services/kb-rag-ticket-worker",
            "jobs/ticket-reconciler-prod",
        },
    }
    container_phases = dict(container_phases or {
        "staging": "disabled", "production": "disabled",
    })
    if set(container_phases) != set(closed_inventory) \
            or any(value not in {"disabled", "managed"}
                   for value in container_phases.values()):
        raise ControllerRejected("platform container phase policy is invalid")
    if not isinstance(phases, dict) or set(phases) != set(closed_inventory) \
            or any(value not in {"disabled", "bootstrap", "managed"}
                   for value in phases.values()) \
            or not isinstance(resources, dict) or set(resources) != set(closed_inventory):
        raise ControllerRejected("platform handoff policy is invalid")
    if any(
        phases[environment] != "disabled"
        and container_phases[environment] != "managed"
        for environment in closed_inventory
    ):
        raise ControllerRejected("Run handoff requires managed environment containers")
    normalized_resources: dict[str, set[str]] = {}
    for environment, allowed in closed_inventory.items():
        raw_resources = resources[environment]
        if not isinstance(raw_resources, list) or len(raw_resources) != len(set(raw_resources)) \
                or not set(raw_resources).issubset(allowed):
            raise ControllerRejected("platform handoff inventory is invalid")
        normalized_resources[environment] = set(raw_resources)
        if phases[environment] == "disabled" and raw_resources:
            raise ControllerRejected("disabled handoff may not retain runtime inventory")
        if phases[environment] == "managed" and set(raw_resources) != allowed:
            raise ControllerRejected("managed handoff requires the complete runtime inventory")
    creator_environments: set[str] = set()
    developer_inventory: set[str] = set()
    observed_secret_ids: set[str] = set()
    observed_runtime_iam: dict[str, set[tuple[str, str]]] = {
        "staging": set(), "production": set(),
    }
    for raw_change in plan["resource_changes"]:
        if not isinstance(raw_change, dict) or raw_change.get("mode") != "managed":
            raise ControllerRejected("platform plan contains a non-managed resource")
        address = raw_change.get("address")
        match = address_re.fullmatch(address) if isinstance(address, str) else None
        if match is None or match.group(2) not in PLATFORM_RESOURCE_NAMES.get(
            match.group(1), set(),
        ):
            raise ControllerRejected(f"unapproved platform resource address: {address}")
        resource_type, resource_name = match.groups()
        index_match = re.search(r'(?:\["([^"]+)"\]|\[([0-9]+)\])$', str(address))
        address_index = (
            (index_match.group(1) or index_match.group(2)) if index_match else None
        )
        if raw_change.get("type") != resource_type or raw_change.get("name") != resource_name:
            raise ControllerRejected("platform resource address/type metadata mismatch")
        change = raw_change.get("change")
        if not isinstance(change, dict) or not isinstance(change.get("actions"), list):
            raise ControllerRejected("platform resource change/actions are invalid")
        actions = change["actions"]
        if not actions or not set(actions).issubset(
            {"no-op", "read", "create", "update", "delete"}
        ):
            raise ControllerRejected("platform resource actions are invalid")
        if "delete" in actions and resource_type in {
            "google_artifact_registry_repository", "google_firestore_database",
            "google_cloud_tasks_queue", "google_cloud_scheduler_job",
            "google_secret_manager_secret",
            "google_project_iam_custom_role", "google_service_account",
            "google_storage_bucket",
        }:
            raise ControllerRejected("platform plan may not delete critical resources")
        if "delete" in actions and resource_name == "environment_apply_developer":
            raise ControllerRejected("platform handoff inventory must be monotonic")
        if "delete" in actions and resource_name in {
            "environment", "ticket_queue_enqueuer", "runtime_producer_queue",
            "runtime_reconciler_queue", "runtime_producer_actas_signer",
            "runtime_reconciler_actas_signer", "tasks_agent_signs_as_runtime_signer",
            "task_signer_invokes_worker", "scheduler_runs_reconciler",
            "n8n_invokes_producer", "runtime_firestore", "runtime_vertex",
            "runtime_telemetry",
            "environment_apply_secret_admin", "e2e_invokes_staging_producer",
            "production_preserved_invoker", "platform_apply_queue_task_inspector",
            "environment_apply_actas",
        }:
            raise ControllerRejected("platform environment resource inventory is monotonic")
        for value in (change.get("before"), change.get("after")):
            if not isinstance(value, dict):
                continue
            if value.get("project") not in {None, project_id}:
                raise ControllerRejected("platform plan targets another project")
            role = value.get("role")
            if isinstance(role, str):
                expected_roles = {
                    expected.format(project=project_id)
                    for expected in PLATFORM_IAM_ROLE_POLICY.get(
                        (resource_type, resource_name), set(),
                    )
                }
                if role not in expected_roles:
                    raise ControllerRejected("platform IAM role is outside the exact allowlist")
            member = value.get("member")
            if member in {"allUsers", "allAuthenticatedUsers"}:
                raise ControllerRejected("platform IAM member may not be public")
            if isinstance(member, str) and "@" in member \
                    and not member.endswith(
                        f"@{project_id}.iam.gserviceaccount.com"
                    ) \
                    and "@gcp-sa-" not in member \
                    and not (resource_name in {
                        "production_release_group", "production_release_approver",
                    }
                             and member.startswith("group:")) \
                    and not (
                        resource_name == "gate_receipt_approver"
                        and member.startswith("user:")
                    ):
                raise ControllerRejected("platform IAM member is outside the project")
            if resource_type == "google_project_iam_custom_role":
                permissions = value.get("permissions")
                expected_hash = PLATFORM_CUSTOM_ROLE_PERMISSION_HASHES.get(resource_name)
                if not isinstance(permissions, list) or expected_hash is None \
                        or _sha256(_canonical_json(sorted(permissions))) != expected_hash:
                    raise ControllerRejected(
                        "platform custom role permissions are not exact"
                    )
        after = change.get("after")
        if not isinstance(after, dict):
            continue
        environment_owned_names = {
            "environment", "ticket_queue_enqueuer", "runtime_producer_queue",
            "runtime_reconciler_queue", "runtime_producer_actas_signer",
            "runtime_reconciler_actas_signer", "tasks_agent_signs_as_runtime_signer",
            "task_signer_invokes_worker", "scheduler_runs_reconciler",
            "n8n_invokes_producer", "runtime_firestore", "runtime_vertex",
            "runtime_telemetry",
            "environment_apply_secret_admin", "environment_apply_index_admin",
            "platform_apply_queue_task_inspector",
            "environment_apply_actas",
        }
        owned_environment: Optional[str] = None
        if resource_name in environment_owned_names:
            if address_index in {"staging", "production"}:
                owned_environment = address_index
            elif isinstance(address_index, str):
                if address_index.startswith("staging-"):
                    owned_environment = "staging"
                elif address_index.startswith("production-"):
                    owned_environment = "production"
        if resource_name == "e2e_invokes_staging_producer":
            owned_environment = "staging"
        if resource_name == "production_preserved_invoker":
            owned_environment = "production"
        if resource_type == "google_secret_manager_secret" \
                and isinstance(address_index, str):
            owned_environment = next(
                (name for name in ("staging", "production")
                 if address_index.startswith(f"{name}-")), None,
            )
        if owned_environment is not None \
                and container_phases[owned_environment] != "managed":
            raise ControllerRejected(
                f"platform {owned_environment} environment resources are gate-disabled"
            )
        if "delete" in actions and owned_environment is not None:
            raise ControllerRejected("platform environment resource inventory is monotonic")
        if resource_name in {
            "runtime_firestore", "runtime_vertex", "runtime_telemetry",
            "runtime_producer_queue", "runtime_reconciler_queue",
            "runtime_producer_actas_signer", "runtime_reconciler_actas_signer",
            "tasks_agent_signs_as_runtime_signer",
        } and owned_environment is not None and address_index is not None:
            observed_runtime_iam[owned_environment].add(
                (resource_name, address_index)
            )
        if resource_type == "google_project_iam_member" \
                and resource_name == "environment_run_creator":
            if address_index not in {"staging", "production"}:
                raise ControllerRejected("temporary Run creator has invalid environment key")
            environment = address_index
            expected_member = (
                f"serviceAccount:ticket-apply-{environment}@{project_id}."
                "iam.gserviceaccount.com"
            )
            if after.get("member") != expected_member \
                    or not str(after.get("role", "")).endswith(
                        "/roles/ticketTfEnvironmentRunCreate"
                    ):
                raise ControllerRejected("temporary Run creator binding is not exact")
            creator_environments.add(environment)
        if resource_name == "environment_apply_developer" and resource_type in {
            "google_cloud_run_v2_service_iam_member",
            "google_cloud_run_v2_job_iam_member",
        }:
            prefix = (
                "services/" if resource_type == "google_cloud_run_v2_service_iam_member"
                else "jobs/"
            )
            target = prefix + str(after.get("name", ""))
            if target not in set().union(*closed_inventory.values()):
                raise ControllerRejected("platform Run developer target is outside inventory")
            environment = (
                "staging" if target in closed_inventory["staging"] else "production"
            )
            expected_member = (
                f"serviceAccount:ticket-apply-{environment}@{project_id}."
                "iam.gserviceaccount.com"
            )
            if after.get("role") != "roles/run.developer" \
                    or after.get("member") != expected_member:
                raise ControllerRejected("platform Run developer binding is not exact")
            developer_inventory.add(target)
        environment_targets = {
            "google_firestore_database": {
                "staging": "ticket-staging", "production": "(default)",
            },
            "google_cloud_tasks_queue": {
                "staging": "ticket-jobs-staging", "production": "ticket-jobs-prod",
            },
            "google_cloud_scheduler_job": {
                "staging": "ticket-reconciler-staging-tick",
                "production": "ticket-reconciler-prod-tick",
            },
        }
        if resource_type in environment_targets:
            expected_target = environment_targets[resource_type].get(str(address_index))
            if expected_target is None or after.get("name") != expected_target:
                raise ControllerRejected(
                    "platform environment target does not match its environment index"
                )
            if resource_type == "google_cloud_scheduler_job" \
                    and after.get("paused") is not True:
                raise ControllerRejected(
                    "platform scheduler must remain paused until authenticated activation"
                )
        if resource_type == "google_project_iam_custom_role" \
                and resource_name == "ticket_queue_enqueuer":
            expected_role_id = {
                "staging": "ticketQueueEnqueuerStaging",
                "production": "ticketQueueEnqueuerProduction",
            }.get(str(address_index))
            if expected_role_id is None or after.get("role_id") != expected_role_id:
                raise ControllerRejected(
                    "platform queue role does not match its environment index"
                )
        if resource_type == "google_secret_manager_secret":
            secret_id = after.get("secret_id")
            if not isinstance(secret_id, str):
                raise ControllerRejected("platform secret container ID is invalid")
            observed_secret_ids.add(secret_id)
            environment = next((
                name for name, ids in (secret_inventory or {}).items()
                if isinstance(ids, list) and secret_id in ids
            ), None)
            if environment is None or address_index != f"{environment}-{secret_id}":
                raise ControllerRejected("platform secret address is outside exact inventory")
        if resource_type == "google_service_account" and resource_name == "runtime":
            if address_index not in PLATFORM_RUNTIME_SERVICE_ACCOUNTS \
                    or after.get("account_id") != address_index:
                raise ControllerRejected("platform runtime service account is not exact")
        if resource_type == "google_service_account" \
                and resource_name == "gate_receipt":
            expected_account = GATE_SERVICE_ACCOUNT_IDS.get(str(address_index))
            if expected_account is None or after.get("account_id") != expected_account:
                raise ControllerRejected("gate receipt service account is not exact")
        if resource_type == "google_cloudbuild_trigger" \
                and resource_name == "gate_receipt":
            expected_account = GATE_SERVICE_ACCOUNT_IDS.get(str(address_index))
            if expected_account is None \
                    or after.get("name") != f"handle-ticket-gate-{address_index}" \
                    or not str(after.get("service_account", "")).endswith(
                        f"/serviceAccounts/{expected_account}@{project_id}."
                        "iam.gserviceaccount.com"
                    ):
                raise ControllerRejected("gate receipt trigger target is not exact")
        if resource_type == "google_project_iam_member" \
                and resource_name == "plan_functional":
            expected = {
                "platform": (
                    f"projects/{project_id}/roles/ticketTfPlatformPlanRead",
                    f"serviceAccount:ticket-plan-platform@{project_id}.iam.gserviceaccount.com",
                ),
                "staging": (
                    f"projects/{project_id}/roles/ticketTfEnvironmentPlanRead",
                    f"serviceAccount:ticket-plan-staging@{project_id}.iam.gserviceaccount.com",
                ),
                "production": (
                    f"projects/{project_id}/roles/ticketTfEnvironmentPlanRead",
                    f"serviceAccount:ticket-plan-production@{project_id}.iam.gserviceaccount.com",
                ),
            }.get(str(address_index))
            if expected is None or (after.get("role"), after.get("member")) != expected:
                raise ControllerRejected("platform plan binding address/member is not exact")
        if resource_type == "google_project_iam_member" \
                and resource_name == "apply_functional":
            platform_roles = {
                "roles/serviceusage.serviceUsageAdmin", "roles/artifactregistry.admin",
                "roles/iam.serviceAccountAdmin", "roles/iam.roleAdmin",
                "roles/cloudbuild.builds.editor",
            }
            residual_roles = {
                "roles/logging.configWriter", "roles/monitoring.editor",
                "roles/serviceusage.serviceUsageConsumer",
            }
            role = str(after.get("role", ""))
            environment = (
                "platform" if role in platform_roles
                else next((name for name in ("staging", "production")
                           if str(address_index).startswith(f"{name}-")), "")
            )
            expected_index = f"{environment}-{role.replace('/', '-')}"
            expected_member = (
                f"serviceAccount:ticket-apply-{environment}@{project_id}."
                "iam.gserviceaccount.com"
            )
            if (role not in platform_roles | residual_roles) \
                    or address_index != expected_index \
                    or after.get("member") != expected_member:
                raise ControllerRejected("platform apply binding address/member is not exact")
        if resource_type == "google_storage_bucket_iam_member":
            bucket = after.get("bucket")
            if resource_name == "controller_verifier_source_reader":
                expected_member = (
                    f"serviceAccount:ticket-controller-verify@{project_id}."
                    "iam.gserviceaccount.com"
                )
                if bucket != f"{project_id}_cloudbuild" \
                        or after.get("member") != expected_member:
                    raise ControllerRejected(
                        "platform verifier source bucket binding is not exact"
                    )
            elif resource_name in {
                "plan_state_viewer", "plan_state_lock", "apply_state_admin",
            }:
                if address_index not in {"platform", "staging", "production"} \
                        or bucket != (
                            f"rag-kb-system-tfstate-{address_index}-900340137010"
                        ):
                    raise ControllerRejected("platform state bucket IAM target is not exact")
                pipeline = "apply" if resource_name == "apply_state_admin" else "plan"
                if after.get("member") != (
                    f"serviceAccount:ticket-{pipeline}-{address_index}@{project_id}."
                    "iam.gserviceaccount.com"
                ):
                    raise ControllerRejected("platform state bucket IAM member is not exact")
            elif resource_name in {
                "staging_apply_rag_bucket_iam", "staging_plan_rag_bucket_reader",
            }:
                pipeline = (
                    "apply" if resource_name == "staging_apply_rag_bucket_iam"
                    else "plan"
                )
                if address_index not in {"staging", "production"} \
                        or bucket != "rag-kb-system-kb-articles" \
                        or after.get("member") != (
                            f"serviceAccount:ticket-{pipeline}-{address_index}@{project_id}."
                            "iam.gserviceaccount.com"
                        ):
                    raise ControllerRejected("platform RAG bucket IAM target is not exact")
            elif bucket != "rag-kb-system-ticket-evidence-900340137010":
                raise ControllerRejected("platform evidence bucket IAM target is not exact")
        if resource_type == "google_cloud_tasks_queue_iam_member":
            environment = str(address_index)
            config = {
                "staging": {
                    "name": "ticket-jobs-staging", "producer": "ticket-producer-stg",
                    "reconciler": "ticket-reconciler-stg",
                },
                "production": {
                    "name": "ticket-jobs-prod", "producer": "ticket-producer-prod",
                    "reconciler": "ticket-reconciler-prod",
                },
            }.get(environment)
            if config is None:
                raise ControllerRejected("platform queue IAM target/member is not exact")
            if resource_name == "platform_apply_queue_task_inspector":
                principal = "ticket-apply-platform"
                expected_role = (
                    f"projects/{project_id}/roles/"
                    "ticketTfPlatformQueueTaskInspector"
                )
            else:
                principal = config[
                    "producer"
                    if resource_name == "runtime_producer_queue"
                    else "reconciler"
                ]
                expected_role = (
                    f"projects/{project_id}/roles/ticketQueueEnqueuer"
                    f"{'Staging' if environment == 'staging' else 'Production'}"
                )
            if after.get("name") != config["name"] \
                    or after.get("role") != expected_role \
                    or after.get("member") != (
                        f"serviceAccount:{principal}@{project_id}.iam.gserviceaccount.com"
                    ):
                raise ControllerRejected("platform queue IAM target/member is not exact")
        if resource_type in {
            "google_cloud_run_v2_service_iam_member",
            "google_cloud_run_v2_job_iam_member",
        } and resource_name != "environment_apply_developer":
            run_policy = {
                ("task_signer_invokes_worker", "staging"): (
                    "kb-rag-ticket-worker-staging", "ticket-task-signer-stg",
                ),
                ("task_signer_invokes_worker", "production"): (
                    "kb-rag-ticket-worker", "ticket-task-signer-prod",
                ),
                ("scheduler_runs_reconciler", "staging"): (
                    "ticket-reconciler-staging", "ticket-scheduler-stg",
                ),
                ("scheduler_runs_reconciler", "production"): (
                    "ticket-reconciler-prod", "ticket-scheduler-prod",
                ),
                ("n8n_invokes_producer", "staging"): (
                    "kb-rag-system-staging", "kb-rag-client",
                ),
                ("n8n_invokes_producer", "production"): (
                    "kb-rag-system", "kb-rag-client",
                ),
                ("e2e_invokes_staging_producer", "staging"): (
                    "kb-rag-system-staging", "ticket-e2e-stg",
                ),
                ("production_preserved_invoker", "production"): (
                    "kb-rag-system", "kb-rag-client",
                ),
            }
            environment = (
                str(address_index) if address_index in {"staging", "production"}
                else "staging" if resource_name == "e2e_invokes_staging_producer"
                else "production" if resource_name == "production_preserved_invoker"
                else ""
            )
            expected_run = run_policy.get((resource_name, environment))
            if expected_run is None \
                    or after.get("name") != expected_run[0] \
                    or after.get("member") != (
                        f"serviceAccount:{expected_run[1]}@{project_id}."
                        "iam.gserviceaccount.com"
                    ):
                raise ControllerRejected("platform Run IAM target/member is not exact")
        if resource_type == "google_project_iam_member" \
                and resource_name == "environment_apply_secret_admin":
            if address_index is None or "-" not in address_index:
                raise ControllerRejected("platform secret IAM address is invalid")
            environment, secret_id = address_index.split("-", 1)
            if secret_id not in (secret_inventory or {}).get(environment, []) \
                    or after.get("member") != (
                        f"serviceAccount:ticket-apply-{environment}@{project_id}."
                        "iam.gserviceaccount.com"
                    ) \
                    or secret_id not in "\n".join(_json_strings(after.get("condition", {}))):
                raise ControllerRejected("platform secret IAM target/member is not exact")
        if resource_type == "google_project_iam_member" \
                and resource_name in {
                    "runtime_firestore", "runtime_vertex", "runtime_telemetry",
                }:
            runtime_members = {
                "staging-producer": "ticket-producer-stg",
                "staging-worker": "ticket-worker-stg",
                "staging-reconciler": "ticket-reconciler-stg",
                "production-worker": "ticket-worker-prod",
                "production-reconciler": "ticket-reconciler-prod",
                "production-producer": "ticket-producer-prod",
                "production-producer-logging": "ticket-producer-prod",
                "production-producer-monitoring": "ticket-producer-prod",
            }
            account = runtime_members.get(str(address_index))
            allowed_keys = (
                {
                    "staging-producer", "staging-worker", "staging-reconciler",
                    "production-producer", "production-worker",
                    "production-reconciler",
                }
                if resource_name == "runtime_firestore"
                else ({
                    "staging-producer", "staging-worker", "production-worker",
                    "production-producer",
                } if resource_name == "runtime_vertex" else {
                    "production-producer-logging",
                    "production-producer-monitoring",
                })
            )
            if address_index not in allowed_keys or after.get("member") != (
                f"serviceAccount:{account}@{project_id}.iam.gserviceaccount.com"
            ):
                raise ControllerRejected("platform runtime IAM address/member is not exact")
            if resource_name == "runtime_telemetry":
                expected_role = {
                    "production-producer-logging": "roles/logging.logWriter",
                    "production-producer-monitoring": "roles/monitoring.metricWriter",
                }.get(str(address_index))
                if after.get("role") != expected_role:
                    raise ControllerRejected(
                        "platform runtime telemetry role is not exact"
                    )
            if resource_name == "runtime_firestore":
                database = (
                    "ticket-staging" if str(address_index).startswith("staging-")
                    else "(default)"
                )
                if database not in "\n".join(_json_strings(after.get("condition", {}))):
                    raise ControllerRejected("platform runtime Firestore scope is not exact")
        if resource_type == "google_project_iam_member" \
                and resource_name in {
                    "platform_apply_storage", "platform_apply_iam_broker",
                    "platform_apply_firestore_broker", "platform_apply_secret_broker",
                    "platform_apply_queue_broker", "platform_apply_scheduler_broker",
                    "platform_apply_run_iam_broker",
                } and after.get("member") != (
                    f"serviceAccount:ticket-apply-platform@{project_id}."
                    "iam.gserviceaccount.com"
                ):
            raise ControllerRejected("platform broker member is not exact")
        if resource_type == "google_project_iam_member" \
                and resource_name in {
                    "environment_apply_index_admin", "environment_apply_metadata_reader",
                }:
            environment = str(address_index)
            if environment not in {"staging", "production"} \
                    or after.get("member") != (
                        f"serviceAccount:ticket-apply-{environment}@{project_id}."
                        "iam.gserviceaccount.com"
                    ):
                raise ControllerRejected("environment pipeline member is not exact")
            if resource_name == "environment_apply_index_admin":
                database = "ticket-staging" if environment == "staging" else "(default)"
                expected_condition = {
                    "title": f"{environment}_database_schema_only",
                    "description": (
                        f"Índices/TTL exclusivamente en la database {database}."
                    ),
                    "expression": (
                        f'resource.name.startsWith("projects/{project_id}/databases/'
                        f'{database}/") || resource.name == '
                        f'"projects/{project_id}/databases/{database}"'
                    ),
                }
                condition = after.get("condition")
                if condition != [expected_condition]:
                    raise ControllerRejected(
                        "environment index condition is not exact"
                    )
        if resource_type == "google_project_iam_member" \
                and resource_name == "gate_receipt_approver":
            if not isinstance(address_index, str) \
                    or after.get("member") != f"user:{address_index}":
                raise ControllerRejected("gate receipt approver address/member is not exact")
        if resource_type == "google_project_iam_member" \
                and resource_name == "staging_observer_run_reader" \
                and (address_index != "0" or after.get("member") != (
                    f"serviceAccount:ticket-staging-observer@{project_id}."
                    "iam.gserviceaccount.com"
                )):
            raise ControllerRejected("staging observer member is not exact")
        if resource_type == "google_service_account_iam_member" \
                and resource_name == "environment_apply_actas":
            allowed_accounts = {
                "staging": {
                    "ticket-producer-stg", "ticket-worker-stg",
                    "ticket-reconciler-stg", "ticket-e2e-stg",
                },
                "production": {
                    "ticket-producer-prod", "ticket-worker-prod",
                    "ticket-reconciler-prod",
                },
            }
            environment, separator, account_id = str(address_index).partition("-")
            expected_index = f"{environment}-{account_id}"
            expected_target = (
                f"projects/{project_id}/serviceAccounts/{account_id}@{project_id}."
                "iam.gserviceaccount.com"
            )
            expected_member = (
                f"serviceAccount:ticket-apply-{environment}@{project_id}."
                "iam.gserviceaccount.com"
            )
            if not separator or address_index != expected_index \
                    or account_id not in allowed_accounts.get(environment, set()) \
                    or after.get("service_account_id") != expected_target \
                    or after.get("member") != expected_member:
                raise ControllerRejected("environment apply actAs target/member is not exact")
        if resource_type == "google_service_account_iam_member" \
                and resource_name == "platform_apply_actas_scheduler":
            expected_account_id = PLATFORM_APPLY_ACTAS_SERVICE_ACCOUNT_IDS.get(
                str(address_index)
            )
            expected_target = (
                f"projects/{project_id}/serviceAccounts/{expected_account_id}@"
                f"{project_id}.iam.gserviceaccount.com"
            )
            expected_member = (
                f"serviceAccount:ticket-apply-platform@{project_id}."
                "iam.gserviceaccount.com"
            )
            if expected_account_id is None \
                    or after.get("service_account_id") != expected_target \
                    or after.get("role") != "roles/iam.serviceAccountUser" \
                    or after.get("member") != expected_member:
                raise ControllerRejected(
                    "platform apply actAs target/member is not exact"
                )
        if resource_type == "google_service_account_iam_member" \
                and resource_name in {
                    "runtime_producer_actas_signer",
                    "runtime_reconciler_actas_signer",
                    "tasks_agent_signs_as_runtime_signer",
                }:
            environment = str(address_index)
            suffix = "stg" if environment == "staging" else "prod"
            if environment not in {"staging", "production"}:
                raise ControllerRejected("platform signer IAM address is invalid")
            target_email = f"ticket-task-signer-{suffix}@{project_id}.iam.gserviceaccount.com"
            if resource_name == "runtime_producer_actas_signer":
                principal = "ticket-producer-stg" if environment == "staging" \
                    else "ticket-producer-prod"
                expected_member = (
                    f"serviceAccount:{principal}@{project_id}.iam.gserviceaccount.com"
                )
            elif resource_name == "runtime_reconciler_actas_signer":
                expected_member = (
                    f"serviceAccount:ticket-reconciler-{suffix}@{project_id}."
                    "iam.gserviceaccount.com"
                )
            else:
                expected_member = (
                    "serviceAccount:service-900340137010@"
                    "gcp-sa-cloudtasks.iam.gserviceaccount.com"
                )
                if after.get("member") != expected_member:
                    raise ControllerRejected("Cloud Tasks service agent is not exact")
            if not str(after.get("service_account_id", "")).endswith(
                f"/serviceAccounts/{target_email}"
            ) or after.get("member") != expected_member:
                raise ControllerRejected("platform signer IAM target/member is not exact")
    expected_creators = {
        environment for environment, phase in phases.items() if phase == "bootstrap"
    }
    if creator_environments != expected_creators:
        raise ControllerRejected("temporary Run creator does not match bootstrap phases")
    expected_developers = set().union(*normalized_resources.values())
    if developer_inventory != expected_developers:
        raise ControllerRejected("direct Run developer inventory differs from attested handoff")
    for environment, phase in container_phases.items():
        expected_runtime_iam = (
            PLATFORM_MANAGED_RUNTIME_IAM_INVENTORY[environment]
            if phase == "managed" else frozenset()
        )
        if observed_runtime_iam[environment] != expected_runtime_iam:
            raise ControllerRejected(
                f"platform {environment} runtime IAM inventory is not exact"
            )
    expected_secret_ids = set()
    if secret_inventory is not None:
        if not isinstance(secret_inventory, Mapping) \
                or set(secret_inventory) != {"staging", "production"}:
            raise ControllerRejected("platform secret inventory is invalid")
        for value in secret_inventory.values():
            if not isinstance(value, list):
                raise ControllerRejected("platform secret inventory is invalid")
            expected_secret_ids.update(value)
    if observed_secret_ids != expected_secret_ids:
        raise ControllerRejected("platform secret containers differ from approved inventory")


def _state_metadata(raw: str) -> tuple[str, int]:
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ControllerRejected("Terraform state is not JSON") from exc
    lineage, serial = state.get("lineage"), state.get("serial")
    if not isinstance(lineage, str) or not lineage:
        raise ControllerRejected("Terraform state lineage missing")
    if not isinstance(serial, int) or serial < 0:
        raise ControllerRejected("Terraform state serial invalid")
    return lineage, serial


def _validate_backend(root: Path, environment: str) -> str:
    expected = f"rag-kb-system-tfstate-{environment}-900340137010"
    backend_file = root / "backend.tf"
    if not backend_file.is_file():
        raise ControllerRejected(f"backend declaration missing for {environment}")
    text = backend_file.read_text(encoding="utf-8")
    buckets = re.findall(r'\bbucket\s*=\s*"([^"]+)"', text)
    if buckets != [expected] or not re.search(r'backend\s+"gcs"', text):
        raise ControllerRejected(
            f"backend bucket for {environment} must be exactly {expected}"
        )
    prefixes = re.findall(r'\bprefix\s*=\s*"([^"]+)"', text)
    if prefixes != ["state"]:
        raise ControllerRejected(
            f"backend prefix for {environment} must be exactly state"
        )
    if re.search(
        r"\b(?:credentials|access_token|impersonate_service_account|encryption_key)\s*=",
        text,
    ):
        raise ControllerRejected("backend credential or impersonation override rejected")
    return expected


def _deterministic_bundle(source: Path, destination: Path) -> bytes:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in _safe_relative_files(source):
            relative = path.relative_to(source)
            if ".terraform" in relative.parts or relative.name.endswith(".tfplan"):
                continue
            info = zipfile.ZipInfo(str(relative), date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    return destination.read_bytes()


class Toolchain:
    """Process boundary, injectable for offline functional verification."""

    def run(self, argv: Sequence[str], *, cwd: Optional[Path] = None,
            capture: bool = False, capture_stderr: bool = False) -> str:
        completed = subprocess.run(  # noqa: S603 - argv list, no shell expansion
            [str(arg) for arg in argv], cwd=cwd, check=True,
            text=True, capture_output=capture,
        )
        if not capture:
            return ""
        return completed.stdout + (completed.stderr if capture_stderr else "")

    def describe_image(self, image_digest: str) -> Mapping[str, Any]:
        raw = self.run(["image", "describe", image_digest], capture=True)
        return json.loads(raw)

    def verify_scan(self, image_digest: str) -> Mapping[str, Any]:
        raw = self.run(["scan-image", "verify", image_digest], capture=True)
        return json.loads(raw)

    def sbom(self, image_digest: str) -> Mapping[str, Any]:
        raw = self.run(
            ["syft", "scan", "--output", "spdx-json", image_digest],
            capture=True,
        )
        return json.loads(raw)

    def backend_state_generation(self, bucket: str) -> Optional[str]:
        object_uri = f"gs://{bucket}/state/default.tfstate"
        raw = self.run([
            "gcloud", "storage", "ls", f"gs://{bucket}/state/", "--json",
            f"--project={TRUSTED_PROJECT_ID}",
        ], capture=True)
        try:
            objects = json.loads(raw or "[]")
        except json.JSONDecodeError as exc:
            raise ControllerRejected("backend state listing is not JSON") from exc
        if not isinstance(objects, list):
            raise ControllerRejected("backend state listing has invalid shape")
        matches = [item for item in objects if isinstance(item, dict) and (
            item.get("url") == object_uri or item.get("name") == "state/default.tfstate"
            or item.get("name") == object_uri
        )]
        if not matches:
            return None
        if len(matches) != 1:
            raise ControllerRejected("backend contains ambiguous default state objects")
        generation = matches[0].get("generation")
        if not isinstance(generation, (str, int)) or not str(generation).isdigit() \
                or int(generation) < 1:
            raise ControllerRejected("backend state generation invalid")
        return str(generation)

    def verify_provenance(
        self, image_digest: str, candidate_sha: str, build_id: str = "",
    ) -> Mapping[str, Any]:
        raw = self.run(
            ["provenance", "verify", image_digest, candidate_sha, build_id], capture=True,
        )
        return json.loads(raw)

    def describe_build(self, build_id: str) -> Mapping[str, Any]:
        raw = self.run(["build", "describe", build_id], capture=True)
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ControllerRejected("Cloud Build receipt has invalid shape")
        return payload

    def describe_trigger(self, trigger_id: str) -> Mapping[str, Any]:
        raw = self.run(["trigger", "describe", trigger_id], capture=True)
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ControllerRejected("Cloud Build trigger has invalid shape")
        return payload

    def observe_staging(self) -> Mapping[str, Any]:
        raw = self.run(["staging", "observe"], capture=True)
        return json.loads(raw)

    def pause_and_verify_empty_queue(self, queue_name: str) -> None:
        self.run(["queue", "pause-and-verify-empty", queue_name])

    def pause_existing_queue_and_verify_empty(self, queue_name: str) -> None:
        self.run(["queue", "pause-existing-and-verify-empty", queue_name])


class ProductionToolchain(Toolchain):
    """Real pinned-image toolchain used inside Dockerfile.release-controller."""

    def describe_image(self, image_digest: str) -> Mapping[str, Any]:
        raw = self.run([
            "gcloud", "artifacts", "docker", "images", "describe",
            image_digest, "--show-provenance", "--format=json",
            f"--project={TRUSTED_PROJECT_ID}", "--location=us-central1",
        ], capture=True)
        payload = json.loads(raw)
        digest = payload.get("image_summary", {}).get("digest") or payload.get("digest")
        return {"digest": digest}

    def describe_build(self, build_id: str) -> Mapping[str, Any]:
        raw = self.run([
            "gcloud", "builds", "describe", build_id, "--format=json",
            f"--project={TRUSTED_PROJECT_ID}", "--region=global",
        ], capture=True)
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ControllerRejected("Cloud Build receipt has invalid shape")
        return payload

    def describe_trigger(self, trigger_id: str) -> Mapping[str, Any]:
        raw = self.run([
            "gcloud", "builds", "triggers", "describe", trigger_id,
            "--region=global", "--format=json", f"--project={TRUSTED_PROJECT_ID}",
        ], capture=True)
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ControllerRejected("Cloud Build trigger has invalid shape")
        return payload

    def pause_and_verify_empty_queue(self, queue_name: str) -> None:
        project_id = os.environ.get("PROJECT_ID", "")
        if project_id != TRUSTED_PROJECT_ID:
            raise ControllerRejected("queue operation project is not rag-kb-system")
        queue_path = f"projects/{project_id}/locations/us-central1/queues/{queue_name}"
        self.run([
            "gcloud", "tasks", "queues", "pause", queue_path,
            f"--project={project_id}", "--location=us-central1", "--quiet",
        ])
        queue = json.loads(self.run([
            "gcloud", "tasks", "queues", "describe", queue_path,
            f"--project={project_id}", "--location=us-central1", "--format=json",
        ], capture=True))
        tasks = json.loads(self.run([
            "gcloud", "tasks", "list", f"--queue={queue_name}",
            f"--project={project_id}", "--location=us-central1", "--format=json",
        ], capture=True))
        if queue.get("state") != "PAUSED" or tasks != []:
            raise ControllerRejected("dark Cloud Tasks queue is not PAUSED and empty")

    def pause_existing_queue_and_verify_empty(self, queue_name: str) -> None:
        project_id = os.environ.get("PROJECT_ID", "")
        if project_id != TRUSTED_PROJECT_ID:
            raise ControllerRejected("queue operation project is not rag-kb-system")
        queue_path = f"projects/{project_id}/locations/us-central1/queues/{queue_name}"
        try:
            queue = json.loads(self.run([
                "gcloud", "tasks", "queues", "describe", queue_path,
                f"--project={project_id}", "--location=us-central1", "--format=json",
            ], capture=True))
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr or ""
            if exc.returncode == 1 and "NOT_FOUND" in stderr \
                    and queue_name in stderr:
                return
            raise
        if not isinstance(queue, Mapping) or queue.get("name") != queue_path:
            raise ControllerRejected("queue existence preflight returned ambiguous scope")
        self.pause_and_verify_empty_queue(queue_name)

    def verify_scan(self, image_digest: str) -> Mapping[str, Any]:
        scan_name = self.run([
            "gcloud", "artifacts", "docker", "images", "scan", image_digest,
            "--remote", "--location=us", "--format=value(response.scan)",
            f"--project={TRUSTED_PROJECT_ID}",
        ], capture=True).strip()
        if re.fullmatch(
            rf"projects/{re.escape(TRUSTED_PROJECT_ID)}/locations/us/scans/"
            r"[A-Za-z0-9._~-]{1,256}",
            scan_name,
        ) is None:
            raise ControllerRejected("On-Demand Scan returned an invalid scan identifier")
        raw = self.run([
            "gcloud", "artifacts", "docker", "images",
            "list-vulnerabilities", scan_name, "--location=us", "--format=json",
            f"--project={TRUSTED_PROJECT_ID}",
        ], capture=True)
        payload = json.loads(raw)
        vulnerabilities = payload if isinstance(payload, list) else payload.get("occurrences", [])
        critical = 0
        high = 0
        high_ids: list[str] = []
        for item in vulnerabilities:
            severity = str(item.get("effectiveSeverity") or item.get("severity") or "").upper()
            critical += severity == "CRITICAL"
            high += severity == "HIGH"
            if severity == "HIGH":
                candidates = (
                    item.get("vulnerabilityId"), item.get("id"), item.get("noteName"),
                )
                identifiers = [
                    match.group(0) for value in candidates if isinstance(value, str)
                    for match in [re.search(r"CVE-[0-9]{4}-[0-9]{4,}", value)]
                    if match is not None
                ]
                if len(set(identifiers)) != 1:
                    raise ControllerRejected("HIGH vulnerability has no unique CVE identifier")
                high_ids.append(identifiers[0])
        status = "passed" if critical == 0 and high == 0 else "rejected"
        return {"status": status, "critical": critical, "high": high,
                "high_ids": sorted(set(high_ids)),
                "scan_report_sha256": _sha256(_canonical_json(payload)),
                "scan_name": scan_name, "vulnerabilities": vulnerabilities}

    def verify_provenance(
        self, image_digest: str, candidate_sha: str, build_id: str = "",
    ) -> Mapping[str, Any]:
        build_id = build_id or os.environ.get("BUILD_ID", "")
        if not build_id:
            raise ControllerRejected("Cloud Build ID missing for provenance")
        raw = self.run([
            "gcloud", "builds", "describe", build_id, "--format=json",
            f"--project={TRUSTED_PROJECT_ID}", "--region=global",
        ], capture=True)
        payload = json.loads(raw)
        source_provenance = payload.get("sourceProvenance", {})
        if not isinstance(source_provenance, dict):
            raise ControllerRejected("authenticated build source provenance is missing")
        resolved_repo = source_provenance.get("resolvedRepoSource", {})
        resolved_git = source_provenance.get("resolvedGitSource", {})
        resolved_source_shas = {
            value for value in (
                resolved_repo.get("commitSha") if isinstance(resolved_repo, dict) else None,
                resolved_git.get("commitSha") if isinstance(resolved_git, dict) else None,
                resolved_git.get("revision") if isinstance(resolved_git, dict) else None,
            ) if isinstance(value, str) and SHA_RE.fullmatch(value)
        }
        if resolved_source_shas != {candidate_sha}:
            raise ControllerRejected(
                "authenticated Cloud Build resolved source does not bind SHA"
            )
        if payload.get("id") != build_id \
                or payload.get("name") != (
                    f"projects/{TRUSTED_PROJECT_ID}/locations/global/builds/{build_id}"
                ) \
                or payload.get("projectId") != TRUSTED_PROJECT_ID \
                or payload.get("status") != "SUCCESS":
            raise ControllerRejected(
                "authenticated Cloud Build provenance requires a finalized SUCCESS build"
            )
        image_raw = self.run([
            "gcloud", "artifacts", "docker", "images", "describe",
            image_digest, "--show-provenance", "--format=json",
            f"--project={TRUSTED_PROJECT_ID}", "--location=us-central1",
        ], capture=True)
        try:
            image_provenance = json.loads(image_raw)
        except json.JSONDecodeError as exc:
            raise ControllerRejected("finalized image provenance is not JSON") from exc
        provenance_strings = set(_json_strings(image_provenance))
        digest = image_digest.rsplit("@", 1)[-1]
        if build_id not in provenance_strings \
                or candidate_sha not in provenance_strings \
                or digest not in provenance_strings \
                or not any("cloudbuild" in value.lower() for value in provenance_strings):
            raise ControllerRejected(
                "finalized image attestation does not bind builder/source/subject"
            )
        return {
            "provenance_verified": True,
            "source_commit": candidate_sha,
            "subject_digest": image_digest,
            "build_id": build_id,
        }

    @staticmethod
    def _run_observation(
        payload: Mapping[str, Any], *, name: str, is_job: bool,
    ) -> dict[str, Any]:
        metadata = payload.get("metadata", {})
        status = payload.get("status", {})
        if not isinstance(metadata, dict) or not isinstance(status, dict) \
                or metadata.get("name") != name:
            raise ControllerRejected("Cloud Run observation resource identity differs")
        images = set(_json_values_for_key(payload.get("spec", {}), "image"))
        if len(images) != 1:
            raise ControllerRejected("Cloud Run observation image is ambiguous")
        conditions = list(_json_values_for_key(status, "conditions"))
        condition_strings = set(_json_strings(conditions))
        ready = "Ready" in condition_strings and bool(
            {"True", "CONDITION_SUCCEEDED", "CONDITION_STATUS_TRUE"}
            & condition_strings
        )
        env_names = list(_json_values_for_key(payload.get("spec", {}), "name"))
        env_values = list(_json_values_for_key(payload.get("spec", {}), "value"))
        handler_mode = ""
        for node in _walk_json(payload.get("spec", {})):
            if isinstance(node, dict) and node.get("name") == "TICKET_HANDLER_MODE":
                value = node.get("value")
                if isinstance(value, str):
                    handler_mode = value
        del env_names, env_values
        if is_job:
            generation = metadata.get("generation")
            if not isinstance(generation, (int, str)) or not str(generation).isdigit():
                raise ControllerRejected("Cloud Run Job generation is missing")
            revision = f"generation-{generation}"
            traffic: list[dict[str, Any]] = []
        else:
            revision = status.get("latestReadyRevisionName")
            if not isinstance(revision, str) or not revision:
                raise ControllerRejected("Cloud Run latest ready revision is missing")
            traffic = []
            for item in status.get("traffic", []):
                if not isinstance(item, dict):
                    raise ControllerRejected("Cloud Run traffic observation is invalid")
                traffic.append({
                    "tag": item.get("tag", ""),
                    "revision": item.get("revisionName", ""),
                    "percent": item.get("percent", 0),
                })
        return {
            "name": name, "revision": revision, "image_digest": next(iter(images)),
            "ready": ready, "handler_mode": handler_mode, "traffic": traffic,
        }

    def observe_staging(self) -> Mapping[str, Any]:
        project = os.environ.get("PROJECT_ID", "")
        if project != TRUSTED_PROJECT_ID:
            raise ControllerRejected("PROJECT_ID is not the exact trusted project")
        resources = {
            "producer": ("services", "kb-rag-system-staging", False),
            "worker": ("services", "kb-rag-ticket-worker-staging", False),
            "reconciler": ("jobs", "ticket-reconciler-staging", True),
        }
        observed: dict[str, Any] = {}
        for role, (kind, name, is_job) in resources.items():
            raw = self.run([
                "gcloud", "run", kind, "describe", name,
                f"--project={project}", "--region=us-central1", "--format=json",
            ], capture=True)
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ControllerRejected("Cloud Run observation is not JSON") from exc
            if not isinstance(payload, dict):
                raise ControllerRejected("Cloud Run observation has invalid shape")
            observed[role] = self._run_observation(payload, name=name, is_job=is_job)
        return observed


class CandidateSource:
    def checkout(self, sha: str, destination: Path) -> None:
        raise NotImplementedError

    def changed_files(self, sha: str, base_sha: str) -> Sequence[str]:
        raise NotImplementedError


class LocalCandidateSource(CandidateSource):
    """Offline adapter used by functional tests; never selected by the CLI."""

    def __init__(self, root: Path, *, changed: Optional[Mapping[str, Sequence[str]]] = None):
        self.root = root
        self.changed = dict(changed or {})

    def checkout(self, sha: str, destination: Path) -> None:
        _require_sha(sha)
        shutil.copytree(self.root, destination)

    def changed_files(self, sha: str, base_sha: str) -> Sequence[str]:
        _require_sha(sha)
        _require_sha(base_sha)
        return tuple(self.changed.get(sha, ()))


class GitCandidateSource(CandidateSource):
    def __init__(self, repository: str, tools: Toolchain):
        if repository != TRUSTED_REPOSITORY:
            raise ControllerRejected("candidate source is not the trusted repository")
        self.repository = repository
        self.tools = tools

    def checkout(self, sha: str, destination: Path) -> None:
        _require_sha(sha)
        destination.mkdir(parents=True)
        self.tools.run(["git", "init", "--quiet"], cwd=destination)
        self.tools.run(["git", "remote", "add", "origin", self.repository], cwd=destination)
        self.tools.run(["git", "fetch", "--depth=1", "origin", sha], cwd=destination)
        self.tools.run(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=destination)
        observed = self.tools.run(["git", "rev-parse", "HEAD"], cwd=destination,
                                  capture=True).strip()
        if observed != sha:
            raise ControllerRejected("checkout SHA differs from requested candidate")

    def changed_files(self, sha: str, base_sha: str) -> Sequence[str]:
        _require_sha(sha)
        _require_sha(base_sha)
        with tempfile.TemporaryDirectory(prefix="release-controller-diff-") as raw:
            root = Path(raw)
            self.tools.run(["git", "init", "--quiet"], cwd=root)
            self.tools.run(["git", "remote", "add", "origin", self.repository], cwd=root)
            self.tools.run(["git", "fetch", "--depth=1", "origin", sha, base_sha], cwd=root)
            output = self.tools.run(
                ["git", "diff", "--name-only", base_sha, sha], cwd=root,
                capture=True,
            )
        return tuple(line for line in output.splitlines() if line)


class ArtifactStore:
    def read(self, generation_uri: str) -> bytes:
        raise NotImplementedError

    def write(self, object_uri: str, data: bytes) -> str:
        raise NotImplementedError

    def resolve(self, object_uri: str) -> str:
        raise NotImplementedError


class MemoryArtifactStore(ArtifactStore):
    def __init__(self):
        self._objects: dict[str, list[bytes]] = {}

    @staticmethod
    def _object_uri(value: str) -> str:
        return value if value.startswith("gs://") else f"gs://memory/{value.lstrip('/')}"

    def write(self, object_uri: str, data: bytes) -> str:
        key = self._object_uri(object_uri).split("#", 1)[0]
        versions = self._objects.setdefault(key, [])
        versions.append(bytes(data))
        return f"{key}#{len(versions)}"

    def read(self, generation_uri: str) -> bytes:
        _require_generation_uri(generation_uri)
        key, generation = generation_uri.rsplit("#", 1)
        try:
            return self._objects[key][int(generation) - 1]
        except (KeyError, IndexError) as exc:
            raise ControllerRejected(f"artifact not found: {generation_uri}") from exc

    def resolve(self, object_uri: str) -> str:
        key = self._object_uri(object_uri).split("#", 1)[0]
        versions = self._objects.get(key)
        if not versions:
            raise ControllerRejected(f"artifact not found: {key}")
        return f"{key}#{len(versions)}"

    def replace_for_test(self, generation_uri: str, data: bytes) -> None:
        key, generation = generation_uri.rsplit("#", 1)
        self._objects[key][int(generation) - 1] = bytes(data)


class GCSArtifactStore(ArtifactStore):
    def __init__(self, tools: Toolchain):
        self.tools = tools

    def read(self, generation_uri: str) -> bytes:
        _require_generation_uri(generation_uri)
        with tempfile.TemporaryDirectory(prefix="release-controller-object-") as raw:
            path = Path(raw) / "object"
            self.tools.run([
                "gcloud", "storage", "cp", generation_uri, str(path),
                f"--project={TRUSTED_PROJECT_ID}",
            ])
            return path.read_bytes()

    def write(self, object_uri: str, data: bytes) -> str:
        if "#" in object_uri or not object_uri.startswith("gs://"):
            raise ControllerRejected("write destination must be an unversioned gs:// URI")
        with tempfile.TemporaryDirectory(prefix="release-controller-upload-") as raw:
            path = Path(raw) / "object"
            path.write_bytes(data)
            output = self.tools.run([
                "gcloud", "storage", "cp", "--if-generation-match=0",
                "--print-created-message", str(path), object_uri,
                f"--project={TRUSTED_PROJECT_ID}",
            ], capture=True, capture_stderr=True)
        created = re.findall(r"gs://[^\s#]+#[1-9][0-9]*", output)
        expected = [uri for uri in created if uri.rsplit("#", 1)[0] == object_uri]
        if len(expected) != 1:
            raise ControllerRejected(
                "write-once upload did not return exactly one immutable generation"
            )
        return _require_generation_uri(expected[0], "created artifact URI")

    def resolve(self, object_uri: str) -> str:
        if "#" in object_uri or not object_uri.startswith("gs://"):
            raise ControllerRejected("resolve requires an unversioned gs:// URI")
        generation = self.tools.run([
            "gcloud", "storage", "objects", "describe", object_uri,
            "--format=value(generation)", f"--project={TRUSTED_PROJECT_ID}",
        ], capture=True).strip()
        if not generation.isdigit() or int(generation) < 1:
            raise ControllerRejected("GCS object has no immutable generation")
        return f"{object_uri}#{generation}"


class ReleaseController:
    def __init__(self, *, source: CandidateSource, artifacts: ArtifactStore,
                 tools: Toolchain, work_root: Path, evidence_bucket: str,
                 e2e_image: str = "us-central1-docker.pkg.dev/test/release/e2e",
                 project_id: str = "rag-kb-system", controller_digest: str = "",
                 runtime_image: str = "us-central1-docker.pkg.dev/test/release/runtime",
                 trusted_root: Path = Path("/opt/release-controller/trusted-context")):
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]+", evidence_bucket):
            raise ControllerRejected("invalid evidence bucket")
        self.source = source
        self.artifacts = artifacts
        self.tools = tools
        self.work_root = work_root.resolve()
        self.evidence_bucket = evidence_bucket
        self.e2e_image = e2e_image
        self.runtime_image = runtime_image
        self.trusted_root = trusted_root.resolve()
        if project_id != TRUSTED_PROJECT_ID:
            raise ControllerRejected("release controller requires the exact project ID")
        self.project_id = project_id
        self.controller_digest = controller_digest
        self.work_root.mkdir(parents=True, exist_ok=True)

    def _checkout(self, sha: str) -> Path:
        _require_sha(sha, "candidate SHA")
        destination = self.work_root / f"candidate-{uuid.uuid4().hex}"
        self.source.checkout(sha, destination)
        validate_terraform_tree(destination)
        trusted_lock = self.trusted_root / "reviewed.terraform.lock.hcl"
        if not trusted_lock.is_file():
            raise ControllerRejected("reviewed Terraform provider lock is missing")
        trusted_bytes = trusted_lock.read_bytes()
        for environment in ENVIRONMENTS:
            candidate_lock = (
                destination / "infra" / "terraform" / "live" / environment /
                ".terraform.lock.hcl"
            )
            if candidate_lock.read_bytes() != trusted_bytes:
                raise ControllerRejected(
                    f"{environment} provider lock differs from reviewed controller input"
                )
        module_lock = (
            destination / "infra" / "terraform" / "modules" /
            "ticket_environment" / ".terraform.lock.hcl"
        )
        without_platform_hashes = lambda value: re.sub(  # noqa: E731
            rb'^\s*"h1:[^\n]+\n', b"", value, flags=re.MULTILINE,
        )
        if not module_lock.is_file() or without_platform_hashes(
            module_lock.read_bytes()
        ) != without_platform_hashes(trusted_bytes):
            raise ControllerRejected(
                "ticket_environment provider lock differs from reviewed controller input"
            )
        return destination

    def _write(self, path: str, data: bytes) -> str:
        return self.artifacts.write(
            f"gs://{self.evidence_bucket}/{path.lstrip('/')}", data,
        )

    def execute(self, argv: Sequence[str]) -> dict[str, Any]:
        args = build_parser().parse_args(list(argv))
        handler = getattr(self, f"_{args.command.replace('-', '_')}")
        return handler(args)

    def _platform_tfvars(
        self, environment: str, uri: str,
    ) -> tuple[bytes, str, str]:
        """Load the immutable platform handoff without reading platform state."""
        if environment not in RUNTIME_SERVICE_ACCOUNTS:
            raise ControllerRejected("platform outputs are only valid for environments")
        if not uri:
            raise ControllerRejected(
                f"platform outputs URI is required for {environment}"
            )
        _require_generation_uri(uri, "platform outputs URI")
        if not uri.startswith(f"gs://{self.evidence_bucket}/platform-outputs/"):
            raise ControllerRejected("platform outputs must be in the evidence bucket")
        raw = self.artifacts.read(uri)
        try:
            manifest = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ControllerRejected("platform outputs are not JSON") from exc
        if not isinstance(manifest, dict):
            raise ControllerRejected("platform outputs must be a JSON object")
        try:
            _verify_manifest(manifest, "manifest_hash")
        except ControllerRejected as exc:
            raise ControllerRejected(f"platform outputs: {exc}") from exc
        platform_fields = {
            "artifact_type", "status", "project_id", "platform_candidate_sha",
            "platform_state_lineage", "platform_state_serial",
            "terraform_outputs_hash", "outputs", "manifest_hash",
        }
        if set(manifest) != platform_fields \
                or manifest.get("artifact_type") != "platform_outputs" \
                or manifest.get("status") != "passed" \
                or manifest.get("project_id") != self.project_id:
            raise ControllerRejected(
                "platform outputs do not bind passed artifact/project"
            )
        _require_sha(
            str(manifest.get("platform_candidate_sha", "")),
            "platform candidate SHA",
        )
        if not isinstance(manifest.get("platform_state_lineage"), str) \
                or not manifest["platform_state_lineage"]:
            raise ControllerRejected("platform outputs state lineage missing")
        serial = manifest.get("platform_state_serial")
        if not isinstance(serial, int) or serial < 0:
            raise ControllerRejected("platform outputs state serial invalid")
        _require_hash(
            str(manifest.get("terraform_outputs_hash", "")),
            "platform Terraform outputs hash",
        )
        outputs = manifest.get("outputs")
        if not isinstance(outputs, dict) or set(outputs) != {
            "runtime_service_accounts", "evidence_bucket",
            "firestore_scope_phase", "firestore_scope_enforced",
            "pipeline_service_accounts", "environment_handoff_phase",
            "environment_run_resources", "environment_secret_ids",
            "environment_container_phase",
            "environment_release_phase",
        } \
                or outputs.get("evidence_bucket") != self.evidence_bucket \
                or outputs.get("firestore_scope_phase") != "enforce" \
                or outputs.get("firestore_scope_enforced") is not True:
            raise ControllerRejected(
                "platform outputs require the bound evidence bucket and enforced Firestore scope"
            )
        phases = outputs.get("environment_handoff_phase")
        inventories = outputs.get("environment_run_resources")
        secret_inventories = outputs.get("environment_secret_ids")
        container_phases = outputs.get("environment_container_phase")
        allowed_inventories = {
            "staging": {
                "services/kb-rag-system-staging",
                "services/kb-rag-ticket-worker-staging",
                "jobs/ticket-reconciler-staging", "jobs/ticket-e2e-staging",
            },
            "production": {
                "services/kb-rag-system", "services/kb-rag-ticket-worker",
                "jobs/ticket-reconciler-prod",
            },
        }
        if not isinstance(phases, dict) or set(phases) != set(allowed_inventories) \
                or not isinstance(inventories, dict) \
                or set(inventories) != set(allowed_inventories) \
                or not isinstance(secret_inventories, dict) \
                or set(secret_inventories) != set(allowed_inventories) \
                or not isinstance(container_phases, dict) \
                or set(container_phases) != set(allowed_inventories):
            raise ControllerRejected("platform handoff outputs are invalid")
        for name, allowed in allowed_inventories.items():
            inventory = inventories[name]
            phase = phases[name]
            if phase not in {"disabled", "bootstrap", "managed"} \
                    or not isinstance(inventory, list) \
                    or len(inventory) != len(set(inventory)) \
                    or not set(inventory).issubset(allowed) \
                    or (phase == "disabled" and inventory) \
                    or (phase == "managed" and set(inventory) != allowed):
                raise ControllerRejected("platform handoff outputs are not exact")
            if container_phases[name] not in {"disabled", "managed"} \
                    or (phase != "disabled" and container_phases[name] != "managed"):
                raise ControllerRejected("platform container gate output is invalid")
        if container_phases[environment] != "managed" \
                or phases[environment] not in {"bootstrap", "managed"}:
            raise ControllerRejected(
                f"{environment} is not authorized by platform handoff"
            )
        normalized_secret_ids: dict[str, set[str]] = {}
        for name, ids in secret_inventories.items():
            if not isinstance(ids, list) or len(ids) != len(set(ids)) \
                    or any(
                        not isinstance(value, str)
                        or re.fullmatch(r"[A-Za-z0-9_-]{1,255}", value) is None
                        for value in ids
                    ):
                raise ControllerRejected("platform secret inventory output is invalid")
            normalized_secret_ids[name] = set(ids)
            if container_phases[name] == "disabled" and ids:
                raise ControllerRejected("disabled container gate exposed secret inventory")
        if normalized_secret_ids["staging"] & normalized_secret_ids["production"]:
            raise ControllerRejected("platform secret inventories are not disjoint")
        runtime = outputs.get("runtime_service_accounts")
        if not isinstance(runtime, dict) \
                or set(runtime) != set(PLATFORM_RUNTIME_SERVICE_ACCOUNTS):
            raise ControllerRejected("platform outputs runtime service accounts missing")
        selected: dict[str, str] = {}
        for account_id in RUNTIME_SERVICE_ACCOUNTS[environment]:
            expected = f"{account_id}@{self.project_id}.iam.gserviceaccount.com"
            if runtime.get(account_id) != expected:
                raise ControllerRejected(
                    f"platform outputs service account mismatch: {account_id}"
                )
            selected[account_id] = expected
        tfvars = {"runtime_service_accounts": selected}
        encoded = _canonical_json(tfvars)
        return encoded, _sha256(raw), str(manifest["manifest_hash"])

    def _validate_secret_refs(
        self, value: Any, *, environment: str, release_phase: str,
    ) -> dict[str, str]:
        allowed = (
            STAGING_SECRET_REF_KEYS if environment == "staging"
            else PRODUCTION_SECRET_REF_KEYS
        )
        if not isinstance(value, dict) or any(
            not isinstance(key, str) or not isinstance(ref, str)
            or SECRET_REF_RE.fullmatch(ref) is None
            or not ref.startswith(f"projects/{self.project_id}/secrets/")
            for key, ref in value.items()
        ):
            raise ControllerRejected(
                "secret refs must use numeric versions in the release project"
            )
        if not set(value).issubset(allowed):
            raise ControllerRejected("secret ref key is outside the closed allowlist")
        expected, _, _ = _runtime_secret_contract(environment, release_phase)
        if set(value) != expected:
            raise ControllerRejected(
                f"{environment} {release_phase} requires exact runtime secret refs"
            )
        return dict(value)

    def _platform_secret_inventory(
        self, *, candidate_sha: str, staging_uri: str, production_uri: str,
        staging_existing_ids: str, production_existing_ids: str,
        container_phases: Mapping[str, str],
    ) -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, str]]:
        inventories: dict[str, list[str]] = {}
        input_hashes: dict[str, str] = {}
        for environment, uri in (
            ("staging", staging_uri), ("production", production_uri),
        ):
            if container_phases.get(environment) == "disabled":
                if uri:
                    raise ControllerRejected(
                        f"{environment} secret input is forbidden while containers are disabled"
                    )
                inventories[environment] = []
                continue
            if container_phases.get(environment) != "managed" or not uri:
                raise ControllerRejected(
                    f"{environment} managed containers require environment tfvars"
                )
            _require_generation_uri(uri, f"{environment} environment tfvars URI")
            if not uri.startswith(
                f"gs://{self.evidence_bucket}/environment-inputs/{environment}/"
            ):
                raise ControllerRejected("platform secret input is outside evidence bucket")
            raw = self.artifacts.read(uri)
            try:
                manifest = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ControllerRejected("platform secret input is not JSON") from exc
            if not isinstance(manifest, dict):
                raise ControllerRejected("platform secret input has invalid shape")
            _verify_manifest(manifest, "manifest_hash")
            if set(manifest) != {
                "schema_version", "artifact_type", "status", "project_id",
                "environment", "main_sha", "tfvars", "manifest_hash",
            } \
                    or manifest.get("schema_version") != 1 \
                    or manifest.get("artifact_type") != "environment_tfvars" \
                    or manifest.get("status") != "passed" \
                    or manifest.get("project_id") != self.project_id \
                    or manifest.get("environment") != environment \
                    or manifest.get("main_sha") != candidate_sha:
                raise ControllerRejected("platform secret input lineage is invalid")
            tfvars = manifest.get("tfvars")
            if not isinstance(tfvars, dict) \
                    or set(tfvars) - ENVIRONMENT_TFVARS[environment]:
                raise ControllerRejected("platform secret input tfvars are invalid")
            containers = tfvars.get("secret_containers")
            expected_keys = (
                STAGING_SECRET_REF_KEYS
                if environment == "staging" else PRODUCTION_SECRET_REF_KEYS
            )
            if not isinstance(containers, dict) or set(containers) != {
                "enabled", "ids", "accessor_roles",
            } or containers.get("enabled") is not True:
                raise ControllerRejected("platform requires enabled secret containers")
            ids = containers.get("ids")
            if not isinstance(ids, dict) or set(ids) != expected_keys \
                    or any(
                        not isinstance(value, str)
                        or re.fullmatch(r"[A-Za-z0-9_-]{1,255}", value) is None
                        for value in ids.values()
                    ):
                raise ControllerRejected("platform secret container inventory is not exact")
            desired = set(ids.values())
            if environment == "staging":
                e2e_ids = tfvars.get("e2e_secret_containers")
                if not isinstance(e2e_ids, dict) or set(e2e_ids) != E2E_SECRET_ENV_KEYS \
                        or any(
                            not isinstance(value, str)
                            or re.fullmatch(r"[A-Za-z0-9_-]{1,255}", value) is None
                            for value in e2e_ids.values()
                        ):
                    raise ControllerRejected("platform E2E secret inventory is not exact")
                desired.update(e2e_ids.values())
            inventories[environment] = sorted(desired)
            input_hashes[environment] = _sha256(raw)
        if set(inventories["staging"]) & set(inventories["production"]):
            raise ControllerRejected("staging and production secret IDs must be disjoint")
        existing_by_environment: dict[str, list[str]] = {}
        for environment, raw_existing in (
            ("staging", staging_existing_ids),
            ("production", production_existing_ids),
        ):
            existing = [] if raw_existing == "" else raw_existing.split(",")
            if len(existing) != len(set(existing)) \
                    or not set(existing).issubset(inventories[environment]):
                raise ControllerRejected(
                    f"existing {environment} secret IDs are outside approved inventory"
                )
            existing_by_environment[environment] = sorted(existing)
        return inventories, existing_by_environment, input_hashes

    def _environment_tfvars(
        self, environment: str, uri: str, *, candidate_sha: str,
        release_phase: str, image_digest: str,
    ) -> tuple[bytes, str, str]:
        if not uri:
            raise ControllerRejected(
                f"environment tfvars URI is required for {environment}"
            )
        _require_generation_uri(uri, "environment tfvars URI")
        if not uri.startswith(
            f"gs://{self.evidence_bucket}/environment-inputs/{environment}/"
        ):
            raise ControllerRejected("environment tfvars must be in the evidence bucket")
        raw = self.artifacts.read(uri)
        try:
            manifest = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ControllerRejected("environment tfvars are not JSON") from exc
        if not isinstance(manifest, dict):
            raise ControllerRejected("environment tfvars must be a JSON object")
        try:
            _verify_manifest(manifest, "manifest_hash")
        except ControllerRejected as exc:
            raise ControllerRejected(f"environment tfvars: {exc}") from exc
        required_fields = {
            "schema_version", "artifact_type", "status", "project_id",
            "environment", "main_sha", "tfvars", "manifest_hash",
        }
        if set(manifest) != required_fields \
                or manifest.get("schema_version") != 1 \
                or manifest.get("artifact_type") != "environment_tfvars" \
                or manifest.get("status") != "passed" \
                or manifest.get("project_id") != self.project_id \
                or manifest.get("environment") != environment \
                or manifest.get("main_sha") != candidate_sha:
            raise ControllerRejected("environment tfvars lineage is invalid")
        supplied = manifest.get("tfvars")
        if not isinstance(supplied, dict):
            raise ControllerRejected("environment tfvars payload missing")
        unknown = set(supplied) - ENVIRONMENT_TFVARS[environment]
        if unknown:
            raise ControllerRejected(
                f"platform outputs contain unapproved {environment} tfvars: "
                + ", ".join(sorted(unknown))
            )
        services_enabled = release_phase != "infra_only"
        e2e_required = environment == "staging" and release_phase in {
            "shadow", "knowledge_only", "full",
        }
        core_env = supplied.get("producer_core_env", {})
        if not isinstance(core_env, dict) or any(
            re.search(r"(?i)(secret|password|token|api[_-]?key|private[_-]?key)", key)
            for key in core_env
        ):
            raise ControllerRejected("producer_core_env contains a secret-shaped key")
        if services_enabled:
            if set(core_env) != CORE_ENV_KEYS or any(
                not isinstance(value, str) or not value.strip()
                for value in core_env.values()
            ):
                raise ControllerRejected("active environment requires exact core env")
            if core_env["GCS_BUCKET"] != "rag-kb-system-kb-articles":
                raise ControllerRejected("core GCS bucket is outside the approved contract")
            _require_https_origin(
                core_env["FORUSBOTS_BASE_URL"], "ForUsBots base URL",
            )
            for boolean_key in ("ENABLE_EXECUTION_LOGGING", "USE_VERTEX_AI"):
                if core_env[boolean_key].lower() not in {"true", "false"}:
                    raise ControllerRejected(f"core boolean is not closed: {boolean_key}")
            for route_key in LLM_ROUTE_ENV_KEYS:
                if not core_env[route_key].startswith(("gpt-", "gemini-")):
                    raise ControllerRejected("LLM route model is outside the closed families")
            _validate_reviewed_llm_pricing(
                core_env["TICKET_LLM_PRICING_JSON"],
                (core_env[key] for key in LLM_ROUTE_ENV_KEYS),
            )
        secret_refs = self._validate_secret_refs(
            supplied.get("secret_version_refs", {}),
            environment=environment, release_phase=release_phase,
        )
        if environment == "production":
            expected_invokers = [
                f"serviceAccount:kb-rag-client@{self.project_id}.iam.gserviceaccount.com"
            ]
            if supplied.get("producer_invoker_members", expected_invokers) != expected_invokers:
                raise ControllerRejected("production invoker inventory mismatch")
            if supplied.get("producer_ingress", "INGRESS_TRAFFIC_ALL") \
                    != "INGRESS_TRAFFIC_ALL":
                raise ControllerRejected("production ingress differs from observed rollout")
            observed = {
                "producer_candidate_tag": "candidate",
                "producer_max_instances": 5,
                "producer_min_instances": 0,
                "producer_concurrency": 80,
                "producer_timeout": "300s",
                "producer_cpu": "1",
                "producer_memory": "512Mi",
                "producer_cpu_idle": True,
                "producer_startup_cpu_boost": True,
                "producer_port": 8000,
            }
            for key, expected_value in observed.items():
                if supplied.get(key, expected_value) != expected_value:
                    raise ControllerRejected(f"production sizing mismatch: {key}")
            expected_startup = {
                "initial_delay_seconds": 0, "timeout_seconds": 240,
                "period_seconds": 240, "failure_threshold": 1,
                "tcp_socket_port": 8000,
            }
            if supplied.get("producer_startup_probe", expected_startup) != expected_startup \
                    or supplied.get("producer_liveness_probe") is not None:
                raise ControllerRejected("production probe inventory mismatch")
        tfvars = dict(supplied)
        tfvars["secret_version_refs"] = secret_refs
        tfvars["shadow_sample_rate"] = 100 if release_phase == "shadow" else 0
        containers = tfvars.get("secret_containers", {
            "enabled": False, "ids": {}, "accessor_roles": {},
        })
        if not isinstance(containers, dict) or set(containers) != {
            "enabled", "ids", "accessor_roles",
        }:
            raise ControllerRejected("secret_containers shape is invalid")
        ids, roles = containers.get("ids"), containers.get("accessor_roles")
        if not isinstance(ids, dict) or not isinstance(roles, dict) \
                or set(ids) != set(roles):
            raise ControllerRejected("secret container ids/roles differ")
        if containers.get("enabled") is True:
            if set(ids) != set(secret_refs) or any(
                not isinstance(secret_id, str)
                or re.fullmatch(r"[A-Za-z0-9_-]{1,255}", secret_id) is None
                or not isinstance(roles[key], list)
                or not roles[key]
                or f"/secrets/{secret_id}/versions/" not in secret_refs[key]
                for key, secret_id in ids.items()
            ):
                raise ControllerRejected("secret containers do not match numeric refs/roles")
            _, _, expected_roles = _runtime_secret_contract(
                environment, release_phase,
            )
            if set(roles) != set(expected_roles) or any(
                set(roles[key]) != expected or len(roles[key]) != len(expected)
                for key, expected in expected_roles.items()
            ):
                raise ControllerRejected("secret container accessor roles are not exact")
            if services_enabled is False:
                raise ControllerRejected("inactive secret containers must be disabled")
        elif containers.get("enabled") is not False or ids or roles:
            raise ControllerRejected("disabled secret containers must be empty")
        elif services_enabled:
            raise ControllerRejected("active environment requires secret containers")
        tfvars["secret_containers"] = containers
        if environment == "staging":
            e2e = tfvars.get("e2e_job", {})
            if not isinstance(e2e, dict) or set(e2e) != {
                "enabled", "image_digest", "service_account_email",
                "nonsecret_env", "secret_version_refs",
            }:
                raise ControllerRejected("staging e2e_job shape is invalid")
            if e2e.get("enabled") is True:
                if not e2e_required:
                    raise ControllerRejected("E2E job is allowed only in ticket-active staging")
                e2e_digest = _require_digest(str(e2e.get("image_digest", "")), "E2E digest")
                if e2e_digest == image_digest:
                    raise ControllerRejected("E2E digest must differ from runtime digest")
                if e2e.get("service_account_email") != (
                    f"ticket-e2e-stg@{self.project_id}.iam.gserviceaccount.com"
                ):
                    raise ControllerRejected("E2E service account mismatch")
                nonsecret_env = e2e.get("nonsecret_env")
                if not isinstance(nonsecret_env, dict) \
                        or set(nonsecret_env) != E2E_NONSECRET_ENV_KEYS \
                        or any(
                            not isinstance(value, str) or not value.strip()
                            for value in nonsecret_env.values()
                        ):
                    raise ControllerRejected("E2E nonsecret env allowlist mismatch")
                if nonsecret_env["E2E_MAIN_SHA"] != candidate_sha:
                    raise ControllerRejected("E2E main SHA does not match candidate")
                for key in (
                    "E2E_N8N_CONTRACT_URL", "E2E_FORUSBOTS_LOOKUP_URL",
                    "E2E_DELIVERY_LOOKUP_URL",
                    "E2E_GCP_AUDIT_CONTRACT_URL", "E2E_DIFFERENTIAL_LEGACY_URL",
                    "E2E_DIFFERENTIAL_LEGACY_AUDIENCE",
                ):
                    if not nonsecret_env[key].startswith("https://"):
                        raise ControllerRejected(f"{key} must use HTTPS")
                _require_generation_uri(
                    nonsecret_env["E2E_PRODUCTION_NEGATIVE_ATTESTATION"],
                    "E2E production negative attestation",
                )
                evidence_destination = nonsecret_env["E2E_EVIDENCE_URI"]
                differential_destination = nonsecret_env[
                    "E2E_DIFFERENTIAL_EVIDENCE_URI"
                ]
                expected_e2e_prefix = (
                    f"gs://{self.evidence_bucket}/handle-ticket/e2e/{candidate_sha}"
                )
                if evidence_destination != f"{expected_e2e_prefix}/e2e.json" \
                        or differential_destination != (
                            f"{expected_e2e_prefix}/differential.json"
                        ):
                    raise ControllerRejected(
                        "E2E evidence URI must be the exact unversioned candidate destination"
                    )
                dimension = nonsecret_env["E2E_PINECONE_DIMENSION"]
                if not dimension.isdigit() or not 1 <= int(dimension) <= 65_536:
                    raise ControllerRejected("E2E Pinecone dimension is invalid")
                namespace = nonsecret_env["E2E_PINECONE_NAMESPACE"]
                if "staging" not in namespace.lower() or namespace == "__default__":
                    raise ControllerRejected("E2E Pinecone namespace is not staging-only")
                e2e_secret_refs = e2e.get("secret_version_refs")
                if not isinstance(e2e_secret_refs, dict) \
                        or set(e2e_secret_refs) != E2E_SECRET_ENV_KEYS \
                        or any(
                            not isinstance(ref, str)
                            or SECRET_REF_RE.fullmatch(ref) is None
                            or not ref.startswith(f"projects/{self.project_id}/secrets/")
                            for ref in e2e_secret_refs.values()
                        ):
                    raise ControllerRejected(
                        "E2E secret refs must match the closed allowlist and use "
                        "numeric versions in the release project"
                    )
                baseline_revision = tfvars.get("producer_baseline_revision")
                if not isinstance(baseline_revision, str) or re.fullmatch(
                    r"kb-rag-system-staging-[a-z0-9-]+", baseline_revision,
                ) is None:
                    raise ControllerRejected(
                        "active staging E2E requires an immutable baseline revision"
                    )
                e2e_containers = tfvars.get("e2e_secret_containers")
                if not isinstance(e2e_containers, dict) \
                        or set(e2e_containers) != E2E_SECRET_ENV_KEYS \
                        or any(
                            not isinstance(secret_id, str)
                            or re.fullmatch(r"[A-Za-z0-9_-]{1,255}", secret_id) is None
                            or f"/secrets/{secret_id}/versions/" not in e2e_secret_refs[key]
                            for key, secret_id in e2e_containers.items()
                        ):
                    raise ControllerRejected(
                        "E2E secret containers do not match numeric refs"
                    )
            elif e2e.get("enabled") is not False or e2e_required:
                raise ControllerRejected("ticket-active staging requires the E2E job")
            elif any((
                e2e.get("image_digest") != "",
                e2e.get("service_account_email") != "",
                e2e.get("nonsecret_env") != {},
                e2e.get("secret_version_refs") != {},
            )):
                raise ControllerRejected("disabled E2E job inputs must be empty")
            elif tfvars.get("e2e_secret_containers", {}) != {}:
                raise ControllerRejected("disabled E2E secret inputs must be empty")
            else:
                baseline_revision = tfvars.get("producer_baseline_revision", "")
                if release_phase == "dark_no_traffic":
                    if not isinstance(baseline_revision, str) or re.fullmatch(
                        r"kb-rag-system-staging-[a-z0-9-]+", baseline_revision,
                    ) is None:
                        raise ControllerRejected(
                            "dark_no_traffic requires an immutable baseline revision"
                        )
                elif baseline_revision != "":
                    raise ControllerRejected(
                        "bootstrap/infra staging baseline revision must be empty"
                    )
            if tfvars.get("producer_baseline_tag") != "baseline" \
                    or tfvars.get("producer_candidate_tag") != "candidate":
                raise ControllerRejected("staging traffic tags must be baseline/candidate")
        encoded = _canonical_json(tfvars)
        return encoded, _sha256(raw), str(manifest["manifest_hash"])

    def _publish_platform_outputs(
        self, *, candidate_sha: str, root: Path,
    ) -> dict[str, str]:
        """Publish the exact post-apply platform handoff as a write-once object."""
        raw = self.tools.run(
            ["terraform", "output", "-json"], cwd=root, capture=True,
        )
        raw_bytes = raw.encode()
        try:
            terraform_outputs = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ControllerRejected("platform terraform output is not JSON") from exc
        required = {
            "runtime_service_accounts", "evidence_bucket",
            "firestore_scope_phase", "firestore_scope_enforced",
            "pipeline_service_accounts", "environment_handoff_phase",
            "environment_run_resources", "environment_secret_ids",
            "environment_container_phase",
            "environment_release_phase",
        }
        if not isinstance(terraform_outputs, dict) \
                or not required.issubset(terraform_outputs):
            raise ControllerRejected("platform terraform outputs are incomplete")
        outputs: dict[str, Any] = {}
        for name in required:
            descriptor = terraform_outputs.get(name)
            if not isinstance(descriptor, dict) or "value" not in descriptor:
                raise ControllerRejected(f"platform terraform output invalid: {name}")
            if descriptor.get("sensitive") is not False:
                raise ControllerRejected(f"platform terraform output is sensitive: {name}")
            outputs[name] = descriptor["value"]
        if outputs["evidence_bucket"] != self.evidence_bucket:
            raise ControllerRejected("platform output evidence bucket mismatch")
        runtime = outputs["runtime_service_accounts"]
        if not isinstance(runtime, dict) \
                or set(runtime) != set(PLATFORM_RUNTIME_SERVICE_ACCOUNTS):
            raise ControllerRejected("platform runtime service accounts are invalid")
        for account_id in PLATFORM_RUNTIME_SERVICE_ACCOUNTS:
            expected = f"{account_id}@{self.project_id}.iam.gserviceaccount.com"
            if runtime.get(account_id) != expected:
                raise ControllerRejected(
                    f"platform runtime service account mismatch: {account_id}"
                )
        state_raw = self.tools.run(
            ["terraform", "state", "pull"], cwd=root, capture=True,
        )
        lineage, serial = _state_metadata(state_raw)
        manifest = _manifest({
            "artifact_type": "platform_outputs",
            "status": "passed",
            "project_id": self.project_id,
            "platform_candidate_sha": candidate_sha,
            "platform_state_lineage": lineage,
            "platform_state_serial": serial,
            "terraform_outputs_hash": _sha256(raw_bytes),
            "outputs": outputs,
        }, "manifest_hash")
        uri = self._write(
            f"platform-outputs/{candidate_sha}/{serial}/platform_outputs.json",
            _canonical_json(manifest),
        )
        return {
            "platform_outputs_uri": uri,
            "platform_outputs_hash": str(manifest["manifest_hash"]),
            "terraform_outputs_hash": str(manifest["terraform_outputs_hash"]),
        }

    def _plan(self, args) -> dict[str, Any]:
        controller_digest = _require_digest(
            args.controller_digest or self.controller_digest, "controller digest",
        )
        candidate = self._checkout(args.candidate_sha)
        root = candidate / "infra" / "terraform" / "live" / args.environment
        lock = root / ".terraform.lock.hcl"
        backend_bucket = _validate_backend(root, args.environment)
        platform_outputs_hash = "none"
        platform_outputs_manifest_hash = "none"
        environment_inputs_hash = "none"
        environment_inputs_manifest_hash = "none"
        environment_tfvars_hash = "none"
        work = candidate / ".release-controller"
        work.mkdir()
        tfvars_path: Optional[Path] = None
        semantic_tfvars: dict[str, Any] = {}
        platform_handoff: dict[str, Any] = {
            "phases": {"staging": "disabled", "production": "disabled"},
            "resources": {"staging": [], "production": []},
        }
        platform_secret_inventory: dict[str, list[str]] = {
            "staging": [], "production": [],
        }
        platform_container_phases = {
            "staging": "disabled", "production": "disabled",
        }
        existing_platform_secret_ids: dict[str, list[str]] = {
            "staging": [], "production": [],
        }
        platform_secret_input_hashes: dict[str, str] = {}
        platform_secret_input_uris = {"staging": "", "production": ""}
        platform_gate_image_digests = {"staging": "", "production": ""}
        platform_release_phases = {"staging": "disabled", "production": "disabled"}
        platform_pipeline_inputs: dict[str, Any] = {}
        if args.environment == "platform":
            if args.firestore_scope_phase not in FIRESTORE_PHASES:
                raise ControllerRejected("invalid Firestore scope phase")
            image_digest = "none"
            release_phase = f"firestore-{args.firestore_scope_phase}"
            platform_pipeline_inputs = _platform_pipeline_inputs(
                args, controller_digest,
            )
            platform_handoff = {
                "phases": {
                    "staging": args.staging_handoff_phase,
                    "production": args.production_handoff_phase,
                },
                "resources": {
                    "staging": _run_resource_list(
                        args.staging_run_resources, environment="staging",
                    ),
                    "production": _run_resource_list(
                        args.production_run_resources, environment="production",
                    ),
                },
            }
            platform_container_phases = {
                "staging": args.staging_container_phase,
                "production": args.production_container_phase,
            }
            platform_release_phases = {
                "staging": args.staging_release_phase,
                "production": args.production_release_phase,
            }
            for environment, digest in (
                ("staging", args.staging_approved_image_digest),
                ("production", args.production_approved_image_digest),
            ):
                if platform_container_phases[environment] == "managed":
                    platform_gate_image_digests[environment] = _require_digest(
                        digest, f"{environment} approved image digest",
                    )
                elif digest:
                    raise ControllerRejected(
                        f"{environment} image digest is forbidden while containers are disabled"
                    )
            if any(
                platform_handoff["phases"][environment] != "disabled"
                and platform_container_phases[environment] != "managed"
                for environment in ("staging", "production")
            ):
                raise ControllerRejected(
                    "Run handoff requires managed environment containers"
                )
            if "managed" in platform_container_phases.values():
                (
                    platform_secret_inventory,
                    existing_platform_secret_ids,
                    platform_secret_input_hashes,
                ) = self._platform_secret_inventory(
                    candidate_sha=args.candidate_sha,
                    staging_uri=args.staging_environment_tfvars_uri,
                    production_uri=args.production_environment_tfvars_uri,
                    staging_existing_ids=args.staging_existing_secret_ids,
                    production_existing_ids=args.production_existing_secret_ids,
                    container_phases=platform_container_phases,
                )
            elif args.staging_existing_secret_ids \
                    or args.production_existing_secret_ids \
                    or args.staging_environment_tfvars_uri \
                    or args.production_environment_tfvars_uri:
                raise ControllerRejected(
                    "disabled containers forbid secret inventory inputs"
                )
            platform_secret_input_uris = {
                "staging": args.staging_environment_tfvars_uri,
                "production": args.production_environment_tfvars_uri,
            }
        else:
            if args.release_phase not in RELEASE_PHASES:
                raise ControllerRejected("invalid release phase")
            if args.environment == "production" and args.release_phase == "infra_only":
                raise ControllerRejected("production does not allow infra_only")
            if args.environment == "staging" and args.release_phase == "infra_only" \
                    and not args.image_digest:
                image_digest = ""
            else:
                image_digest = _require_digest(args.image_digest)
            release_phase = args.release_phase
            platform_tfvars, platform_outputs_hash, platform_outputs_manifest_hash = (
                self._platform_tfvars(args.environment, args.platform_outputs_uri)
            )
            environment_tfvars, environment_inputs_hash, environment_inputs_manifest_hash = (
                self._environment_tfvars(
                    args.environment, args.environment_tfvars_uri,
                    candidate_sha=args.candidate_sha,
                    release_phase=release_phase, image_digest=image_digest,
                )
            )
            merged = json.loads(platform_tfvars)
            merged.update(json.loads(environment_tfvars))
            semantic_tfvars = merged
            tfvars = _canonical_json(merged)
            environment_tfvars_hash = _sha256(tfvars)
            tfvars_path = work / f"{args.environment}.tfvars.json"
            tfvars_path.write_bytes(tfvars)
        promotion_hash = "none"
        if args.environment == "production":
            _require_generation_uri(args.promotion_uri, "promotion URI")
            promotion = self._load_json(args.promotion_uri, "promotion")
            self._validate_promotion_document(
                promotion, main_sha=args.candidate_sha,
                image_digest=image_digest,
                controller_digest=controller_digest,
            )
            promotion_hash = promotion["attestation_hash"]

        self.tools.run([
            "terraform", "init", "-input=false", "-lockfile=readonly",
        ], cwd=root)
        state_generation = self.tools.backend_state_generation(backend_bucket)
        if state_generation is None:
            lineage, serial = "__absent__", -1
        else:
            state_raw = self.tools.run(
                ["terraform", "state", "pull"], cwd=root, capture=True,
            )
            lineage, serial = _state_metadata(state_raw)
        plan_path = work / "plan.tfplan"
        plan_command = [
            "terraform", "plan", "-input=false", "-lock-timeout=120s",
            f"-var=project_id={self.project_id}", "-var=region=us-central1",
        ]
        if args.environment == "platform":
            plan_command.extend([
                "-var=cicd_bootstrap=" + _canonical_json(
                    platform_pipeline_inputs["cicd_bootstrap"]
                ).decode(),
                "-var=gate_approver_accounts=" + _canonical_json(
                    platform_pipeline_inputs["gate_approver_accounts"]
                ).decode(),
                "-var=production_release_group_email=" + json.dumps(
                    platform_pipeline_inputs["production_release_group_email"]
                ),
                "-var=enable_legacy_trigger_neutralization=" + str(
                    platform_pipeline_inputs["enable_legacy_trigger_neutralization"]
                ).lower(),
                f"-var=firestore_scope_migration={{enabled={str(args.firestore_scope_phase != 'disabled').lower()},phase=\"{args.firestore_scope_phase}\",import_legacy={str(args.firestore_scope_phase == 'prepare').lower()}}}"
                , "-var=environment_handoff_phase=" + _canonical_json(
                    platform_handoff["phases"]
                ).decode(),
                "-var=environment_run_resources=" + _canonical_json(
                    platform_handoff["resources"]
                ).decode(),
                "-var=environment_secret_ids=" + _canonical_json(
                    platform_secret_inventory
                ).decode(),
                "-var=environment_container_phase=" + _canonical_json(
                    platform_container_phases
                ).decode(),
                "-var=environment_release_phase=" + _canonical_json(
                    platform_release_phases
                ).decode(),
                "-var=existing_environment_secret_ids=" + _canonical_json(
                    existing_platform_secret_ids
                ).decode(),
            ])
        else:
            plan_command.extend([
                f"-var-file={tfvars_path}",
                f"-var=image_digest={image_digest}",
                f"-var=release_phase={release_phase}",
            ])
        plan_command.append(f"-out={plan_path}")
        self.tools.run(plan_command, cwd=root)
        if not plan_path.is_file():
            raise ControllerRejected("terraform plan produced no binary plan")
        show = _sanitize_terraform_show(self.tools.run(
            ["terraform", "show", "-no-color", str(plan_path)],
            cwd=root, capture=True,
        ))
        show_json_raw = self.tools.run([
            "terraform", "show", "-json", str(plan_path),
        ], cwd=root, capture=True)
        try:
            show_json = json.loads(show_json_raw)
        except json.JSONDecodeError as exc:
            raise ControllerRejected("Terraform JSON plan is invalid") from exc
        if args.environment == "platform":
            _validate_platform_plan(
                show_json, project_id=self.project_id, handoff=platform_handoff,
                secret_inventory=platform_secret_inventory,
                container_phases=platform_container_phases,
            )
        else:
            _validate_environment_plan(
                show_json, environment=args.environment,
                project_id=self.project_id, tfvars=semantic_tfvars,
                image_digest=image_digest, release_phase=release_phase,
            )
        bundle_path = work / "source.zip"
        bundle = _deterministic_bundle(candidate / "infra" / "terraform", bundle_path)
        plan_bytes = plan_path.read_bytes()
        build_id = os.environ.get("BUILD_ID") or f"local-{uuid.uuid4().hex}"
        prefix = f"plans/{args.environment}/{build_id}"
        plan_uri = self._write(f"{prefix}/plan.tfplan", plan_bytes)
        show_uri = self._write(f"{prefix}/terraform_show.txt", show.encode())
        show_json_uri = self._write(
            f"{prefix}/terraform_show.json", show_json_raw.encode(),
        )
        bundle_uri = self._write(f"{prefix}/source.zip", bundle)
        plan_hash = _sha256(plan_bytes)
        gate_scope = {
            "_CANDIDATE_SHA": args.candidate_sha,
            "_CONTROLLER_DIGEST": controller_digest,
            "_PLAN_URI": plan_uri,
            "_PLAN_SHA256": plan_hash,
            "_ROOT": args.environment,
            "_RELEASE_PHASE": release_phase,
            "_IMAGE_DIGEST": image_digest,
            "_PLATFORM_CONTAINER_PHASES_SHA256": _sha256(
                _canonical_json(platform_container_phases)
            ),
            "_PLATFORM_APPROVED_IMAGE_DIGESTS_SHA256": _sha256(
                _canonical_json(platform_gate_image_digests)
            ),
            "_PLATFORM_RELEASE_PHASES_SHA256": _sha256(
                _canonical_json(platform_release_phases)
            ),
        }
        manifest_body = {
            "root": args.environment,
            "backend_bucket": backend_bucket,
            "state_lineage": lineage,
            "state_serial": serial,
            "state_generation": state_generation or "absent",
            "candidate_sha": args.candidate_sha,
            "image_digest": image_digest,
            "release_phase": release_phase,
            "provider_lock_hash": _sha256(lock.read_bytes()),
            "plan_hash": plan_hash,
            "show_hash": _sha256(show.encode()),
            "show_json_hash": _sha256(show_json_raw.encode()),
            "bundle_hash": _sha256(bundle),
            "plan_uri": plan_uri,
            "show_uri": show_uri,
            "show_json_uri": show_json_uri,
            "bundle_uri": bundle_uri,
            "promotion_uri": args.promotion_uri or "none",
            "promotion_hash": promotion_hash,
            "controller_digest": controller_digest,
            "platform_outputs_uri": (
                args.platform_outputs_uri if args.environment != "platform" else "none"
            ),
            "platform_outputs_hash": platform_outputs_hash,
            "platform_outputs_manifest_hash": platform_outputs_manifest_hash,
            "environment_tfvars_uri": (
                args.environment_tfvars_uri if args.environment != "platform" else "none"
            ),
            "environment_inputs_hash": environment_inputs_hash,
            "environment_inputs_manifest_hash": environment_inputs_manifest_hash,
            "environment_tfvars_hash": environment_tfvars_hash,
            "platform_handoff": platform_handoff,
            "platform_secret_inventory": platform_secret_inventory,
            "platform_container_phases": platform_container_phases,
            "existing_platform_secret_ids": existing_platform_secret_ids,
            "platform_secret_input_hashes": platform_secret_input_hashes,
            "platform_secret_input_uris": platform_secret_input_uris,
            "platform_gate_image_digests": platform_gate_image_digests,
            "platform_release_phases": platform_release_phases,
            "platform_pipeline_inputs": platform_pipeline_inputs,
            "gate_scope": gate_scope,
        }
        manifest_body["required_gate_roles"] = self._required_apply_gate_roles(
            manifest_body
        )
        manifest = _manifest(manifest_body, "manifest_hash")
        manifest_uri = self._write(
            f"{prefix}/plan_manifest.json", _canonical_json(manifest),
        )
        return {
            "plan_uri": plan_uri,
            "plan_sha256": manifest["plan_hash"],
            "plan_manifest_uri": manifest_uri,
            "plan_manifest_hash": manifest["manifest_hash"],
        }

    def _apply(self, args) -> dict[str, Any]:
        _require_generation_uri(args.plan_uri, "plan URI")
        _require_hash(args.plan_sha256, "approved plan hash")
        match = PLAN_URI_RE.fullmatch(args.plan_uri)
        if match is None or match.group(1) != args.environment:
            raise ControllerRejected("plan URI does not belong to requested root")
        plan_bytes = self.artifacts.read(args.plan_uri)
        if _sha256(plan_bytes) != args.plan_sha256:
            raise ControllerRejected("approved plan hash does not match bytes")
        manifest_object = args.plan_uri.split("#", 1)[0].replace(
            "/plan.tfplan", "/plan_manifest.json",
        )
        manifest_uri = self.artifacts.resolve(manifest_object)
        manifest = self._load_json(manifest_uri, "plan manifest")
        _verify_manifest(manifest, "manifest_hash")
        current_controller_digest = _require_digest(
            args.controller_digest or self.controller_digest,
            "controller digest",
        )
        if manifest.get("controller_digest") != current_controller_digest:
            raise ControllerRejected("plan controller digest mismatch")
        expected = {
            "root": args.environment,
            "plan_uri": args.plan_uri,
            "plan_hash": args.plan_sha256,
        }
        for key, value in expected.items():
            if manifest.get(key) != value:
                raise ControllerRejected(f"plan manifest {key} mismatch")
        if args.environment != "platform":
            platform_uri = str(manifest.get("platform_outputs_uri", ""))
            platform_tfvars, raw_hash, platform_manifest_hash = self._platform_tfvars(
                args.environment, platform_uri,
            )
            environment_tfvars, input_hash, input_manifest_hash = self._environment_tfvars(
                args.environment, str(manifest.get("environment_tfvars_uri", "")),
                candidate_sha=str(manifest.get("candidate_sha", "")),
                release_phase=str(manifest.get("release_phase", "")),
                image_digest=str(manifest.get("image_digest", "")),
            )
            merged = json.loads(platform_tfvars)
            merged.update(json.loads(environment_tfvars))
            semantic_tfvars = merged
            tfvars = _canonical_json(merged)
            if raw_hash != manifest.get("platform_outputs_hash") \
                    or platform_manifest_hash != manifest.get(
                        "platform_outputs_manifest_hash"
                    ) \
                    or input_hash != manifest.get("environment_inputs_hash") \
                    or input_manifest_hash != manifest.get(
                        "environment_inputs_manifest_hash"
                    ) \
                    or _sha256(tfvars) != manifest.get("environment_tfvars_hash"):
                raise ControllerRejected("release inputs differ from approved plan")
        candidate = self._checkout(str(manifest.get("candidate_sha", "")))
        root = candidate / "infra" / "terraform" / "live" / args.environment
        backend_bucket = _validate_backend(root, args.environment)
        if manifest.get("backend_bucket") != backend_bucket:
            raise ControllerRejected("plan manifest backend bucket mismatch")
        lock = root / ".terraform.lock.hcl"
        if _sha256(lock.read_bytes()) != manifest.get("provider_lock_hash"):
            raise ControllerRejected("provider lock hash mismatch")
        bundle_uri = str(manifest.get("bundle_uri", ""))
        bundle_bytes = self.artifacts.read(bundle_uri)
        if _sha256(bundle_bytes) != manifest.get("bundle_hash"):
            raise ControllerRejected("approved source bundle hash mismatch")
        recomputed_bundle_path = candidate / ".recomputed-source.zip"
        recomputed_bundle = _deterministic_bundle(
            candidate / "infra" / "terraform", recomputed_bundle_path,
        )
        if _sha256(recomputed_bundle) != manifest.get("bundle_hash"):
            raise ControllerRejected("candidate checkout differs from source bundle")
        plan_path = candidate / ".approved.tfplan"
        plan_path.write_bytes(plan_bytes)
        current_generation = self.tools.backend_state_generation(backend_bucket)
        expected_generation = manifest.get("state_generation")
        observed_generation = current_generation or "absent"
        if observed_generation != expected_generation:
            raise ControllerRejected("Terraform backend state generation drift")
        self.tools.run([
            "terraform", "init", "-input=false", "-lockfile=readonly",
        ], cwd=root)
        if current_generation is None:
            lineage, serial = "__absent__", -1
        else:
            current_state = self.tools.run(
                ["terraform", "state", "pull"], cwd=root, capture=True,
            )
            lineage, serial = _state_metadata(current_state)
        if lineage != manifest.get("state_lineage") or serial != manifest.get("state_serial"):
            raise ControllerRejected("Terraform state lineage/serial drift")
        show = _sanitize_terraform_show(self.tools.run(
            ["terraform", "show", "-no-color", str(plan_path)],
            cwd=root, capture=True,
        ))
        if _sha256(show.encode()) != manifest.get("show_hash"):
            raise ControllerRejected("approved terraform show hash mismatch")
        show_json_raw = self.tools.run([
            "terraform", "show", "-json", str(plan_path),
        ], cwd=root, capture=True)
        if _sha256(show_json_raw.encode()) != manifest.get("show_json_hash"):
            raise ControllerRejected("approved Terraform JSON plan hash mismatch")
        stored_show_json = self.artifacts.read(str(manifest.get("show_json_uri", "")))
        if _sha256(stored_show_json) != manifest.get("show_json_hash"):
            raise ControllerRejected("stored Terraform JSON plan hash mismatch")
        try:
            show_json = json.loads(show_json_raw)
        except json.JSONDecodeError as exc:
            raise ControllerRejected("Terraform JSON plan is invalid") from exc
        if args.environment == "platform":
            _validate_platform_plan(
                show_json, project_id=self.project_id,
                handoff=manifest.get("platform_handoff"),
                secret_inventory=manifest.get("platform_secret_inventory"),
                container_phases=manifest.get("platform_container_phases"),
            )
        else:
            _validate_environment_plan(
                show_json, environment=args.environment,
                project_id=self.project_id, tfvars=semantic_tfvars,
                image_digest=str(manifest.get("image_digest", "")),
                release_phase=str(manifest.get("release_phase", "")),
            )
        if args.environment == "production":
            promotion_uri = manifest.get("promotion_uri")
            if promotion_uri != args.promotion_uri:
                raise ControllerRejected("production promotion URI mismatch")
            promotion = self._load_json(promotion_uri, "promotion")
            if promotion["attestation_hash"] != manifest.get("promotion_hash"):
                raise ControllerRejected("production promotion hash mismatch")
            self._validate_promotion_document(
                promotion, main_sha=str(manifest["candidate_sha"]),
                image_digest=str(manifest["image_digest"]),
                controller_digest=str(manifest.get("controller_digest", "")),
            )
        gate_manifest = dict(manifest)
        gate_scope = dict(manifest.get("gate_scope", {}))
        if manifest.get("release_phase") == "firestore-enforce":
            _require_generation_uri(args.prepare_smoke_uri, "G1C prepare smoke URI")
            gate_scope["_PREPARE_SMOKE_URI"] = args.prepare_smoke_uri
        elif args.prepare_smoke_uri:
            raise ControllerRejected("prepare smoke URI is only valid for G1C enforce")
        gate_manifest["gate_scope"] = gate_scope
        gate_receipt_hashes = self._validate_apply_gate_receipts(
            gate_manifest, args.gate_receipts,
        )
        paused_queues: list[str] = []
        if args.environment == "platform":
            phases = manifest.get("platform_container_phases", {})
            for environment, queue_name in (
                ("staging", "ticket-jobs-staging"),
                ("production", "ticket-jobs-prod"),
            ):
                if isinstance(phases, Mapping) and phases.get(environment) == "managed":
                    self.tools.pause_existing_queue_and_verify_empty(queue_name)
        self.tools.run([
            "terraform", "apply", "-input=false", "-auto-approve",
            str(plan_path),
        ], cwd=root)
        if args.environment == "platform":
            phases = manifest.get("platform_container_phases", {})
            for environment, queue_name in (
                ("staging", "ticket-jobs-staging"),
                ("production", "ticket-jobs-prod"),
            ):
                if isinstance(phases, Mapping) and phases.get(environment) == "managed":
                    self.tools.pause_and_verify_empty_queue(queue_name)
                    paused_queues.append(queue_name)
        result = {"status": "applied", "root": args.environment,
                  "plan_sha256": args.plan_sha256,
                  "gate_receipt_hashes": gate_receipt_hashes,
                  "operationally_paused_empty_queues": paused_queues}
        if args.environment == "platform":
            result.update(self._publish_platform_outputs(
                candidate_sha=str(manifest["candidate_sha"]), root=root,
            ))
        return result

    def _load_json(self, uri: str, label: str) -> dict[str, Any]:
        _require_generation_uri(uri, label)
        try:
            body = json.loads(self.artifacts.read(uri))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ControllerRejected(f"{label} is not JSON") from exc
        if not isinstance(body, dict):
            raise ControllerRejected(f"{label} must be a JSON object")
        return body

    @staticmethod
    def _required_apply_gate_roles(manifest: Mapping[str, Any]) -> list[str]:
        root = manifest.get("root")
        required: set[str] = set()
        if root == "platform":
            pipeline_inputs = manifest.get("platform_pipeline_inputs", {})
            cicd = pipeline_inputs.get("cicd_bootstrap", {}) \
                if isinstance(pipeline_inputs, Mapping) else {}
            if isinstance(cicd, Mapping) and cicd.get("enabled") is True:
                # Initial G1B is the plan's one manual bootstrap exception;
                # every controller-managed platform apply is post-bootstrap.
                required.update({"g1b-gcp-owner", "g1b-release-owner"})
            phases = manifest.get("platform_container_phases", {})
            if isinstance(phases, dict) and phases.get("staging") == "managed":
                required.add("g2-gcp-owner")
            if isinstance(phases, dict) and phases.get("production") == "managed":
                required.update({
                    "g6b-gcp-owner", "g6b-release-owner",
                    "g6b-forusbots-owner",
                })
            firestore_phase = str(manifest.get("release_phase", "")).removeprefix(
                "firestore-"
            )
            if firestore_phase in {"prepare", "enforce"}:
                required.update({
                    f"g1c-{firestore_phase}-gcp-owner",
                    f"g1c-{firestore_phase}-api-owner",
                    f"g1c-{firestore_phase}-operations",
                })
        elif root == "staging":
            required.add("g2-gcp-owner")
        elif root == "production":
            required.update({
                "g6b-gcp-owner", "g6b-release-owner", "g6b-forusbots-owner",
            })
        else:
            raise ControllerRejected("plan root has no gate policy")
        return sorted(required)

    @staticmethod
    def _gate_role_parts(key: str) -> tuple[str, str]:
        patterns = (
            (r"g1c-(prepare|enforce)-(gcp-owner|api-owner|operations)", None),
            (r"g1b-(gcp-owner|release-owner)", "G1B"),
            (r"g2-(gcp-owner)", "G2"),
            (r"g4-(requester|n8n-owner|participant-plan-owner|forusbots-owner|delivery-owner)", "G4"),
            (r"g5-(maintainer|requester)", "G5"),
            (r"g5v-(security-owner|release-owner|requester)", "G5V"),
            (r"g6b-(gcp-owner|release-owner|forusbots-owner)", "G6B"),
        )
        for pattern, literal in patterns:
            match = re.fullmatch(pattern, key)
            if match is None:
                continue
            if literal is None:
                return f"G1C_{match.group(1).upper()}", match.group(2)
            return literal, match.group(1)
        raise ControllerRejected("gate receipt role key is outside policy")

    def _validate_apply_gate_receipts(
        self, manifest: Mapping[str, Any], raw_build_ids: str,
    ) -> dict[str, str]:
        root = manifest.get("root")
        phase = manifest.get("release_phase")
        if root == "staging" and phase in {"shadow", "knowledge_only", "full"}:
            raise ControllerRejected(
                "active staging apply is blocked until authenticated G4 quorum "
                "receipts bind differential and semantic evidence"
            )
        if root == "production" and phase in {"shadow", "knowledge_only", "full"}:
            raise ControllerRejected(
                "production activation is blocked until authenticated G7/G8/G9 "
                "quorum receipts are implemented"
            )
        if root == "platform":
            release_phases = manifest.get("platform_release_phases", {})
            if isinstance(release_phases, Mapping) and any(
                value in {"shadow", "knowledge_only", "full"}
                for value in release_phases.values()
            ):
                raise ControllerRejected(
                    "platform active queue/scheduler transition is blocked until "
                    "G4 binds the exact plan and evidence"
                )
        if phase == "firestore-enforce":
            raise ControllerRejected(
                "G1C enforce is blocked until a trusted prepare smoke chain is verified"
            )
        required = self._required_apply_gate_roles(manifest)
        scope = manifest.get("gate_scope")
        if not isinstance(scope, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in scope.items()
        ):
            raise ControllerRejected("plan gate scope is invalid")
        return self._validate_scoped_receipt_set(
            required, raw_build_ids, {key: scope for key in required},
            label="apply",
        )

    def _validate_scoped_receipt_set(
        self, required: Sequence[str], raw_build_ids: str,
        scopes: Mapping[str, Optional[Mapping[str, str]]], *, label: str,
        bindings: Optional[Mapping[str, str]] = None,
    ) -> dict[str, str]:
        build_ids = _receipt_build_ids(raw_build_ids)
        if set(build_ids) != set(required) or len(set(build_ids.values())) != len(required):
            raise ControllerRejected(
                f"{label} requires the exact authenticated multiparty gate receipt quorum"
            )
        receipts: dict[str, str] = {}
        approvers: set[str] = set()
        for key in required:
            payload = self.tools.describe_build(build_ids[key])
            scope = scopes.get(key)
            if scope is None:
                substitutions = payload.get("substitutions")
                if not isinstance(substitutions, Mapping):
                    raise ControllerRejected(f"authenticated {label} scope is missing")
                scope = {
                    str(name): str(value) for name, value in substitutions.items()
                    if isinstance(name, str) and name.startswith("_")
                    and isinstance(value, str)
                }
            if bindings and any(scope.get(name) != value for name, value in bindings.items()):
                raise ControllerRejected(f"authenticated {label} artifact binding is invalid")
            gate, role = self._gate_role_parts(key)
            normalized = self._validate_gate_build(
                payload, gate_key=key, gate=gate, approver_role=role,
                build_id=build_ids[key], scope=scope, require_success=True,
            )
            approver = str(normalized["approver_account"])
            if approver in approvers:
                raise ControllerRejected("multiparty gate approvers must be distinct")
            approvers.add(approver)
            receipts[key] = _sha256(_canonical_json(normalized))
        return receipts

    def _validate_gate_build(
        self, payload: Mapping[str, Any], *, gate_key: str, gate: str,
        approver_role: str, build_id: str, scope: Mapping[str, str],
        require_success: bool,
    ) -> dict[str, Any]:
        trigger_id = payload.get("buildTriggerId")
        if not isinstance(trigger_id, str) or not trigger_id:
            raise ControllerRejected(f"authenticated {gate} trigger ID is missing")
        trigger = self.tools.describe_trigger(trigger_id)
        trigger_name = f"handle-ticket-gate-{gate_key}"
        account_id = GATE_SERVICE_ACCOUNT_IDS.get(gate_key)
        if account_id is None:
            raise ControllerRejected("gate receipt service account is outside policy")
        service_account = f"{account_id}@{self.project_id}.iam.gserviceaccount.com"
        trigger_build = trigger.get("build")
        trigger_steps = trigger_build.get("steps") if isinstance(trigger_build, dict) else None
        fixed_prefix = [
            "gate-receipt", "--gate", gate,
            "--approver-role", approver_role, "--approver-accounts",
        ]
        trigger_env = [
            "BUILD_ID=$BUILD_ID",
            "PROJECT_ID=$PROJECT_ID",
            "COMMIT_SHA=$COMMIT_SHA",
        ]
        if not isinstance(trigger_steps, list) or len(trigger_steps) != 1 \
                or not isinstance(trigger_steps[0], dict) \
                or trigger_steps[0].get("name") != scope.get("_CONTROLLER_DIGEST") \
                or not isinstance(trigger_steps[0].get("args"), list) \
                or trigger_steps[0]["args"][:6] != fixed_prefix \
                or len(trigger_steps[0]["args"]) != 7 \
                or trigger_steps[0].get("env") != trigger_env:
            raise ControllerRejected(f"trusted {gate} trigger command is invalid")
        allowed_approvers = _account_set(
            str(trigger_steps[0]["args"][6]), gate=f"{gate}/{approver_role}",
        )
        source = trigger.get("sourceToBuild")
        approval_config = trigger.get("approvalConfig")
        if trigger.get("id") != trigger_id \
                or trigger.get("name") != trigger_name \
                or not str(trigger.get("serviceAccount", "")).endswith(
                    f"/serviceAccounts/{service_account}"
                ) \
                or not isinstance(approval_config, dict) \
                or approval_config.get("approvalRequired") is not True \
                or not isinstance(source, dict) \
                or source.get("uri") != "https://github.com/ialvisti/ForUsGuide" \
                or source.get("ref") != "refs/heads/main" \
                or source.get("repoType") != "GITHUB":
            raise ControllerRejected(f"trusted {gate} trigger identity is invalid")
        approval = payload.get("approval")
        result = approval.get("result") if isinstance(approval, dict) else None
        approver = result.get("approverAccount") if isinstance(result, dict) else None
        if not isinstance(approval, dict) or approval.get("state") != "APPROVED" \
                or not isinstance(result, dict) \
                or result.get("decision") != "APPROVED" \
                or approver not in allowed_approvers:
            raise ControllerRejected(f"authenticated {gate} approval is invalid")
        expected_status = {"SUCCESS"} if require_success else {"WORKING", "SUCCESS"}
        if payload.get("id") != build_id \
                or payload.get("name") != (
                    f"projects/{self.project_id}/locations/global/builds/{build_id}"
                ) \
                or payload.get("projectId") != self.project_id \
                or payload.get("status") not in expected_status \
                or not str(payload.get("serviceAccount", "")).endswith(
                    f"/serviceAccounts/{service_account}"
                ):
            raise ControllerRejected(f"authenticated {gate} build identity is invalid")
        provenance = payload.get("sourceProvenance")
        sources = [] if not isinstance(provenance, dict) else [
            source for source in (
                provenance.get("resolvedRepoSource"),
                provenance.get("resolvedGitSource"),
            ) if isinstance(source, dict) and source
        ]
        controller_source_shas = set() if len(sources) != 1 else {
            value for value in (sources[0].get("commitSha"), sources[0].get("revision"))
            if isinstance(value, str) and SHA_RE.fullmatch(value)
        }
        if len(controller_source_shas) != 1:
            raise ControllerRejected(f"authenticated {gate} controller source SHA is invalid")
        controller_source_sha = next(iter(controller_source_shas))
        candidate_sha = scope.get("_CANDIDATE_SHA", "")
        if not isinstance(candidate_sha, str) or SHA_RE.fullmatch(candidate_sha) is None:
            raise ControllerRejected(f"authenticated {gate} candidate SHA is invalid")
        substitutions = payload.get("substitutions")
        user_substitutions = (
            {key: value for key, value in substitutions.items() if key.startswith("_")}
            if isinstance(substitutions, dict) else {}
        )
        if user_substitutions != dict(scope):
            raise ControllerRejected(f"authenticated {gate} scope is invalid")
        steps = payload.get("steps")
        execution_fields = (
            "name", "args", "secretEnv", "script", "entrypoint",
            "dir", "volumes",
        )
        expected_executed_env = [
            f"BUILD_ID={build_id}",
            f"PROJECT_ID={self.project_id}",
            f"COMMIT_SHA={controller_source_sha}",
        ]
        if not isinstance(steps, list) or len(steps) != 1 \
                or not isinstance(steps[0], dict) \
                or steps[0].get("env") != expected_executed_env \
                or any(
                    steps[0].get(field) != trigger_steps[0].get(field)
                    for field in execution_fields
                ):
            raise ControllerRejected(f"authenticated {gate} command is invalid")
        return {
            "gate": gate, "approver_role": approver_role,
            "build_id": build_id, "build_trigger_id": trigger_id,
            "trigger_name": trigger_name, "scope": dict(scope),
            "service_account": service_account, "approver_account": approver,
            "controller_source_sha": controller_source_sha,
            "approval_time": result.get("approvalTime"),
        }

    def _gate_receipt(self, args) -> dict[str, Any]:
        build_id = os.environ.get("BUILD_ID", "")
        if not re.fullmatch(r"[A-Za-z0-9-]{8,128}", build_id):
            raise ControllerRejected("current authenticated gate build ID is missing")
        payload = self.tools.describe_build(build_id)
        substitutions = payload.get("substitutions")
        if not isinstance(substitutions, dict):
            raise ControllerRejected("current gate substitutions are missing")
        _require_sha(str(substitutions.get("_CANDIDATE_SHA", "")), "gate candidate SHA")
        _require_digest(self.controller_digest, "controller digest")
        _account_set(args.approver_accounts, gate=args.gate)
        gate_key = (
            f"g1c-{args.gate.removeprefix('G1C_').lower()}-{args.approver_role}"
            if args.gate.startswith("G1C_")
            else f"{args.gate.lower().replace('_', '-')}-{args.approver_role}"
        )
        scope = {
            key: value for key, value in substitutions.items()
            if isinstance(key, str) and key.startswith("_") and isinstance(value, str)
        }
        normalized = self._validate_gate_build(
            payload, gate_key=gate_key, gate=args.gate,
            approver_role=args.approver_role, build_id=build_id,
            scope=scope, require_success=False,
        )
        return {
            "status": "approval-validated",
            "gate": args.gate, "build_id": build_id,
            "receipt_sha256": _sha256(_canonical_json(normalized)),
        }

    def _validate_evidence_document(
        self, evidence: Mapping[str, Any], *, main_sha: str,
        image_digest: str, controller_digest: str,
    ) -> dict[str, Any]:
        if set(evidence) != EVIDENCE_FIELDS:
            raise ControllerRejected("evidence manifest fields are invalid")
        _verify_manifest(evidence, "manifest_hash")
        if evidence.get("main_sha") != main_sha \
                or evidence.get("image_digest") != image_digest \
                or evidence.get("controller_builder_digest") != controller_digest:
            raise ControllerRejected("evidence manifest lineage mismatch")
        evidence_sha = _require_sha(
            str(evidence.get("evidence_sha", "")), "evidence SHA",
        )
        claims = evidence.get("artifact_claims")
        if not isinstance(claims, dict) or set(claims) != set(EVIDENCE_NAMES):
            raise ControllerRejected("evidence artifact claims incomplete")
        changed = self.source.changed_files(evidence_sha, main_sha)
        if any(
            not any(fnmatch.fnmatch(path, glob) for glob in DOCS_ONLY_GLOBS)
            for path in changed
        ):
            raise ControllerRejected("evidence SHA is not docs-only")
        self._checkout(evidence_sha)
        for name in EVIDENCE_NAMES:
            uri = _require_generation_uri(
                str(evidence.get(f"{name}_uri", "")), f"{name} URI",
            )
            raw = self.artifacts.read(uri)
            if _sha256(raw) != evidence.get(f"{name}_hash"):
                raise ControllerRejected(f"{name} evidence hash mismatch")
            try:
                artifact = json.loads(raw)
                normalized = validate_artifact(
                    name, artifact, main_sha=main_sha, image_digest=image_digest,
                )
            except (json.JSONDecodeError, ValueError) as exc:
                raise ControllerRejected(f"{name} evidence invalid: {exc}") from exc
            if normalized != claims[name]:
                raise ControllerRejected(f"{name} evidence claims mismatch")
        try:
            validate_semantic_review_bindings(evidence, claims)
        except ValueError as exc:
            raise ControllerRejected(f"semantic_review evidence invalid: {exc}") from exc
        return dict(claims)

    def _validate_promotion_document(
        self, promotion: Mapping[str, Any], *, main_sha: str,
        image_digest: str, controller_digest: str,
    ) -> None:
        if set(promotion) != PROMOTION_FIELDS:
            raise ControllerRejected("promotion fields are invalid")
        _verify_manifest(promotion, "attestation_hash")
        if promotion.get("main_sha") != main_sha \
                or promotion.get("image_digest") != image_digest \
                or promotion.get("controller_builder_digest") != controller_digest:
            raise ControllerRejected("promotion lineage mismatch")
        evidence_uri = _require_generation_uri(
            str(promotion.get("evidence_manifest_uri", "")),
            "promotion evidence URI",
        )
        raw = self.artifacts.read(evidence_uri)
        try:
            evidence = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ControllerRejected("promotion evidence manifest is not JSON") from exc
        if not isinstance(evidence, dict):
            raise ControllerRejected("promotion evidence manifest is invalid")
        if evidence.get("manifest_hash") != promotion.get("evidence_manifest_hash"):
            raise ControllerRejected("promotion evidence manifest hash mismatch")
        self._validate_evidence_document(
            evidence, main_sha=main_sha, image_digest=image_digest,
            controller_digest=str(promotion.get("evidence_controller_builder_digest", "")),
        )
        copied = {
            "evidence_sha", *EVIDENCE_COPY_FIELDS, "artifact_claims",
        }
        if any(promotion.get(field) != evidence.get(field) for field in copied):
            raise ControllerRejected("promotion does not copy exact evidence claims")

    def _evidence_manifest(self, args) -> dict[str, Any]:
        _require_sha(args.evidence_sha, "evidence SHA")
        _require_sha(args.main_sha, "main SHA")
        _require_digest(args.image_digest)
        changed = self.source.changed_files(args.evidence_sha, args.main_sha)
        for path in changed:
            if not any(fnmatch.fnmatch(path, glob) for glob in DOCS_ONLY_GLOBS):
                raise ControllerRejected(f"evidence branch contains code: {path}")
        candidate = self._checkout(args.evidence_sha)
        evidence_dir = candidate / "docs" / "verification" / "handle-ticket"
        try:
            inputs = json.loads((evidence_dir / "evidence-inputs.json").read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ControllerRejected("evidence-inputs.json missing or invalid") from exc
        if not isinstance(inputs, dict) or set(inputs) != set(EVIDENCE_NAMES):
            raise ControllerRejected("evidence input set is incomplete")
        controller_digest = _require_digest(
            args.controller_digest, "controller digest",
        )
        artifact_fields: dict[str, Any] = {}
        artifact_claims: dict[str, Any] = {}
        e2e_execution_id: Optional[str] = None
        for name in EVIDENCE_NAMES:
            uri = _require_generation_uri(str(inputs[name]), f"{name} URI")
            if name in {"e2e", "differential"}:
                match = re.fullmatch(
                    rf"gs://{re.escape(self.evidence_bucket)}/handle-ticket/e2e/"
                    rf"{re.escape(args.main_sha)}/([^/#]+)/{name}\.json#[1-9][0-9]*",
                    uri,
                )
                if match is None:
                    raise ControllerRejected(
                        f"{name} evidence is outside the candidate execution prefix"
                    )
                if e2e_execution_id is None:
                    e2e_execution_id = match.group(1)
                elif e2e_execution_id != match.group(1):
                    raise ControllerRejected(
                        "E2E and differential evidence executions differ"
                    )
            raw = self.artifacts.read(uri)
            try:
                artifact = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ControllerRejected(f"{name} evidence is not JSON") from exc
            try:
                claims = validate_artifact(
                    name, artifact, main_sha=args.main_sha,
                    image_digest=args.image_digest,
                )
            except ValueError as exc:
                raise ControllerRejected(f"{name} evidence invalid: {exc}") from exc
            artifact_fields[f"{name}_uri"] = uri
            artifact_fields[f"{name}_hash"] = _sha256(raw)
            artifact_claims[name] = claims
        try:
            validate_semantic_review_bindings(artifact_fields, artifact_claims)
        except ValueError as exc:
            raise ControllerRejected(f"semantic_review evidence invalid: {exc}") from exc
        evidence_inputs_hash = _sha256(_canonical_json({
            "artifact_fields": artifact_fields,
            "artifact_claims": artifact_claims,
        }))
        common_bindings = {
            "_CANDIDATE_SHA": args.main_sha,
            "_CONTROLLER_DIGEST": controller_digest,
            "_IMAGE_DIGEST": args.image_digest,
        }
        g4_scope = {
            **common_bindings,
            "_EVIDENCE_INPUTS_SHA256": evidence_inputs_hash,
        }
        required = [
            "g2-gcp-owner", "g4-requester", "g4-n8n-owner",
            "g4-participant-plan-owner", "g4-forusbots-owner",
            "g4-delivery-owner",
        ]
        receipt_hashes = self._validate_scoped_receipt_set(
            required, args.gate_receipts,
            {
                key: (None if key == "g2-gcp-owner" else g4_scope)
                for key in required
            },
            label="evidence publication", bindings=common_bindings,
        )
        body = _manifest({
            "evidence_sha": args.evidence_sha,
            "main_sha": args.main_sha,
            "image_digest": args.image_digest,
            "controller_builder_digest": controller_digest,
            **artifact_fields,
            "artifact_claims": artifact_claims,
        }, "manifest_hash")
        uri = self._write(
            f"evidence/{args.main_sha}/evidence_manifest.json",
            _canonical_json(body),
        )
        return {"evidence_manifest_uri": uri,
                "evidence_manifest_hash": body["manifest_hash"],
                "gate_receipt_hashes": receipt_hashes}

    def _staging_attest(self, args) -> dict[str, Any]:
        _require_sha(args.candidate_sha, "candidate SHA")
        _require_digest(args.image_digest)
        evidence_uri = self.artifacts.resolve(
            f"gs://{self.evidence_bucket}/evidence/{args.candidate_sha}/evidence_manifest.json"
        )
        evidence = self._load_json(evidence_uri, "evidence manifest")
        controller_digest = _require_digest(
            args.controller_digest, "controller digest",
        )
        claims = self._validate_evidence_document(
            evidence, main_sha=args.candidate_sha,
            image_digest=args.image_digest,
            controller_digest=controller_digest,
        )
        artifact_fields = {
            field: evidence[field] for field in EVIDENCE_COPY_FIELDS
        }
        evidence_inputs_hash = _sha256(_canonical_json({
            "artifact_fields": artifact_fields,
            "artifact_claims": claims,
        }))
        g5_scope = {
            "_CANDIDATE_SHA": args.candidate_sha,
            "_CONTROLLER_DIGEST": controller_digest,
            "_IMAGE_DIGEST": args.image_digest,
            "_EVIDENCE_INPUTS_SHA256": evidence_inputs_hash,
            "_EVIDENCE_MANIFEST_URI": evidence_uri,
            "_EVIDENCE_MANIFEST_SHA256": evidence["manifest_hash"],
        }
        receipt_hashes = self._validate_scoped_receipt_set(
            ["g5-maintainer", "g5-requester"], args.gate_receipts,
            {key: g5_scope for key in ("g5-maintainer", "g5-requester")},
            label="staging attestation",
        )
        promotion = _manifest({
            "main_sha": args.candidate_sha,
            "image_digest": args.image_digest,
            "evidence_manifest_uri": evidence_uri,
            "evidence_manifest_hash": evidence["manifest_hash"],
            "evidence_controller_builder_digest": controller_digest,
            "controller_builder_digest": controller_digest,
            "evidence_sha": evidence["evidence_sha"],
            **{
                field: evidence[field]
                for name in EVIDENCE_NAMES
                for field in (f"{name}_uri", f"{name}_hash")
            },
            "artifact_claims": claims,
        }, "attestation_hash")
        uri = self._write(
            f"promotions/{args.candidate_sha}/{uuid.uuid4().hex}/promotion.json",
            _canonical_json(promotion),
        )
        return {"promotion_uri": uri,
                "promotion_hash": promotion["attestation_hash"],
                "gate_receipt_hashes": receipt_hashes}

    @staticmethod
    def _observed_staging_services(
        observation: Any, *, image_digest: str,
    ) -> tuple[dict[str, dict[str, Any]], str]:
        expected_names = {
            "producer": "kb-rag-system-staging",
            "worker": "kb-rag-ticket-worker-staging",
            "reconciler": "ticket-reconciler-staging",
        }
        if not isinstance(observation, dict) or set(observation) != set(expected_names):
            raise ControllerRejected("staging observation inventory is not exact")
        services: dict[str, dict[str, Any]] = {}
        producer: dict[str, Any] = {}
        for role, expected_name in expected_names.items():
            item = observation.get(role)
            required = {
                "name", "revision", "image_digest", "ready", "handler_mode", "traffic",
            }
            if not isinstance(item, dict) or set(item) != required \
                    or item.get("name") != expected_name:
                raise ControllerRejected("observed staging resource identity differs")
            revision = item.get("revision")
            if not isinstance(revision, str) or not revision \
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", revision):
                raise ControllerRejected("observed staging revision is invalid")
            if item.get("image_digest") != image_digest:
                raise ControllerRejected("observed staging image digest differs")
            if item.get("ready") is not True:
                raise ControllerRejected("observed staging resource is not ready")
            if item.get("handler_mode") not in {"disabled", "shadow"}:
                raise ControllerRejected("observed staging handler mode is unsafe")
            if not isinstance(item.get("traffic"), list):
                raise ControllerRejected("observed staging traffic is invalid")
            services[role] = {
                "revision": revision, "image_digest": image_digest, "ready": True,
            }
            if role == "producer":
                producer = item
        handler_mode = producer["handler_mode"]
        if handler_mode == "shadow":
            release_phase = "shadow"
        else:
            candidate_traffic = [
                target for target in producer["traffic"]
                if isinstance(target, dict) and target.get("tag") == "candidate"
            ]
            release_phase = (
                "dark_no_traffic"
                if len(candidate_traffic) == 1
                and candidate_traffic[0].get("percent") == 0
                else "disabled"
            )
        return services, release_phase

    def _staging_observe(self, args) -> dict[str, Any]:
        candidate_sha = _require_sha(args.candidate_sha, "candidate SHA")
        image_digest = _require_digest(args.image_digest)
        build_id = os.environ.get("BUILD_ID", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", build_id):
            raise ControllerRejected("authenticated observation BUILD_ID is invalid")
        services, release_phase = self._observed_staging_services(
            self.tools.observe_staging(), image_digest=image_digest,
        )
        document = {
            "schema_version": "1.0", "artifact_type": "staging_revisions",
            "status": "pass", "main_sha": candidate_sha,
            "image_digest": image_digest,
            "result": {"release_phase": release_phase, "services": services},
        }
        try:
            validate_artifact(
                "staging_revisions", document, main_sha=candidate_sha,
                image_digest=image_digest,
            )
        except ValueError as exc:
            raise ControllerRejected(f"staging observation evidence invalid: {exc}") from exc
        uri = self._write(
            f"staging-observations/{candidate_sha}/{build_id}/staging_revisions.json",
            _canonical_json(document),
        )
        return {"status": "observed", "staging_revisions_uri": uri,
                "image_digest": image_digest}

    def _rollback_poll_observation(
        self, uri: str, *, phase: str, candidate_sha: str, image_digest: str,
    ) -> dict[str, Any]:
        _require_generation_uri(uri, f"rollback poll {phase} URI")
        pattern = re.compile(
            rf"^gs://{re.escape(self.evidence_bucket)}/handle-ticket/e2e/"
            rf"{re.escape(candidate_sha)}/([^/#]+)/rollback-{phase}\.json#[1-9][0-9]*$"
        )
        match = pattern.fullmatch(uri)
        if match is None:
            raise ControllerRejected("rollback poll evidence is outside trusted E2E prefix")
        document = self._load_json(uri, f"rollback poll {phase}")
        required = {
            "schema_version", "artifact_type", "status", "main_sha",
            "candidate_image_digest", "execution", "phase", "job_id_sha256",
            "terminal_state", "http_status",
        }
        if set(document) != required \
                or document.get("schema_version") != "1.0" \
                or document.get("artifact_type") != "rollback_poll_observation" \
                or document.get("status") != "pass" \
                or document.get("main_sha") != candidate_sha \
                or document.get("candidate_image_digest") != image_digest \
                or document.get("execution") != match.group(1) \
                or document.get("phase") != phase \
                or HASH_RE.fullmatch(str(document.get("job_id_sha256", ""))) is None \
                or document.get("terminal_state") not in {
                    "completed", "succeeded", "partial", "failed", "timeout", "cancelled",
                } \
                or document.get("http_status") != 200:
            raise ControllerRejected("rollback poll evidence is invalid")
        return document

    def _rollback_observe(self, args) -> dict[str, Any]:
        candidate_sha = _require_sha(args.candidate_sha, "candidate SHA")
        image_digest = _require_digest(args.image_digest)
        baseline_digest = _require_digest(
            args.baseline_image_digest, "baseline image digest",
        )
        if baseline_digest == image_digest:
            raise ControllerRejected("rollback baseline may not equal candidate digest")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", args.baseline_revision):
            raise ControllerRejected("rollback baseline revision is invalid")
        before = self._rollback_poll_observation(
            args.poll_before_uri, phase="before", candidate_sha=candidate_sha,
            image_digest=image_digest,
        )
        after = self._rollback_poll_observation(
            args.poll_after_uri, phase="after", candidate_sha=candidate_sha,
            image_digest=image_digest,
        )
        if before["execution"] != after["execution"] \
                or before["job_id_sha256"] != after["job_id_sha256"] \
                or before["terminal_state"] != after["terminal_state"]:
            raise ControllerRejected("rollback polling does not preserve the same job")
        observation = self.tools.observe_staging()
        if not isinstance(observation, dict) or set(observation) != {
            "producer", "worker", "reconciler",
        }:
            raise ControllerRejected("rollback observation inventory is not exact")
        producer = observation.get("producer")
        if not isinstance(producer, dict) \
                or producer.get("name") != "kb-rag-system-staging" \
                or producer.get("ready") is not True \
                or producer.get("revision") != args.baseline_revision \
                or producer.get("image_digest") != baseline_digest:
            raise ControllerRejected("rollback did not restore the exact ready baseline")
        traffic = producer.get("traffic")
        if not isinstance(traffic, list):
            raise ControllerRejected("rollback traffic observation is invalid")
        candidate_targets = [
            item for item in traffic
            if isinstance(item, dict) and item.get("tag") == "candidate"
        ]
        baseline_targets = [
            item for item in traffic
            if isinstance(item, dict) and item.get("tag") == "baseline"
        ]
        if len(candidate_targets) != 1 or candidate_targets[0].get("percent") != 0 \
                or len(baseline_targets) != 1 \
                or baseline_targets[0].get("percent") != 100 \
                or baseline_targets[0].get("revision") != args.baseline_revision:
            raise ControllerRejected("rollback traffic did not restore baseline exclusively")
        document = {
            "schema_version": "1.0", "artifact_type": "rollback",
            "status": "pass", "main_sha": candidate_sha,
            "image_digest": image_digest,
            "result": {
                "exercise": before["execution"], "rollback_succeeded": True,
                "candidate_image_digest": image_digest,
                "candidate_traffic_percent": 0,
                "restored_revision": args.baseline_revision,
                "restored_image_digest": baseline_digest,
                "poll_preserved": True,
            },
        }
        try:
            validate_artifact(
                "rollback", document, main_sha=candidate_sha,
                image_digest=image_digest,
            )
        except ValueError as exc:
            raise ControllerRejected(f"rollback observation evidence invalid: {exc}") from exc
        build_id = os.environ.get("BUILD_ID", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", build_id):
            raise ControllerRejected("authenticated observation BUILD_ID is invalid")
        uri = self._write(
            f"rollback-observations/{candidate_sha}/{build_id}/rollback.json",
            _canonical_json(document),
        )
        return {"status": "observed", "rollback_uri": uri,
                "image_digest": image_digest}

    def _validate_trusted_runtime_inputs(self, kb: Path) -> None:
        required = {
            "requirements.lock": "requirements.lock",
            "requirements-dev.lock": "requirements-dev.lock",
            ".secrets.baseline": "reviewed.secrets.baseline",
        }
        for candidate_name, trusted_name in required.items():
            candidate_file = kb / candidate_name
            trusted_file = self.trusted_root / trusted_name
            if not candidate_file.is_file() or not trusted_file.is_file() \
                    or candidate_file.read_bytes() != trusted_file.read_bytes():
                raise ControllerRejected(
                    f"{candidate_name} differs from reviewed controller input"
                )
        for name in (
            "Dockerfile.runtime", "Dockerfile.runtime.dockerignore",
            "Dockerfile.ci", "Dockerfile.ci.dockerignore",
            "Dockerfile.e2e", "Dockerfile.e2e.dockerignore",
            "verify_secrets_baseline.py",
        ):
            if not (self.trusted_root / name).is_file():
                raise ControllerRejected(f"trusted controller input missing: {name}")

    def _run_isolated_ci(self, candidate_sha: str, kb: Path) -> str:
        self._validate_trusted_runtime_inputs(kb)
        tag = f"release-ci-local:{candidate_sha}"
        self.tools.run([
            "docker", "build", "--platform=linux/amd64",
            f"--file={self.trusted_root / 'Dockerfile.ci'}",
            f"--tag={tag}", ".",
        ], cwd=kb)
        trusted_verifier = (
            self.work_root / f"trusted-verify-secrets-{uuid.uuid4().hex}.py"
        )
        shutil.copyfile(
            self.trusted_root / "verify_secrets_baseline.py", trusted_verifier,
        )
        isolated = [
            "docker", "run", "--rm", "--network=none", "--read-only",
            "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--pids-limit=512", "--memory=4g", "--cpus=2",
            "--tmpfs=/tmp:rw,noexec,nosuid,size=1024m",
            "--user=1000:1000", "--env=HOME=/tmp",
            "--env=PYTHONDONTWRITEBYTECODE=1",
            "--env=PYTEST_ADDOPTS=-p no:cacheprovider",
            "--env=MYPY_CACHE_DIR=/tmp/mypy-cache",
            "--env=RUFF_CACHE_DIR=/tmp/rc",
            "--env=XDG_CACHE_HOME=/tmp/cache",
            "--env=PYTHONPATH=/nonexistent",
            "--env=GOOGLE_APPLICATION_CREDENTIALS=/nonexistent/google-adc.json",
            "--env=CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE=/nonexistent/google-adc.json",
            "--env=GCE_METADATA_HOST=127.0.0.1:9",
        ]
        commands = (
            [*isolated, "--entrypoint=python", tag, "-m", "pytest", "-q", "-rs",
             "-m", "not live_dependencies and not staging_e2e"],
            [*isolated, "--entrypoint=python", tag, "-m", "pytest", "--collect-only", "-q"],
            [*isolated, "--entrypoint=ruff", tag, "check", "."],
            [*isolated, "--entrypoint=mypy", tag],
            [*isolated, "--entrypoint=python", tag, "-m", "pip", "check"],
            [
             *isolated,
             f"--volume={trusted_verifier}:"
             "/opt/release-controller/verify_secrets_baseline.py:ro",
             "--entrypoint=/bin/sh", tag, "-c",
             "detect-secrets scan --all-files . --no-verify "
             "--exclude-files '\\.venv/.*' "
             "--exclude-files '\\.pytest_cache/.*' "
             "--exclude-files '\\.mypy_cache/.*' "
             "--exclude-files '\\.ruff_cache/.*' "
             "--exclude-files '^\\.secrets\\.baseline$' "
             "--exclude-files '__pycache__/.*' "
             "--exclude-files 'rag-testing/stress_test_results.*' "
             "> /tmp/candidate-secrets.baseline && "
             "python /opt/release-controller/verify_secrets_baseline.py "
             "--approved .secrets.baseline "
             "--candidate /tmp/candidate-secrets.baseline "
             "--scan-root ."],
            [
                "docker", "run", "--rm", "--read-only",
                "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--pids-limit=256", "--memory=2g", "--cpus=1",
                "--tmpfs=/tmp:rw,noexec,nosuid,size=512m",
                "--user=1000:1000", "--env=HOME=/tmp",
                "--env=XDG_CACHE_HOME=/tmp/cache",
                "--env=GOOGLE_APPLICATION_CREDENTIALS=/nonexistent/google-adc.json",
                "--env=CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE=/nonexistent/google-adc.json",
                "--env=GCE_METADATA_HOST=127.0.0.1:9",
                "--entrypoint=pip-audit", tag,
                "--strict", "--require-hashes", "-r", "requirements.lock",
            ],
        )
        for command in commands:
            self.tools.run(command)
        return tag

    def _run_terraform_ci(self, candidate: Path) -> None:
        controller_image = _require_digest(
            self.controller_digest, "controller digest",
        )
        source = candidate / "infra" / "terraform"
        sandbox = self.work_root / f"terraform-sandbox-{uuid.uuid4().hex}"
        shutil.copytree(source, sandbox)
        shutil.copyfile(
            self.trusted_root / "reviewed.terraform.lock.hcl",
            sandbox / "modules" / "ticket_environment" / ".terraform.lock.hcl",
        )
        base = [
            "docker", "run", "--rm", "--network=none", "--read-only",
            "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--tmpfs=/tmp:rw,noexec,nosuid,size=64m",
            "--env=GOOGLE_APPLICATION_CREDENTIALS=/nonexistent/google-adc.json",
            "--env=GCE_METADATA_HOST=127.0.0.1:9",
            "--env=CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE=/nonexistent/google-adc.json",
            "--env=TF_CLI_CONFIG_FILE=/opt/release-controller/terraform-cli.tfrc",
            "--env=CHECKPOINT_DISABLE=1",
            f"--volume={sandbox}:/workspace/terraform:rw",
            "--entrypoint=terraform", controller_image,
        ]
        self.tools.run([
            *base, "-chdir=/workspace/terraform", "fmt", "-check", "-recursive",
        ])
        roots = [
            *(f"live/{environment}" for environment in ENVIRONMENTS),
            "modules/ticket_environment",
        ]
        for relative in roots:
            work = f"/workspace/terraform/{relative}"
            self.tools.run([
                *base, f"-chdir={work}", "init", "-backend=false",
                "-input=false", "-lockfile=readonly", "-no-color",
            ])
            self.tools.run([
                *base, f"-chdir={work}", "validate", "-no-color",
            ])
            if (sandbox / relative / "tests").is_dir():
                self.tools.run([
                    *base, f"-chdir={work}", "test",
                    "-test-directory=tests", "-no-color",
                ])

    def _runtime_image(self, args) -> dict[str, Any]:
        candidate = self._checkout(args.candidate_sha)
        kb = candidate / "kb-rag-system"
        self._run_terraform_ci(candidate)
        self._run_isolated_ci(args.candidate_sha, kb)
        build_id = re.sub(
            r"[^A-Za-z0-9_.-]", "-", os.environ.get("BUILD_ID", uuid.uuid4().hex),
        )
        tag = f"{self.runtime_image}:{args.candidate_sha}-{build_id}"
        self.tools.run([
            "docker", "build", "--platform=linux/amd64",
            f"--file={self.trusted_root / 'Dockerfile.runtime'}",
            f"--tag={tag}", ".",
        ], cwd=kb)
        self.tools.run(["docker", "push", tag], cwd=kb)
        described = self.tools.describe_image(tag)
        digest = described.get("digest")
        if not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
            raise ControllerRejected("runtime tag did not resolve to one immutable digest")
        immutable = f"{self.runtime_image}@{digest}"
        smoke_path = self.work_root / "trusted-container-smoke.py"
        shutil.copyfile(
            self.trusted_root / "container_smoke.py", smoke_path,
        )
        self.tools.run([
            "docker", "run", "--rm", "--network=none",
            "--workdir=/app", "--env=PYTHONPATH=/app:/opt/python",
            f"--volume={smoke_path}:/opt/container-smoke.py:ro",
            "--entrypoint=python", immutable, "/opt/container-smoke.py",
        ])
        sbom = self.tools.sbom(immutable)
        packages = sbom.get("packages")
        if sbom.get("spdxVersion") != "SPDX-2.3" \
                or not isinstance(packages, list) or not packages:
            raise ControllerRejected("runtime SBOM is not nonempty SPDX 2.3")
        scan = self.tools.verify_scan(immutable)
        if scan.get("status") != "passed" or scan.get("critical") or scan.get("high"):
            raise ControllerRejected("runtime image scan did not pass")
        sbom_document_uri = self._write(
            f"runtime/{args.candidate_sha}/{build_id}/sbom.spdx.json",
            _canonical_json(sbom),
        )
        scan_report_uri = self._write(
            f"runtime/{args.candidate_sha}/{build_id}/scan_report.json",
            _canonical_json(scan),
        )
        documents = {
            "sbom": {
                "format": "spdx-json",
                "document_namespace": str(
                    sbom.get("documentNamespace") or f"urn:sha256:{_sha256(_canonical_json(sbom))}"
                ),
                "subject_digest": immutable,
                "package_count": len(packages),
            },
            "scan": {
                "policy_passed": True,
                "subject_digest": immutable,
                "severity_counts": {
                    "CRITICAL": int(scan.get("critical", 0)),
                    "HIGH": int(scan.get("high", 0)),
                },
                "high_approvals": [],
            },
        }
        artifact_uris: dict[str, str] = {}
        for name, result in documents.items():
            document = {
                "schema_version": "1.0", "artifact_type": name,
                "status": "pass", "main_sha": args.candidate_sha,
                "image_digest": immutable, "result": result,
            }
            artifact_uris[f"{name}_uri"] = self._write(
                f"runtime/{args.candidate_sha}/{build_id}/{name}.json",
                _canonical_json(document),
            )
        return {
            "status": "published", "main_sha": args.candidate_sha,
            "image_digest": immutable, "sbom_document_uri": sbom_document_uri,
            "scan_report_uri": scan_report_uri, "source_build_id": build_id,
            **artifact_uris,
        }

    def _runtime_attest(self, args) -> dict[str, Any]:
        candidate_sha = _require_sha(args.candidate_sha, "candidate SHA")
        image_digest = _require_digest(args.image_digest)
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", args.source_build_id):
            raise ControllerRejected("source build ID is invalid")
        provenance = self.tools.verify_provenance(
            image_digest, candidate_sha, args.source_build_id,
        )
        if provenance.get("provenance_verified") is not True \
                or provenance.get("source_commit") != candidate_sha \
                or provenance.get("subject_digest") != image_digest \
                or provenance.get("build_id") != args.source_build_id:
            raise ControllerRejected("authenticated finalized runtime provenance is invalid")
        scan = self.tools.verify_scan(image_digest)
        critical = int(scan.get("critical", 0))
        high = int(scan.get("high", 0))
        receipt_hashes: dict[str, str] = {}
        if critical:
            raise ControllerRejected("finalized runtime scan contains CRITICAL findings")
        if high:
            high_ids = scan.get("high_ids")
            report_hash = scan.get("scan_report_sha256")
            if high != 1 or not isinstance(high_ids, list) or len(high_ids) != 1 \
                    or re.fullmatch(r"CVE-[0-9]{4}-[0-9]{4,}", high_ids[0]) is None \
                    or not isinstance(report_hash, str) or HASH_RE.fullmatch(report_hash) is None:
                raise ControllerRejected(
                    "HIGH exception requires one exact CVE and immutable scan report"
                )
            scope = {
                "_CANDIDATE_SHA": candidate_sha,
                "_CONTROLLER_DIGEST": _require_digest(
                    self.controller_digest, "controller digest",
                ),
                "_IMAGE_DIGEST": image_digest,
                "_VULNERABILITY_ID": high_ids[0],
                "_SCAN_REPORT_SHA256": report_hash,
            }
            required = [
                "g5v-security-owner", "g5v-release-owner", "g5v-requester",
            ]
            receipt_hashes = self._validate_scoped_receipt_set(
                required, args.gate_receipts,
                {key: scope for key in required}, label="G5V exception",
            )
        elif args.gate_receipts:
            raise ControllerRejected("G5V receipts are forbidden without a HIGH finding")
        document = {
            "schema_version": "1.0", "artifact_type": "ci_provenance",
            "status": "pass", "main_sha": candidate_sha,
            "image_digest": image_digest,
            "result": {**provenance, "g5v_receipt_hashes": receipt_hashes},
        }
        uri = self._write(
            f"runtime/{candidate_sha}/{args.source_build_id}/ci_provenance.json",
            _canonical_json(document),
        )
        return {"status": "attested", "ci_provenance_uri": uri,
                "image_digest": image_digest, "source_build_id": args.source_build_id}

    def _test_only(self, args) -> dict[str, Any]:
        candidate = self._checkout(args.candidate_sha)
        digest = _require_digest(args.image_digest)
        kb = candidate / "kb-rag-system"
        self._run_isolated_ci(args.candidate_sha, kb)
        described = self.tools.describe_image(digest)
        if described.get("digest") != digest.split("@", 1)[1]:
            raise ControllerRejected("existing image resolved to another digest")
        scan = self.tools.verify_scan(digest)
        if scan.get("status") != "passed" or scan.get("critical") or scan.get("high"):
            raise ControllerRejected("existing image scan did not pass")
        return {"status": "verified", "candidate_sha": args.candidate_sha,
                "image_digest": digest}

    @staticmethod
    def _validate_e2e_context(kb: Path) -> None:
        for relative in E2E_REQUIRED:
            required = kb / relative
            if not required.exists():
                raise ControllerRejected(f"E2E context missing {relative}")
            if required.is_symlink():
                raise ControllerRejected(f"E2E context symlink rejected: {relative}")
        ignore = (kb / "Dockerfile.e2e.dockerignore").read_text(encoding="utf-8")
        lines = [line.strip() for line in ignore.splitlines()
                 if line.strip() and not line.lstrip().startswith("#")]
        if not lines or lines[0] != "*":
            raise ControllerRejected("E2E dockerignore must exclude all by default")
        allowed_includes = {
            "!Dockerfile.e2e", "!requirements.lock", "!requirements-dev.lock",
            "!pytest.ini", "!pyproject.toml", "!api/**", "!data_pipeline/**",
            "!scripts/**", "!tests/**",
            "!rag-testing/ticket_differential.py",
            "!rag-testing/ticket_differential_thresholds.json",
        }
        includes = {line for line in lines if line.startswith("!")}
        if includes != allowed_includes:
            raise ControllerRejected("E2E dockerignore allowlist differs from policy")
        for relative in ("api", "data_pipeline", "scripts", "tests"):
            list(_safe_relative_files(kb / relative))

    def _e2e_image(self, args) -> dict[str, Any]:
        candidate = self._checkout(args.candidate_sha)
        kb = candidate / "kb-rag-system"
        self._validate_e2e_context(kb)
        self._validate_trusted_runtime_inputs(kb)
        build_id = re.sub(
            r"[^A-Za-z0-9_.-]", "-", os.environ.get("BUILD_ID", uuid.uuid4().hex),
        )
        tag = f"{self.e2e_image}:{args.candidate_sha}-{build_id}"
        self.tools.run([
            "docker", "build", "--platform=linux/amd64",
            f"--file={self.trusted_root / 'Dockerfile.e2e'}",
            f"--tag={tag}", ".",
        ], cwd=kb)
        self.tools.run(["docker", "push", tag], cwd=kb)
        described = self.tools.describe_image(tag)
        digest = described.get("digest")
        if not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
            raise ControllerRejected("E2E tag did not resolve to one immutable digest")
        immutable = f"{self.e2e_image}@{digest}"
        self.tools.run([
            "docker", "run", "--rm", "--network=none",
            "--workdir=/app", "--env=PYTHONPATH=/app",
            "--entrypoint", "python", immutable,
            "scripts/container_smoke.py",
        ])
        sbom = self.tools.sbom(immutable)
        if sbom.get("spdxVersion") != "SPDX-2.3":
            raise ControllerRejected("E2E SBOM is not SPDX 2.3")
        scan = self.tools.verify_scan(immutable)
        if scan.get("status") != "passed" or scan.get("critical") or scan.get("high"):
            raise ControllerRejected("E2E image scan did not pass")
        body = _manifest({
            "schema_version": 1,
            "artifact_type": "e2e_image",
            "status": "passed",
            "main_sha": args.candidate_sha,
            "image_digest": immutable,
            "sbom_sha256": _sha256(_canonical_json(sbom)),
            "scan_policy_sha256": _sha256(_canonical_json(scan)),
            "scan_severity_counts": {
                "CRITICAL": int(scan.get("critical", 0)),
                "HIGH": int(scan.get("high", 0)),
            },
        }, "manifest_hash")
        uri = self._write(
            f"e2e-images/{args.candidate_sha}/e2e_image_manifest.json",
            _canonical_json(body),
        )
        return {"e2e_image_manifest_uri": uri,
                "e2e_image_manifest_hash": body["manifest_hash"],
                "image_digest": immutable}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="release-controller")
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan")
    plan.add_argument("environment", choices=ENVIRONMENTS)
    plan.add_argument("--candidate-sha", required=True)
    plan.add_argument("--firestore-scope-phase", default="disabled")
    plan.add_argument("--image-digest", default="")
    plan.add_argument("--release-phase", default="infra_only")
    plan.add_argument("--promotion-uri", default="")
    plan.add_argument("--platform-outputs-uri", default="")
    plan.add_argument("--environment-tfvars-uri", default="")
    plan.add_argument("--controller-digest", default="")
    plan.add_argument(
        "--staging-handoff-phase", choices=("disabled", "bootstrap", "managed"),
        default="disabled",
    )
    plan.add_argument(
        "--production-handoff-phase", choices=("disabled", "bootstrap", "managed"),
        default="disabled",
    )
    plan.add_argument("--staging-run-resources", default="")
    plan.add_argument("--production-run-resources", default="")
    plan.add_argument(
        "--staging-container-phase", choices=("disabled", "managed"),
        default="disabled",
    )
    plan.add_argument(
        "--production-container-phase", choices=("disabled", "managed"),
        default="disabled",
    )
    plan.add_argument(
        "--staging-release-phase", choices=("disabled", *RELEASE_PHASES),
        default="disabled",
    )
    plan.add_argument(
        "--production-release-phase", choices=("disabled", *RELEASE_PHASES),
        default="disabled",
    )
    plan.add_argument("--staging-environment-tfvars-uri", default="")
    plan.add_argument("--production-environment-tfvars-uri", default="")
    plan.add_argument("--staging-existing-secret-ids", default="")
    plan.add_argument("--production-existing-secret-ids", default="")
    plan.add_argument("--staging-approved-image-digest", default="")
    plan.add_argument("--production-approved-image-digest", default="")
    plan.add_argument("--cicd-bootstrap-controller-digest", default="")
    plan.add_argument("--gate-approver-accounts-json", default="{}")
    plan.add_argument("--production-release-group-email", default="")
    plan.add_argument(
        "--enable-legacy-trigger-neutralization",
        choices=("false", "true"), default="false",
    )

    apply = commands.add_parser("apply")
    apply.add_argument("environment", choices=ENVIRONMENTS)
    apply.add_argument("--plan-uri", required=True)
    apply.add_argument("--plan-sha256", required=True)
    apply.add_argument("--promotion-uri", default="")
    apply.add_argument("--controller-digest", default="")
    apply.add_argument("--gate-receipts", default="")
    apply.add_argument("--prepare-smoke-uri", default="")

    attest = commands.add_parser("staging-attest")
    attest.add_argument("--candidate-sha", required=True)
    attest.add_argument("--image-digest", required=True)
    attest.add_argument("--controller-digest", required=True)
    attest.add_argument("--gate-receipts", default="")

    evidence = commands.add_parser("evidence-manifest")
    evidence.add_argument("--evidence-sha", required=True)
    evidence.add_argument("--main-sha", required=True)
    evidence.add_argument("--image-digest", required=True)
    evidence.add_argument("--controller-digest", required=True)
    evidence.add_argument("--gate-receipts", default="")

    test = commands.add_parser("test-only")
    test.add_argument("--candidate-sha", required=True)
    test.add_argument("--image-digest", required=True)

    e2e = commands.add_parser("e2e-image")
    e2e.add_argument("--candidate-sha", required=True)

    runtime = commands.add_parser("runtime-image")
    runtime.add_argument("--candidate-sha", required=True)
    runtime_attest = commands.add_parser("runtime-attest")
    runtime_attest.add_argument("--candidate-sha", required=True)
    runtime_attest.add_argument("--image-digest", required=True)
    runtime_attest.add_argument("--source-build-id", required=True)
    runtime_attest.add_argument("--gate-receipts", default="")
    observe = commands.add_parser("staging-observe")
    observe.add_argument("--candidate-sha", required=True)
    observe.add_argument("--image-digest", required=True)
    rollback = commands.add_parser("rollback-observe")
    rollback.add_argument("--candidate-sha", required=True)
    rollback.add_argument("--image-digest", required=True)
    rollback.add_argument("--baseline-revision", required=True)
    rollback.add_argument("--baseline-image-digest", required=True)
    rollback.add_argument("--poll-before-uri", required=True)
    rollback.add_argument("--poll-after-uri", required=True)
    gate = commands.add_parser("gate-receipt")
    gate.add_argument(
        "--gate", required=True,
        choices=(
            "G1B", "G2", "G4", "G5", "G5V", "G6B",
            "G1C_PREPARE", "G1C_ENFORCE",
        ),
    )
    gate.add_argument("--approver-role", required=True)
    gate.add_argument("--approver-accounts", required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(argv or sys.argv[1:])
    if "--help" in arguments or "-h" in arguments:
        build_parser().parse_args(arguments)
    tools = ProductionToolchain()
    repository = TRUSTED_REPOSITORY
    work_root = Path(os.environ.get("RELEASE_WORK_ROOT", "/workspace/controller"))
    bucket = os.environ.get(
        "RELEASE_EVIDENCE_BUCKET", "rag-kb-system-ticket-evidence-900340137010",
    )
    project = os.environ.get("PROJECT_ID", "")
    if project != TRUSTED_PROJECT_ID:
        print("RELEASE REJECTED: PROJECT_ID is not the trusted project", file=sys.stderr)
        return 2
    controller = ReleaseController(
        source=GitCandidateSource(repository, tools),
        artifacts=GCSArtifactStore(tools),
        tools=tools,
        work_root=work_root,
        evidence_bucket=bucket,
        e2e_image=f"us-central1-docker.pkg.dev/{project}/kb-rag/kb-rag-e2e",
        runtime_image=(
            f"us-central1-docker.pkg.dev/{project}/kb-rag/kb-rag-system"
        ),
        project_id=project,
        controller_digest=os.environ.get("RELEASE_CONTROLLER_DIGEST", ""),
    )
    try:
        result = controller.execute(arguments)
    except (ControllerRejected, subprocess.CalledProcessError, OSError) as exc:
        print(f"RELEASE REJECTED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
