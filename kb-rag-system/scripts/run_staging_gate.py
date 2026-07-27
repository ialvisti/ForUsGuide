"""Reviewed entrypoint for the staging E2E and differential Run Job.

The Cloud Run Job can override arguments but not its command.  This wrapper
therefore exposes a closed mode set while keeping both executions bound to the
same reviewed image, runtime digest, synthetic inputs and staging guard.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from urllib.parse import urlsplit


def _run(command: list[str], *, environment: dict[str, str] | None = None) -> int:
    completed = subprocess.run(  # noqa: S603 - argv is a closed reviewed list
        command,
        check=False,
        env=environment,
    )
    return completed.returncode


def _run_e2e() -> int:
    return _run(
        [
            sys.executable,
            "-m", "pytest",
            "-q",
            "--maxfail=1",
            "-m", "staging_e2e",
            "tests/e2e/test_ticket_staging.py",
        ]
    )


def _run_differential(config) -> int:
    from tests.e2e.test_ticket_staging import build_differential_cases

    output_directory = config.evidence_path.parent
    output_directory.mkdir(parents=True, exist_ok=True)
    cases_path = output_directory / "differential-cases.synthetic.json"
    report_path = output_directory / "differential.json"
    cases_path.write_text(
        json.dumps({"cases": build_differential_cases(config)}, sort_keys=True) + "\n"
    )
    child_environment = dict(os.environ)
    child_environment.update(
        {
            "APP_ENV": "staging",
            "TICKET_DIFFERENTIAL_LEGACY_API_KEY": (
                config.differential_legacy_api_key
            ),
            "TICKET_DIFFERENTIAL_V2_API_KEY": config.api_key,
        }
    )
    return _run(
        [
            sys.executable,
            "rag-testing/ticket_differential.py",
            "--cases", str(cases_path),
            "--out", str(report_path),
            "--legacy-url", config.differential_legacy_url,
            "--v2-url", config.producer_url + "/api/v2/handle-ticket",
            "--legacy-audience", config.differential_legacy_audience,
            "--v2-audience", config.producer_url,
            "--main-sha", config.main_sha,
            "--image-digest", config.runtime_digest,
            "--evidence-uri", config.differential_evidence_uri,
            "--execution-scope", config.execution_scope,
        ],
        environment=child_environment,
    )


def _rollback_poll_document(config, *, phase: str, exercise: str, job_id: str) -> dict:
    from tests.e2e.test_ticket_staging import StagingHarness

    harness = StagingHarness(config, None)  # recorder is unused by the poll path
    try:
        result, _states, statuses = harness.poll({
            "status_url": f"/api/v2/ticket-jobs/{job_id}",
        })
    finally:
        harness.close()
    terminal_state = result.get("state")
    if result.get("ticket_job_id") not in {None, job_id} \
            or terminal_state not in {
                "succeeded", "partial", "failed", "timeout", "cancelled",
            } \
            or not statuses or any(status != 200 for status in statuses):
        raise AssertionError("rollback poll did not preserve the exact terminal job")
    return {
        "schema_version": "1.0",
        "artifact_type": "rollback_poll_observation",
        "status": "pass",
        "main_sha": config.main_sha,
        "candidate_image_digest": config.runtime_digest,
        "execution": exercise,
        "phase": phase,
        "job_id_sha256": hashlib.sha256(job_id.encode()).hexdigest(),
        "terminal_state": terminal_state,
        "http_status": 200,
    }


def _run_rollback_poll(config) -> int:
    phase = os.environ.get("E2E_ROLLBACK_PHASE", "")
    exercise = os.environ.get("E2E_ROLLBACK_EXERCISE_ID", "")
    job_id = os.environ.get("E2E_ROLLBACK_JOB_ID", "")
    if phase not in {"before", "after"} \
            or re.fullmatch(r"[a-z][a-z0-9-]{0,62}", exercise) is None \
            or re.fullmatch(r"[A-Za-z0-9_-]{8,128}", job_id) is None:
        raise ValueError("rollback poll environment is invalid")
    document = _rollback_poll_document(
        config, phase=phase, exercise=exercise, job_id=job_id,
    )
    from google.cloud import storage

    parsed = urlsplit(config.evidence_uri)
    object_name = (
        f"handle-ticket/e2e/{config.main_sha}/{exercise}/rollback-{phase}.json"
    )
    blob = storage.Client(project=config.project).bucket(parsed.netloc).blob(object_name)
    blob.upload_from_string(
        json.dumps(document, sort_keys=True, separators=(",", ":")),
        content_type="application/json", if_generation_match=0,
    )
    generation = blob.generation
    if generation is None or not str(generation).isdigit() or int(generation) < 1:
        raise AssertionError("rollback poll upload returned no immutable generation")
    print(
        f"ROLLBACK_POLL_URI=gs://{parsed.netloc}/{object_name}#{generation}",
        flush=True,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the reviewed staging gate")
    parser.add_argument(
        "mode", choices=("e2e", "differential", "rollback-poll", "all"),
    )
    args = parser.parse_args(argv)

    # Import and validate before dispatch. This aborts on production markers,
    # missing approvals/contracts, mutable digests or incomplete secret env.
    from tests.e2e.test_ticket_staging import StagingE2EConfig

    config = StagingE2EConfig.from_environ(os.environ)
    if args.mode == "rollback-poll":
        return _run_rollback_poll(config)
    if args.mode in {"e2e", "all"}:
        result = _run_e2e()
        if result != 0:
            return result
    if args.mode in {"differential", "all"}:
        return _run_differential(config)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - never print payload/config values
        print(f"staging gate error: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(2) from None
