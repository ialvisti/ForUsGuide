"""Offline contract tests for the gated staging E2E runner.

These tests deliberately import the live harness without executing it.  They
freeze the twenty cases from Task 14 and the fail-closed/sanitized packaging
contract so PR CI can detect an incomplete runner without needing credentials.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import re
import sys
from pathlib import Path

import pytest

from tests.e2e import test_ticket_staging as staging


ROOT = Path(__file__).parents[1]


def _load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_load_script("create_plan_manifest")
create_evidence = _load_script("create_evidence_manifest")
run_staging_gate = _load_script("run_staging_gate")

EXPECTED_CASE_IDS = (
    "01_submit_poll_success",
    "02_cross_instance_poll",
    "03_worker_checkpoint_resume",
    "04_concurrent_idempotency",
    "05_idempotency_payload_conflict",
    "06_authorization_mismatches",
    "07_mixed_route_partial",
    "08_inquiry_limit_unprocessed",
    "09_forusbots_id_preservation",
    "10_prompt_injection_invariants",
    "11_admission_limits",
    "12_secret_sentinel_absent",
    "13_n8n_terminal_matrix",
    "14_tombstone_requeue_generation",
    "15_native_ttl_eventual_deletion",
    "16_pinecone_read_only_namespace",
    "17_lease_epoch_fencing",
    "18_reconciler_repairs",
    "19_database_iam_isolation",
    "20_ambiguous_effect_reconciliation",
)


def test_matrix_defines_exactly_the_twenty_plan_cases() -> None:
    assert staging.CASE_IDS == EXPECTED_CASE_IDS
    collected = tuple(
        name.removeprefix("test_")
        for name, member in inspect.getmembers(staging, inspect.isfunction)
        if name.startswith("test_")
    )
    assert collected == EXPECTED_CASE_IDS


def test_missing_live_environment_fails_closed_without_skip() -> None:
    with pytest.raises(staging.StagingE2EConfigurationError) as error:
        staging.StagingE2EConfig.from_environ({})

    message = str(error.value)
    assert "E2E_ENVIRONMENT" in message
    assert "E2E_PRODUCER_URL" in message
    assert "E2E_G2_APPROVAL" not in message
    assert "E2E_G4_APPROVAL" not in message
    assert "E2E_G2_APPROVAL" not in staging.E2E_NONSECRET_INPUT_KEYS
    assert "E2E_G4_APPROVAL" not in staging.E2E_NONSECRET_INPUT_KEYS
    assert "E2E_PARTICIPANT_PLAN_CONTRACT_VERSION" in message
    assert "E2E_N8N_CONTRACT_URL" in message
    assert "E2E_GCP_AUDIT_CONTRACT_URL" in message
    assert "E2E_MAIN_SHA" in message
    assert "E2E_EVIDENCE_URI" in message
    assert "PINECONE_API_KEY" in message


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("E2E_ENVIRONMENT", "production"),
        ("E2E_GCP_PROJECT", "kb-rag-system"),
        ("E2E_FIRESTORE_DATABASE", "(default)"),
        ("E2E_QUEUE", "ticket-jobs-production"),
        ("E2E_PRODUCER_SERVICE", "kb-rag-system-production"),
        ("E2E_WORKER_SERVICE", "kb-rag-ticket-worker-production"),
    ),
)
def test_production_markers_are_rejected(
    name: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = staging.synthetic_valid_environment()
    environment[name] = value
    with pytest.raises(staging.ProductionTargetRejected):
        staging.StagingE2EConfig.from_environ(environment)


def test_evidence_sanitizer_is_allowlist_only() -> None:
    observation = staging.sanitize_evidence(
        {
            "case_id": EXPECTED_CASE_IDS[0],
            "passed": True,
            "http_statuses": [202, 200],
            "counts": {"logical_jobs": 1, "tasks": 1},
            "state": "succeeded",
            "request_hash": "a" * 64,
            "participant_id": "synthetic-participant-123",
            "ticket_job_id": "raw-job-id",
            "email_body": "secret sentinel and private content",
            "authorization": "Bearer raw-token",
            "api_key": "raw-key",
            "url": "https://example.test/path?token=raw",
            "email": "runner@example.iam.gserviceaccount.com",
            "nested": {"secret": "raw-secret"},
        }
    )

    assert observation == {
        "case_id": EXPECTED_CASE_IDS[0],
        "passed": True,
        "http_statuses": [202, 200],
        "counts": {"logical_jobs": 1, "tasks": 1},
        "state": "succeeded",
        "request_hash": "a" * 64,
    }
    serialized = repr(observation)
    for forbidden in (
        "participant",
        "raw-job-id",
        "secret sentinel",
        "Bearer",
        "raw-key",
        "example.test",
        "runner@",
    ):
        assert forbidden not in serialized


def test_harness_never_silently_skips() -> None:
    source = inspect.getsource(staging)
    assert "pytest.skip" not in source
    assert "importorskip" not in source


def test_canonical_e2e_artifact_is_accepted_by_evidence_manifest() -> None:
    config = staging.StagingE2EConfig.from_environ(
        staging.synthetic_valid_environment()
    )
    recorder = staging.EvidenceRecorder(config)
    for case_id in EXPECTED_CASE_IDS:
        recorder.record({"case_id": case_id, "passed": True})

    document = recorder.document()
    assert create_evidence.validate_artifact(
        "e2e",
        document,
        main_sha=config.main_sha,
        image_digest=config.runtime_digest,
    ) == document
    assert document["result"] == {
        "tests_collected": 20,
        "tests_passed": 20,
        "tests_failed": 0,
        "tests_skipped": 0,
    }


def test_rollback_poll_document_hashes_job_id_and_records_real_terminal_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeHarness:
        def __init__(self, config, recorder):
            del config
            assert recorder is None

        def poll(self, accepted):
            assert accepted["status_url"].endswith("/job-12345678")
            return (
                {"ticket_job_id": "job-12345678", "state": "succeeded"},
                ["succeeded"], [200],
            )

        def close(self):
            return None

    monkeypatch.setattr(staging, "StagingHarness", FakeHarness)
    config = staging.StagingE2EConfig.from_environ(
        staging.synthetic_valid_environment()
    )
    document = run_staging_gate._rollback_poll_document(  # noqa: SLF001
        config, phase="before", exercise="rollback-drill-1", job_id="job-12345678",
    )

    assert document["phase"] == "before"
    assert document["terminal_state"] == "succeeded"
    assert document["http_status"] == 200
    assert document["job_id_sha256"] == __import__("hashlib").sha256(
        b"job-12345678"
    ).hexdigest()
    assert "job-12345678" not in json.dumps(document)


def test_evidence_upload_is_create_only_and_reports_generation(tmp_path: Path) -> None:
    config = staging.StagingE2EConfig.from_environ(
        {
            **staging.synthetic_valid_environment(),
            "E2E_EVIDENCE_PATH": str(tmp_path / "e2e.json"),
        }
    )
    recorder = staging.EvidenceRecorder(config)
    for case_id in EXPECTED_CASE_IDS:
        recorder.record({"case_id": case_id, "passed": True})

    class FakeBlob:
        generation = 7
        uploaded: dict[str, object] = {}

        def upload_from_filename(self, filename: str, **kwargs: object) -> None:
            self.uploaded = {"filename": filename, **kwargs}

        def reload(self) -> None:
            raise AssertionError("objectCreator must never issue a post-upload GET")

    class FakeBucket:
        blob_instance = FakeBlob()

        def blob(self, name: str) -> FakeBlob:
            assert name == (
                "handle-ticket/e2e/" + "c" * 40 +
                "/ticket-e2e-staging-synthetic-00001/e2e.json"
            )
            return self.blob_instance

    class FakeStorage:
        bucket_instance = FakeBucket()

        def bucket(self, name: str) -> FakeBucket:
            assert name == "synthetic-evidence"
            return self.bucket_instance

    uri = recorder.write_and_upload(storage_client=FakeStorage())
    assert uri == (
        "gs://synthetic-evidence/handle-ticket/e2e/" + "c" * 40 +
        "/ticket-e2e-staging-synthetic-00001/e2e.json#7"
    )
    assert FakeBucket.blob_instance.uploaded["if_generation_match"] == 0
    assert FakeBucket.blob_instance.uploaded["content_type"] == "application/json"


def test_each_cloud_run_execution_gets_distinct_write_once_destinations() -> None:
    first_env = staging.synthetic_valid_environment()
    second_env = dict(first_env)
    second_env["CLOUD_RUN_EXECUTION"] = "ticket-e2e-staging-synthetic-00002"

    first = staging.StagingE2EConfig.from_environ(first_env)
    second = staging.StagingE2EConfig.from_environ(second_env)

    assert first.evidence_uri != second.evidence_uri
    assert first.differential_evidence_uri != second.differential_evidence_uri
    assert first.evidence_uri.endswith(
        "/ticket-e2e-staging-synthetic-00001/e2e.json"
    )
    assert second.differential_evidence_uri.endswith(
        "/ticket-e2e-staging-synthetic-00002/differential.json"
    )


def test_iac_injects_exact_e2e_environment_and_has_sufficient_timeout() -> None:
    variables = (ROOT.parent / "infra/terraform/modules/ticket_environment/variables.tf").read_text()
    e2e = (ROOT.parent / "infra/terraform/modules/ticket_environment/e2e.tf").read_text()
    staging_variables = (ROOT.parent / "infra/terraform/live/staging/variables.tf").read_text()

    assert "nonsecret_env" in variables
    assert "nonsecret_env" in staging_variables
    for key in staging.E2E_NONSECRET_INPUT_KEYS:
        assert f'"{key}"' in variables
    for key in staging.E2E_SECRET_INPUT_KEYS:
        assert f'"{key}"' in variables
    for key in staging.E2E_DERIVED_ENV_KEYS:
        assert f'name  = "{key}"' in e2e
    assert 'for_each = var.e2e_job.nonsecret_env' in e2e
    assert 'for_each = var.e2e_job.secret_version_refs' in e2e
    timeout_match = re.search(r'timeout\s*=\s*"(?P<seconds>[0-9]+)s"', e2e)
    assert timeout_match is not None
    # 20 E2E cases and the 3 synthetic differential cases are sequential and
    # may each consume their 45-minute terminal poll budget. Leave teardown
    # and audit margin without making a healthy run wait for that ceiling.
    assert int(timeout_match.group("seconds")) >= (len(EXPECTED_CASE_IDS) + 3) * 2700 + 900


def test_staging_wrapper_fails_fast_on_first_e2e_failure() -> None:
    wrapper = (ROOT / "scripts/run_staging_gate.py").read_text()
    assert '"--maxfail=1"' in wrapper


def test_e2e_image_contains_only_exact_differential_inputs() -> None:
    dockerfile = (ROOT / "Dockerfile.e2e").read_text()
    dockerignore = (ROOT / "Dockerfile.e2e.dockerignore").read_text()

    expected_files = (
        "rag-testing/ticket_differential.py",
        "rag-testing/ticket_differential_thresholds.json",
    )
    for path in expected_files:
        assert f"COPY {path} ./" in dockerfile
        assert f"!{path}" in dockerignore

    assert "!rag-testing/**" not in dockerignore
    assert "rag-testing/reports" not in dockerignore
    assert "rag-testing/captures" not in dockerignore


def test_runner_has_a_real_all_mode_for_e2e_and_differential() -> None:
    dockerfile = (ROOT / "Dockerfile.e2e").read_text()
    wrapper_path = ROOT / "scripts/run_staging_gate.py"
    assert wrapper_path.is_file()
    wrapper = wrapper_path.read_text()

    assert 'ENTRYPOINT ["python", "-m", "scripts.run_staging_gate"]' in dockerfile
    assert 'CMD ["all"]' in dockerfile
    assert '"-m", "pytest"' in wrapper
    assert "rag-testing/ticket_differential.py" in wrapper
    assert "--cases" in wrapper
    assert "--evidence-uri" in wrapper
    assert "--offline-no-upload" not in wrapper


def test_live_differential_cases_ship_reviewed_semantic_rubrics() -> None:
    config = staging.StagingE2EConfig.from_environ(
        staging.synthetic_valid_environment()
    )
    cases = staging.build_differential_cases(config)
    assert len(cases) == 3
    for case in cases:
        rubric = case["semantic_rubric"]
        assert set(rubric) == {
            "version", "required_concepts", "forbidden_phrases",
        }
        assert rubric["version"] == "1.0"
        assert rubric["required_concepts"]
        assert all(concept["phrases"] for concept in rubric["required_concepts"])


def test_differential_cases_are_synthetic_and_contain_no_credentials() -> None:
    config = staging.StagingE2EConfig.from_environ(
        staging.synthetic_valid_environment()
    )
    cases = staging.build_differential_cases(config)
    assert len(cases) >= 3
    assert all(case["case_id"].startswith("synthetic-") for case in cases)
    assert all(case["request"]["participant_id"] == config.participant_id for case in cases)
    serialized = json.dumps(cases, sort_keys=True)
    for secret in (
        config.api_key,
        config.fault_signing_secret,
        config.n8n_contract_token,
        config.pinecone_api_key,
    ):
        assert secret not in serialized
