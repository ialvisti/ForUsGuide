"""Fail-closed contracts for the Terraform Cloud Run runtime topology.

These tests deliberately inspect the deployable HCL.  They protect the
production import from silently dropping core configuration or routing a new
revision before its explicit promotion gate.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = REPO_ROOT / "infra" / "terraform" / "modules" / "ticket_environment"
PRODUCTION_ROOT = REPO_ROOT / "infra" / "terraform" / "live" / "production"
STAGING_ROOT = REPO_ROOT / "infra" / "terraform" / "live" / "staging"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_production_requires_the_observed_core_and_secret_inputs() -> None:
    variables = _read(PRODUCTION_ROOT / "variables.tf")
    main = _read(PRODUCTION_ROOT / "main.tf")

    assert 'variable "producer_core_env"' in variables
    assert 'variable "secret_version_refs"' in variables
    for name in (
        "ENABLE_EXECUTION_LOGGING",
        "FORUSBOTS_BASE_URL",
        "GCS_BUCKET",
        "INDEX_NAME",
        "LLM_ROUTE_CLASSIFY",
        "LLM_ROUTE_DECOMPOSE",
        "LLM_ROUTE_GR_OUTCOME",
        "LLM_ROUTE_GR_RESPONSE",
        "LLM_ROUTE_KNOWLEDGE",
        "LLM_ROUTE_REQUIRED_DATA",
        "LLM_ROUTE_EXTRACT_INQUIRIES",
        "LLM_ROUTE_KB_QUESTION_SYNTHESIS",
        "LLM_ROUTE_FORUSBOTS_FIELD_MAP",
        "LLM_ROUTE_GR_BODY_BUILD",
        "LLM_ROUTE_TICKET_FIELD_EXTRACT",
        "LOG_LEVEL",
        "NAMESPACE",
        "OPENAI_MODEL",
        "OPENAI_REASONING_EFFORT",
        "TICKET_LLM_PRICING_JSON",
        "USE_VERTEX_AI",
    ):
        assert name in variables, f"missing observable core env contract: {name}"
    for name in ("API_KEY", "FORUSBOTS_AUTH_TOKEN", "OPENAI_API_KEY", "PINECONE_API_KEY"):
        assert name in variables, f"missing numeric secret-version contract: {name}"

    assert re.search(r"producer_core_env\s*=\s*var\.producer_core_env", main)
    assert re.search(r"secret_version_refs\s*=\s*var\.secret_version_refs", main)


def test_producer_traffic_is_explicit_for_dark_and_promoted_phases() -> None:
    cloud_run = _read(MODULE_ROOT / "cloud_run.tf")
    variables = _read(MODULE_ROOT / "variables.tf")
    production = _read(PRODUCTION_ROOT / "main.tf")

    assert 'variable "producer_baseline_revision"' in variables
    assert 'var.release_phase == "dark_no_traffic"' in cloud_run
    assert 'type     = "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION"' in cloud_run
    assert "revision = var.producer_baseline_revision" in cloud_run
    assert re.search(r"percent\s*=\s*100", cloud_run)
    assert 'type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"' in cloud_run
    assert re.search(r"percent\s*=\s*0", cloud_run)
    assert 'var.release_phase != "dark_no_traffic"' in cloud_run
    assert re.search(
        r"producer_baseline_revision\s*=\s*var\.producer_baseline_revision",
        production,
    )


def test_producer_models_the_observed_runtime_shape() -> None:
    cloud_run = _read(MODULE_ROOT / "cloud_run.tf")
    variables = _read(MODULE_ROOT / "variables.tf")

    for variable in (
        "producer_core_env",
        "producer_ingress",
        "producer_max_instances",
        "producer_concurrency",
        "producer_timeout",
        "producer_cpu",
        "producer_memory",
        "producer_port",
        "producer_startup_probe",
        "producer_liveness_probe",
    ):
        assert f'variable "{variable}"' in variables

    assert re.search(r"ingress\s*=\s*var\.producer_ingress", cloud_run)
    assert re.search(
        r"max_instance_request_concurrency\s*=\s*var\.producer_concurrency",
        cloud_run,
    )
    assert re.search(r"timeout\s*=\s*var\.producer_timeout", cloud_run)
    assert re.search(
        r"max_instance_count\s*=\s*var\.producer_max_instances",
        cloud_run,
    )
    assert "for_each = var.producer_core_env" in cloud_run
    assert re.search(r"dynamic\s+\"startup_probe\"", cloud_run)
    assert re.search(r"dynamic\s+\"liveness_probe\"", cloud_run)
    assert "container_port = var.producer_port" in cloud_run
    assert re.search(r"service_account\s*=\s*var\.producer_sa_email", cloud_run)


def test_production_import_preserves_revision_00048_as_rollback_only() -> None:
    variables = _read(PRODUCTION_ROOT / "variables.tf")
    main = _read(PRODUCTION_ROOT / "main.tf")

    expected_defaults = {
        "producer_baseline_revision": '"kb-rag-system-00048-bkc"',
        "producer_ingress": '"INGRESS_TRAFFIC_ALL"',
        "producer_max_instances": "5",
        "producer_concurrency": "80",
        "producer_timeout": '"300s"',
        "producer_cpu": '"1"',
        "producer_memory": '"512Mi"',
        "producer_startup_cpu_boost": "true",
        "producer_port": "8000",
    }
    for name, expected in expected_defaults.items():
        block = variables.split(f'variable "{name}"', 1)[1].split("\n}", 1)[0]
        assert re.search(rf"default\s*=\s*{re.escape(expected)}", block)

    probe = variables.split('variable "producer_startup_probe"', 1)[1].split(
        '\nvariable "producer_liveness_probe"', 1
    )[0]
    for expected in (
        "timeout_seconds       = 240",
        "period_seconds        = 240",
        "failure_threshold     = 1",
        "tcp_socket_port       = 8000",
    ):
        assert expected in probe
    liveness = variables.split('variable "producer_liveness_probe"', 1)[1].split(
        '\nvariable "', 1
    )[0]
    assert "default  = null" in liveness

    assert re.search(
        r'producer_sa_email\s*=\s*local\.sas\["ticket-producer-prod"\]',
        main,
    )
    assert (
        'producer_sa_email    = "kb-rag-runner@${var.project_id}.'
        'iam.gserviceaccount.com"' not in main
    )


def test_production_candidate_has_a_dedicated_fail_closed_identity() -> None:
    platform_main = _read(
        REPO_ROOT / "infra/terraform/live/platform/main.tf"
    )
    platform_iam = _read(
        REPO_ROOT / "infra/terraform/live/platform/runtime_project_iam.tf"
    )
    platform_imports = _read(
        REPO_ROOT / "infra/terraform/live/platform/imports.tf"
    )
    production = _read(PRODUCTION_ROOT / "main.tf")
    production_variables = _read(PRODUCTION_ROOT / "variables.tf")
    cloud_run = _read(MODULE_ROOT / "cloud_run.tf")

    assert re.search(
        r'"ticket-producer-prod"\s*=\s*"Producer runtime \(production\)"',
        platform_main,
    )
    assert '"ticket-producer-prod"' in production_variables
    assert 'local.sas["ticket-producer-prod"]' in production

    for inventory in (
        "runtime_firestore_grant_inventory",
        "runtime_vertex_grant_inventory",
        "environment_runtime_iam_inventory",
    ):
        block = platform_iam.split(f"{inventory} =", 1)[1].split("\n  }", 1)[0]
        assert 'google_service_account.runtime["ticket-producer-prod"].email' in block

    assert 'resource "google_project_iam_member" "runtime_telemetry"' in platform_iam
    assert '"roles/logging.logWriter"' in platform_iam
    assert '"roles/monitoring.metricWriter"' in platform_iam

    # The legacy identity remains available to the immutable rollback revision,
    # but this tree neither adopts nor retires its non-Firestore core grants.
    assert "production-producer = \"rag-kb-system roles/aiplatform.user" not in (
        platform_imports
    )
    assert "kb_rag_runner_firestore_legacy" in platform_imports

    assert "production_producer_identity_is_dedicated" in cloud_run
    assert (
        '"ticket-producer-prod@${var.project_id}.iam.gserviceaccount.com"'
        in cloud_run
    )

    producer = cloud_run.split(
        'resource "google_cloud_run_v2_service" "producer"', 1
    )[1].split('resource "google_cloud_run_v2_service" "worker"', 1)[0]
    assert "condition     = local.production_producer_identity_is_dedicated" in producer
    assert not re.search(
        r'ticket_handler_mode\s*==\s*"disabled"\s*\|\|\s*'
        r"local\.production_producer_identity_is_dedicated",
        producer,
    )
    assert "google_secret_manager_secret_iam_member.runtime_accessor" in producer
    assert "google_storage_bucket_iam_member.producer_core_objects" in producer


def test_worker_receives_the_runtime_configuration_it_validates() -> None:
    """El worker inicializa RAG/LLM/ForusBots. Sin el mapa core heredaría el
    FORUSBOTS_BASE_URL HTTP por defecto y fallaría al arrancar en producción."""
    cloud_run = _read(MODULE_ROOT / "cloud_run.tf")
    worker = cloud_run.split(
        'resource "google_cloud_run_v2_service" "worker"', 1
    )[1].split(
        'resource "google_cloud_run_v2_job" "reconciler"', 1
    )[0]

    assert "for_each = var.producer_core_env" in worker

    reconciler = cloud_run.split(
        'resource "google_cloud_run_v2_job" "reconciler"', 1
    )[1]
    assert 'name  = "TICKET_LLM_PRICING_JSON"' in reconciler
    assert 'lookup(var.producer_core_env, "TICKET_LLM_PRICING_JSON", "")' in reconciler


def test_runtime_invariants_do_not_use_execution_resources() -> None:
    terraform_text = "\n".join(_read(path) for path in MODULE_ROOT.glob("*.tf"))

    for forbidden in (
        'resource "null_resource"',
        'resource "terraform_data"',
        "local-exec",
        "remote-exec",
        "provisioner",
    ):
        assert forbidden not in terraform_text


def test_active_job_gauges_have_the_required_state_created_at_index() -> None:
    firestore = _read(MODULE_ROOT / "firestore.tf")
    block = firestore.split(
        'resource "google_firestore_index" "jobs_state_created_at"', 1
    )[1].split("\n}", 1)[0]

    assert 'collection = "ticket_jobs"' in block
    assert re.findall(r'field_path\s*=\s*"([^"]+)"', block) == [
        "state", "created_at",
    ]
    assert re.findall(r'order\s*=\s*"([^"]+)"', block) == [
        "ASCENDING", "ASCENDING",
    ]


def test_firestore_json_mirror_matches_all_terraform_indexes_and_ttls() -> None:
    firestore = _read(MODULE_ROOT / "firestore.tf")
    mirror = json.loads(_read(REPO_ROOT / "kb-rag-system" / "firestore.indexes.json"))

    expected_indexes = {
        (("principal_id", "ASCENDING"), ("state", "ASCENDING")),
        (("state", "ASCENDING"), ("lease_expires_at", "ASCENDING")),
        (("state", "ASCENDING"), ("created_at", "ASCENDING")),
        (("enqueue_state", "ASCENDING"), ("created_at", "ASCENDING")),
    }
    # The ticket handler and the /tickets review console share this canonical
    # file but live in different named databases, and only the handler's
    # resources are mirrored by this Terraform module (the console's are added
    # in the console's own infrastructure stage). Partition by collection group
    # so this test keeps detecting handler drift in both directions without
    # forbidding the console's declarations.
    handler_collections = {
        "ticket_jobs",
        "ticket_job_payloads",
        "ticket_idempotency_receipts",
        "ticket_rate_windows",
        "ticket_executions",
        "execution_logs",
    }
    console_collections = {
        "ticket_reviews",
        "ticket_console_cache",
        "devrev_message_cache",
        "ticket_import_staging",
        "idempotency_keys",
    }
    declared_collections = {index["collectionGroup"] for index in mirror["indexes"]} | {
        field["collectionGroup"] for field in mirror["fieldOverrides"]
    }
    assert declared_collections <= handler_collections | console_collections

    mirrored_indexes = {
        tuple((field["fieldPath"], field["order"]) for field in index["fields"])
        for index in mirror["indexes"]
        if index["collectionGroup"] in handler_collections
    }
    assert mirrored_indexes == expected_indexes

    expected_ttls = {
        ("ticket_job_payloads", "expires_at"),
        ("ticket_jobs", "expires_at"),
        ("ticket_idempotency_receipts", "expires_at"),
        ("ticket_rate_windows", "expires_at"),
        ("ticket_executions", "expires_at"),
        ("execution_logs", "expires_at"),
    }
    mirrored_ttls = {
        (field["collectionGroup"], field["fieldPath"])
        for field in mirror["fieldOverrides"]
        if field.get("ttl") is True and field["collectionGroup"] in handler_collections
    }
    assert mirrored_ttls == expected_ttls

    # The console's disposable collections, and only those, may carry a TTL.
    console_ttls = {
        (field["collectionGroup"], field["fieldPath"])
        for field in mirror["fieldOverrides"]
        if field.get("ttl") is True and field["collectionGroup"] in console_collections
    }
    assert console_ttls == {
        ("ticket_console_cache", "expires_at"),
        ("devrev_message_cache", "expires_at"),
        ("ticket_import_staging", "expires_at"),
        ("idempotency_keys", "expires_at"),
    }
    assert not any(
        field["collectionGroup"] == "ticket_reviews" for field in mirror["fieldOverrides"]
    )

    for fields in expected_indexes:
        for field, order in fields:
            assert f'field_path = "{field}"' in firestore
            assert f'order      = "{order}"' in firestore
    for collection, field in expected_ttls:
        assert f'collection = "{collection}"' in firestore
        assert f'field      = "{field}"' in firestore

    # state+__name__ is covered by Firestore's automatic state index, whose
    # final key is __name__ ASC. Do not declare a redundant manual index.
    assert all("__name__" not in pair for index in expected_indexes for pair in index)


def test_infra_only_may_omit_an_image_but_services_fail_closed() -> None:
    variables = _read(MODULE_ROOT / "variables.tf")
    cloud_run = _read(MODULE_ROOT / "cloud_run.tf")

    assert 'var.image_digest == "" || can(regex(' in variables
    assert "condition     = local.image_is_immutable" in cloud_run
    assert "un digest @sha256 es obligatorio al crear servicios" in cloud_run


def test_release_phase_maps_to_one_exact_handler_mode() -> None:
    main = _read(MODULE_ROOT / "main.tf")

    for phase, mode in (
        ("infra_only", "disabled"),
        ("dark_no_traffic", "disabled"),
        ("dark_100", "disabled"),
        ("shadow", "shadow"),
        ("knowledge_only", "knowledge_only"),
        ("full", "full"),
    ):
        assert re.search(rf'{phase}\s*=\s*"{mode}"', main)
    assert "var.ticket_handler_mode == local.expected_ticket_handler_mode" in main


def test_worker_target_uri_is_computed_and_oidc_audience_is_stable() -> None:
    """The worker cannot consume its own computed URI in its template.

    Producer/reconciler use the provider-computed URI as the HTTP target, while
    Cloud Tasks and the worker share a deterministic Cloud Run custom audience.
    """
    cloud_run = _read(MODULE_ROOT / "cloud_run.tf")
    variables = _read(MODULE_ROOT / "variables.tf")

    assert 'variable "worker_url"' not in variables
    assert 'local.worker_target_url' in cloud_run
    assert 'custom_audiences = [local.worker_oidc_audience]' in cloud_run
    assert cloud_run.count('name  = "TICKET_WORKER_AUDIENCE"') == 3
    assert 'value = local.worker_oidc_audience' in cloud_run
    assert re.search(
        r"producer_managed_env_names\s*=.*?TICKET_WORKER_AUDIENCE.*?\]\)\)",
        cloud_run,
        re.DOTALL,
    )

    worker = cloud_run.split(
        'resource "google_cloud_run_v2_service" "worker"', 1
    )[1].split(
        'resource "google_cloud_run_v2_job" "reconciler"', 1
    )[0]
    assert 'name  = "TICKET_WORKER_URL"' not in worker


def test_e2e_job_uses_provider_computed_producer_uri() -> None:
    e2e = _read(MODULE_ROOT / "e2e.tf")
    module_variables = _read(MODULE_ROOT / "variables.tf")
    staging_variables = _read(STAGING_ROOT / "variables.tf")

    assert "producer_url" not in module_variables.split('variable "e2e_job"', 1)[1]
    assert "producer_url" not in staging_variables.split('variable "e2e_job"', 1)[1]
    assert e2e.count("google_cloud_run_v2_service.producer[0].uri") == 3
    assert "var.e2e_job.producer_url" not in e2e


def test_staging_e2e_secondary_url_is_a_terraform_derived_tagged_revision() -> None:
    cloud_run = _read(MODULE_ROOT / "cloud_run.tf")
    e2e = _read(MODULE_ROOT / "e2e.tf")
    outputs = _read(MODULE_ROOT / "outputs.tf")
    module_variables = _read(MODULE_ROOT / "variables.tf")
    staging_main = _read(STAGING_ROOT / "main.tf")
    staging_variables = _read(STAGING_ROOT / "variables.tf")

    assert 'variable "producer_baseline_tag"' in module_variables
    assert 'variable "producer_baseline_revision"' in staging_variables
    assert "producer_baseline_revision = var.producer_baseline_revision" in staging_main
    assert 'tag      = var.producer_baseline_tag' in cloud_run
    assert 'tag     = var.producer_candidate_tag' in cloud_run
    assert 'name  = "E2E_SECONDARY_PRODUCER_URL"' in e2e
    assert "local.producer_baseline_url" in e2e
    assert 'output "e2e_secondary_producer_url"' in outputs
    assert "E2E_SECONDARY_PRODUCER_URL" not in module_variables.split(
        'variable "e2e_job"', 1
    )[1]


def test_e2e_service_account_gets_only_its_eleven_exact_secret_accessors() -> None:
    variables = _read(MODULE_ROOT / "variables.tf")
    secrets = _read(MODULE_ROOT / "secrets.tf")
    e2e = _read(MODULE_ROOT / "e2e.tf")
    staging_main = _read(STAGING_ROOT / "main.tf")

    assert 'variable "e2e_secret_containers"' in variables
    assert re.search(
        r"e2e_secret_containers\s*=\s*var\.e2e_secret_containers",
        staging_main,
    )
    assert 'resource "google_secret_manager_secret_iam_member" "e2e_runtime_accessor"' in secrets
    block = secrets.split(
        'resource "google_secret_manager_secret_iam_member" "e2e_runtime_accessor"', 1
    )[1]
    assert (
        'for_each = var.e2e_job.enabled && var.env == "staging" '
        "? var.e2e_secret_containers : {}"
    ) in block
    assert 'role      = "roles/secretmanager.secretAccessor"' in block
    assert "var.e2e_job.service_account_email" in block
    assert "secretmanager.versions." not in secrets
    for key in (
        "E2E_API_KEY", "E2E_DIFFERENTIAL_LEGACY_API_KEY",
        "E2E_WRONG_PRINCIPAL_API_KEY",
        "E2E_WRONG_TENANT_API_KEY", "E2E_RATE_LIMIT_API_KEY",
        "E2E_FAULT_SIGNING_SECRET", "E2E_N8N_CONTRACT_TOKEN",
        "E2E_FORUSBOTS_LOOKUP_TOKEN", "E2E_DELIVERY_LOOKUP_TOKEN",
        "E2E_GCP_AUDIT_TOKEN", "PINECONE_API_KEY",
    ):
        assert f'"{key}"' in e2e
    for forged_gate_claim in ("E2E_G2_APPROVAL", "E2E_G4_APPROVAL"):
        assert forged_gate_claim not in variables
        assert forged_gate_claim not in e2e


def test_cloud_run_preserves_existing_n8n_iam_contract_without_custom_wif_env() -> None:
    cloud_run = _read(MODULE_ROOT / "cloud_run.tf")

    assert 'name  = "TICKET_WIF_AUDIENCE"' not in cloud_run
    assert 'name  = "TICKET_WIF_ALLOWED_EMAILS"' not in cloud_run
    assert 'name  = "TICKET_WIF_EXPECTED_EMAIL"' not in cloud_run


def test_terraform_uses_existing_kb_rag_client_without_aws_wif() -> None:
    platform_root = REPO_ROOT / "infra" / "terraform" / "live" / "platform"
    platform_iam = _read(platform_root / "runtime_project_iam.tf")
    all_hcl = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (REPO_ROOT / "infra" / "terraform").rglob("*.tf")
    )
    assert not (platform_root / "workload_identity.tf").exists()
    for obsolete in (
        "enable_n8n_wif",
        "n8n_aws_account_id",
        "n8n_aws_role_arns",
        "n8n-ticket-invoker-stg",
        "n8n-ticket-invoker-prod",
        "ticket_wif_audience",
        "ticket_wif_allowed_emails",
    ):
        assert obsolete not in all_hcl
    assert "kb-rag-client@${var.project_id}.iam.gserviceaccount.com" in platform_iam


def test_active_producer_secrets_are_complete_and_worker_is_minimal() -> None:
    cloud_run = _read(MODULE_ROOT / "cloud_run.tf")
    production_variables = _read(PRODUCTION_ROOT / "variables.tf")

    for key in (
        "API_KEY",
        "FORUSBOTS_AUTH_TOKEN",
        "OPENAI_API_KEY",
        "PINECONE_API_KEY",
        "TICKET_FAULT_SIGNING_SECRET",
    ):
        assert f'"{key}"' in cloud_run

    worker = cloud_run.split(
        'resource "google_cloud_run_v2_service" "worker"', 1
    )[1].split('resource "google_cloud_run_v2_job" "reconciler"', 1)[0]
    assert "for_each = local.worker_secret_version_refs" in worker
    for forbidden in (
        "API_KEY",
        "API_CLIENT_KEYS",
        "API_CLIENT_TENANTS",
        "PARTICIPANT_PLAN_SOURCE",
        "TICKET_FAULT_SIGNING_SECRET",
    ):
        assert forbidden not in worker

    for obsolete in (
        "API_CLIENT_KEYS",
        "API_CLIENT_TENANTS",
        "PARTICIPANT_PLAN_SOURCE",
    ):
        assert f'"{obsolete}"' not in production_variables


def test_forusbots_secret_is_injected_and_granted_only_to_worker() -> None:
    cloud_run = _read(MODULE_ROOT / "cloud_run.tf")
    production_variables = _read(PRODUCTION_ROOT / "variables.tf")

    assert re.search(
        r"FORUSBOTS_AUTH_TOKEN\s*=\s*toset\(\[\"worker\"\]\)",
        cloud_run,
    )
    producer_scope = cloud_run.split(
        "producer_runtime_secret_env =", 1
    )[1].split("producer_secret_version_refs =", 1)[0]
    assert re.search(
        r'setsubtract\(\s*local\.expected_runtime_secret_env,\s*'
        r'toset\(\["FORUSBOTS_AUTH_TOKEN"\]\),?\s*\)',
        producer_scope,
    )
    assert re.search(
        r'accessor_roles\["FORUSBOTS_AUTH_TOKEN"\].*==\s*toset\(\["worker"\]\)',
        production_variables,
    )
    worker = cloud_run.split(
        'resource "google_cloud_run_v2_service" "worker"', 1
    )[1].split('resource "google_cloud_run_v2_job" "reconciler"', 1)[0]
    assert "google_secret_manager_secret_iam_member.runtime_accessor" in worker


def test_service_secret_refs_are_exactly_bound_to_project_key_and_container() -> None:
    cloud_run = _read(MODULE_ROOT / "cloud_run.tf")

    for local_name in (
        "expected_runtime_secret_env",
        "runtime_secret_refs_exact",
        "runtime_secret_accessors_exact",
    ):
        assert local_name in cloud_run
    assert '"projects/${var.project_id}/secrets/${secret_id}/versions/"' in cloud_run
    assert (
        "toset(keys(var.secret_version_refs)) == "
        "local.expected_runtime_secret_env" in cloud_run
    )
    assert (
        "toset(keys(var.secret_containers.ids)) == "
        "local.expected_runtime_secret_env" in cloud_run
    )
    assert "toset(keys(var.secret_containers.accessor_roles))" in cloud_run
    assert "local.expected_secret_accessor_roles[key]" in cloud_run
    assert "cada secret_version_ref debe coincidir con project/key/container" in cloud_run


def test_every_deployed_service_requires_complete_startup_configuration() -> None:
    cloud_run = _read(MODULE_ROOT / "cloud_run.tf")
    staging_variables = _read(STAGING_ROOT / "variables.tf")

    assert "local.runtime_core_env_complete" in cloud_run
    assert "local.forusbots_origin_is_canonical" in cloud_run
    assert "toset(keys(var.producer_core_env)) == local.required_producer_core_env" in cloud_run
    assert "local.pricing_manifest_is_reviewed" in cloud_run
    assert 'pricing_as_of == "2026-07-21"' in cloud_run
    assert "local.expected_pricing_model_keys" in cloud_run
    for route in (
        "LLM_ROUTE_EXTRACT_INQUIRIES",
        "LLM_ROUTE_KB_QUESTION_SYNTHESIS",
        "LLM_ROUTE_FORUSBOTS_FIELD_MAP",
        "LLM_ROUTE_GR_BODY_BUILD",
        "LLM_ROUTE_TICKET_FIELD_EXTRACT",
    ):
        assert f'"{route}"' in cloud_run
    assert "todo producer/worker desplegado exige core env exacto" in cloud_run
    assert "FORUSBOTS_BASE_URL debe ser un origen canónico revisado" in cloud_run
    assert 'can(regex("@sha256:[0-9a-f]{64}$", var.image_digest))' in staging_variables
    assert '"infra_only", "dark_no_traffic", "dark_100", "shadow"' in staging_variables


def test_production_imports_existing_default_firestore_database() -> None:
    imports = _read(PRODUCTION_ROOT / "imports.tf")
    platform_containers = _read(
        REPO_ROOT / "infra/terraform/live/platform/environment_containers.tf"
    )
    firestore = _read(MODULE_ROOT / "firestore.tf")

    assert "module.production.google_firestore_database.ticket" not in imports
    assert 'production = "projects/rag-kb-system/databases/(default)"' in platform_containers
    assert "to = google_firestore_database.environment[each.key]" in platform_containers
    assert re.search(r"\bid\s*=\s*each\.value", platform_containers)
    assert 'resource "google_firestore_database"' not in firestore


def test_production_imports_secret_containers_and_scopes_access_per_secret() -> None:
    main = _read(PRODUCTION_ROOT / "main.tf")
    variables = _read(PRODUCTION_ROOT / "variables.tf")
    imports = _read(PRODUCTION_ROOT / "imports.tf")
    platform_containers = _read(
        REPO_ROOT / "infra/terraform/live/platform/environment_containers.tf"
    )
    secrets = _read(MODULE_ROOT / "secrets.tf")
    cloud_run = _read(MODULE_ROOT / "cloud_run.tf")

    assert 'variable "secret_containers"' in variables
    assert re.search(r"secret_containers\s*=\s*var\.secret_containers", main)
    assert "module.production.google_secret_manager_secret.runtime" not in imports
    assert 'resource "google_secret_manager_secret" "environment"' in platform_containers
    assert "existing_environment_secret_ids" in platform_containers
    assert 'resource "google_secret_manager_secret_iam_member" "runtime_accessor"' in secrets
    assert "roles/secretmanager.secretAccessor" in secrets
    assert "google_project_iam" not in secrets
    assert "worker_secret_version_refs" in cloud_run
    worker = cloud_run.split(
        'resource "google_cloud_run_v2_service" "worker"', 1
    )[1].split('resource "google_cloud_run_v2_job" "reconciler"', 1)[0]
    assert "for_each = local.worker_secret_version_refs" in worker


def test_runtime_provider_iam_is_role_and_resource_scoped() -> None:
    iam = _read(MODULE_ROOT / "iam.tf")
    platform_iam = _read(
        REPO_ROOT / "infra/terraform/live/platform/runtime_project_iam.tf"
    )
    platform_imports = _read(REPO_ROOT / "infra/terraform/live/platform/imports.tf")

    assert 'resource "google_project_iam_member"' not in iam
    assert 'resource "google_project_iam_member" "runtime_vertex"' in platform_iam
    assert 'resource "google_project_iam_member" "runtime_firestore"' in platform_iam
    assert "ticket-staging" in platform_iam
    assert re.search(r'database\s*=\s*"\(default\)"', platform_iam)
    assert 'resource "google_storage_bucket_iam_member" "producer_core_objects"' in iam
    assert 'role   = "roles/storage.objectViewer"' in iam
    assert 'resource "google_project_iam_member" "reconciler_vertex"' not in iam
    assert not re.search(
        r'resource "google_project_iam_member"[^}]+roles/storage\.objectViewer',
        iam,
        re.DOTALL,
    )
    assert 'production-producer = "rag-kb-system roles/aiplatform.user' not in platform_imports
    assert 'google_service_account.runtime["ticket-producer-prod"].email' in platform_iam
    assert "module.production.google_project_iam_member.producer_vertex" not in _read(
        PRODUCTION_ROOT / "imports.tf"
    )


def test_staging_exposes_every_gated_runtime_input() -> None:
    main = _read(STAGING_ROOT / "main.tf")
    variables = _read(STAGING_ROOT / "variables.tf")

    for variable in (
        "producer_core_env",
        "secret_containers",
        "e2e_job",
    ):
        assert f'variable "{variable}"' in variables
        assert re.search(rf"{variable}\s*=\s*var\.{variable}", main)

    assert 'variable "runtime_service_accounts"' in variables
    assert "data \"terraform_remote_state\" \"platform\"" not in main


def test_environment_roots_receive_platform_outputs_without_cross_state_reads() -> None:
    for root in (STAGING_ROOT, PRODUCTION_ROOT):
        main = _read(root / "main.tf")
        variables = _read(root / "variables.tf")

        assert 'data "terraform_remote_state" "platform"' not in main
        assert 'variable "runtime_service_accounts"' in variables
        assert "var.runtime_service_accounts" in main


def test_firestore_database_uses_real_api_delete_protection() -> None:
    firestore = _read(
        REPO_ROOT / "infra/terraform/live/platform/environment_containers.tf"
    )

    assert 'delete_protection_state = "DELETE_PROTECTION_ENABLED"' in firestore
    assert re.search(r'deletion_policy\s*=\s*"ABANDON"', firestore)
    assert 'deletion_policy = "DELETE_PROTECTION_ENABLED"' not in firestore
