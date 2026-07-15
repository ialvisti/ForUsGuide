"""Fail-closed contracts for the Terraform Cloud Run runtime topology.

These tests deliberately inspect the deployable HCL.  They protect the
production import from silently dropping core configuration or routing a new
revision before its explicit promotion gate.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = REPO_ROOT / "infra" / "terraform" / "modules" / "ticket_environment"
PRODUCTION_ROOT = REPO_ROOT / "infra" / "terraform" / "live" / "production"


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
        "LOG_LEVEL",
        "NAMESPACE",
        "OPENAI_MODEL",
        "OPENAI_REASONING_EFFORT",
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
