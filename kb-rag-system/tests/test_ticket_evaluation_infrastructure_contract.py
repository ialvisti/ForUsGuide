"""Deployment contracts for the RAG-only ticket evaluation platform.

These tests intentionally inspect Terraform source.  The repository's
authoritative Terraform binary runs in the reviewed remote gate, while this
suite keeps the security boundary reviewable without provider credentials.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
TF_ROOT = REPO_ROOT / "infra" / "terraform"
MODULE = TF_ROOT / "modules" / "tickets_console"
LIVE = TF_ROOT / "live"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _module_text() -> str:
    return "\n".join(_read(path) for path in sorted(MODULE.glob("*.tf")))


def test_ticket_platform_uses_isolated_roots_and_pinned_provider() -> None:
    assert MODULE.is_dir()
    for environment in ("staging", "production"):
        root = LIVE / f"tickets-console-{environment}"
        assert root.is_dir()
        versions = _read(root / "versions.tf")
        backend = _read(root / "backend.tf")
        main = _read(root / "main.tf")

        assert 'required_version = "= 1.9.8"' in versions
        assert 'version = "= 7.41.0"' in versions
        assert f'prefix = "tickets-console/{environment}"' in backend
        assert 'source = "../../modules/tickets_console"' in main



def test_shared_platform_enables_the_iap_api() -> None:
    platform = _read(LIVE / "platform" / "main.tf")
    assert '"iap.googleapis.com"' in platform


def test_ticket_platform_declares_three_isolated_services() -> None:
    cloud_run = _read(MODULE / "cloud_run.tf")
    variables = _read(MODULE / "variables.tf")

    for resource in ("ingest", "console", "evidence_broker"):
        assert f'resource "google_cloud_run_v2_service" "{resource}"' in cloud_run
    assert 'value = "ticket-evaluation-ingest"' in cloud_run
    assert 'value = "tickets-console"' in cloud_run
    assert 'value = "tickets-evidence-broker"' in cloud_run

    # The source callers are Cloud Run services/jobs without VPC egress. Cloud
    # Run does not classify that path as internal, so IAM + exact OIDC (not a
    # broken INTERNAL_ONLY endpoint) is the private boundary here.
    assert len(re.findall(r'ingress\s*=\s*"INGRESS_TRAFFIC_ALL"', cloud_run)) == 3
    assert "INGRESS_TRAFFIC_INTERNAL_ONLY" not in cloud_run
    assert cloud_run.count("invoker_iam_disabled = false") == 3
    assert re.search(r'iap_enabled\s*=\s*true', cloud_run)
    assert len(re.findall(r"deletion_protection\s*=\s*true", cloud_run)) == 3
    assert 'variable "deployment_phase"' in variables
    assert 'contains(["foundation", "workload"]' in variables


def test_ticket_platform_wires_exact_service_to_service_iam() -> None:
    iam = _read(MODULE / "iam.tf")
    main = _read(MODULE / "main.tf")

    assert (
        'resource "google_cloud_run_v2_service_iam_member" '
        '"publisher_invokes_ingest"'
    ) in iam
    assert 'member   = "serviceAccount:${var.publisher_service_account_email}"' in iam
    assert "expected_publisher_service_account" in main
    assert (
        "var.publisher_service_account_email == "
        "local.expected_publisher_service_account"
    ) in main
    assert (
        'resource "google_cloud_run_v2_service_iam_member" '
        '"console_invokes_broker"'
    ) in iam
    assert 'member   = "serviceAccount:${google_service_account.console.email}"' in iam
    assert 'resource "google_iap_web_cloud_run_service_iam_binding" "reviewers"' in iam
    assert re.search(r'role\s*=\s*"roles/iap\.httpsResourceAccessor"', iam)
    assert (
        "service-${data.google_project.current.number}"
        "@gcp-sa-iap.iam.gserviceaccount.com"
    ) in iam

    assert re.search(r'role\s*=\s*"roles/datastore\.user"', iam)
    assert re.search(r'role\s*=\s*"roles/datastore\.viewer"', iam)
    assert 'databases/${local.console_database}' in iam
    assert 'databases/(default)' in iam


def test_ticket_platform_uses_named_database_and_no_file_exchange_storage() -> None:
    firestore = _read(MODULE / "firestore.tf")

    assert 'name        = local.console_database' in firestore
    assert 'delete_protection_state = "DELETE_PROTECTION_ENABLED"' in firestore
    assert 'deletion_policy         = "ABANDON"' in firestore
    for collection in (
        "devrev_message_cache",
        "ticket_console_cache",
        "idempotency_keys",
    ):
        assert f'"{collection}"' in firestore
    for forbidden in ("ticket_imports", "ticket_exports", "ticket_import_staging"):
        assert forbidden not in firestore

    assert re.search(r'collection\s*=\s*"ticket_reviews"', firestore)
    assert 'field_path = "updated_at"' in firestore


def test_ticket_platform_wires_runtime_configuration_and_numeric_secrets() -> None:
    cloud_run = _read(MODULE / "cloud_run.tf")
    variables = _read(MODULE / "variables.tf")
    iam = _read(MODULE / "iam.tf")
    combined = _module_text()

    for name in (
        "TICKET_EVALUATION_INGEST_FIRESTORE_DATABASE",
        "TICKET_EVALUATION_INGEST_OIDC_AUDIENCE",
        "TICKET_EVALUATION_INGEST_ALLOWED_SERVICE_ACCOUNTS",
        "TICKET_EVALUATION_INGEST_DEVREV_TOKEN",
        "TICKETS_FIRESTORE_DATABASE",
        "TICKETS_IAP_AUDIENCE",
        "TICKETS_CONSOLE_ORIGIN",
        "TICKETS_DEVREV_TOKEN",
        "TICKETS_ALLOWED_EMAIL_DOMAINS",
        "TICKETS_DEVREV_ALLOWED_PART_DONS",
        "TICKETS_DEVREV_ALLOWED_TICKET_VISIBILITY_IDS",
        "TICKETS_DEVREV_ALLOWED_TIMELINE_VISIBILITIES",
        "TICKETS_EVIDENCE_BROKER_URL",
        "TICKETS_EVIDENCE_BROKER_AUDIENCE",
        "TICKETS_BROKER_FIRESTORE_DATABASE",
        "TICKETS_BROKER_CONSOLE_SERVICE_ACCOUNT",
        "TICKETS_BROKER_AUDIENCE",
        "TICKETS_CORRELATION_LOOKUP_KEYRING_JSON",
    ):
        assert f'name  = "{name}"' in cloud_run or f'name = "{name}"' in cloud_run

    for variable in (
        "devrev_token_secret_version",
        "cursor_aead_key_secret_version",
        "role_bindings_secret_version",
        "csrf_signing_secret_version",
        "correlation_keyring_secret_version",
        "reviewer_iap_members",
        "allowed_email_domains",
        "devrev_allowed_part_dons",
        "devrev_allowed_ticket_visibility_ids",
        "devrev_allowed_timeline_visibilities",
    ):
        assert f'variable "{variable}"' in variables

    assert '/versions/[0-9]+$' in variables
    assert 'startswith(ref, "projects/${var.project_id}/secrets/")' in combined
    assert "secret_containers_are_distinct" in combined
    assert 'toset(["private", "internal", "external", "public"])' in variables
    assert "DevRev DONs or opaque identity IDs" in variables
    for variable in (
        "devrev_ai_author_ids",
        "devrev_system_author_ids",
        "devrev_human_author_ids",
    ):
        assert f"length(var.{variable}) <= 100" in variables
    assert "author_id_sets_are_disjoint" in combined
    assert 'roles/secretmanager.secretAccessor' in iam
    assert "google_secret_manager_secret_version" not in combined
    assert 'version = "latest"' not in combined
    assert "allUsers" not in combined
    assert "allAuthenticatedUsers" not in combined


def test_ticket_platform_exports_publisher_handoff_without_remote_state() -> None:
    main_tf = _read(MODULE / "main.tf")
    cloud_run = _read(MODULE / "cloud_run.tf")
    iam = _read(MODULE / "iam.tf")
    outputs = _read(MODULE / "outputs.tf")
    assert 'data "google_project" "current"' in main_tf
    assert (
        'console_origin = "https://${local.console_service_name}-'
        '${data.google_project.current.number}.${var.region}.run.app"'
    ) in main_tf
    assert "service-${data.google_project.current.number}@gcp-sa-iap" in iam
    assert "var.project_number" not in _module_text()
    assert 'value = local.console_origin' in cloud_run
    assert re.search(
        r'console_url\s*=\s*"\$\{local\.console_origin\}/tickets"',
        main_tf,
    )
    assert 'value       = local.workload_enabled ? local.console_url : null' in outputs
    assert "var.console_origin" not in _module_text()
    for name in (
        "ingest_url",
        "ingest_audience",
        "console_url",
        "console_database",
        "console_service_account_email",
        "ingest_service_account_email",
    ):
        assert f'output "{name}"' in outputs

    for environment in ("staging", "production"):
        root = LIVE / environment
        main = _read(root / "main.tf")
        variables = _read(root / "variables.tf")
        assert "ticket_evaluation_publish_enabled" in main
        assert "ticket_evaluation_ingest_url" in main
        assert "ticket_evaluation_ingest_audience" in main
        assert 'variable "ticket_evaluation_publish_enabled"' in variables
        assert 'variable "ticket_evaluation_ingest_url"' in variables
        assert 'variable "ticket_evaluation_ingest_audience"' in variables


def test_ticket_platform_has_no_execution_or_secret_payload_escape_hatches() -> None:
    terraform = "\n".join(
        _read(path)
        for path in sorted(
            [*MODULE.glob("*.tf"), *LIVE.glob("tickets-console-*/*.tf")]
        )
    )
    for forbidden in (
        'resource "terraform_data"',
        'resource "null_resource"',
        'provisioner "local-exec"',
        'provisioner "remote-exec"',
        'resource "google_secret_manager_secret_version"',
        "gcloud run deploy",
        "gcloud run services update",
        'data "terraform_remote_state"',
    ):
        assert forbidden not in terraform


def test_evaluation_dead_letters_page_the_existing_on_call_channels() -> None:
    monitoring = _read(
        TF_ROOT / "modules" / "ticket_environment" / "monitoring.tf"
    )

    assert 'resource "google_logging_metric" "evaluation_dead_letter"' in monitoring
    assert r'\"reason\":\"evaluation_rejected\"' in monitoring
    assert (
        'resource "google_monitoring_alert_policy" '
        '"ticket_evaluation_dead_letter"'
    ) in monitoring
    assert "google_logging_metric.evaluation_dead_letter.name" in monitoring
    assert "notification_channels = var.notification_channels" in monitoring
