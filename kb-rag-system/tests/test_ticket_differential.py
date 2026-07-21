"""
Tests del arnés diferencial real (plan de finalización, Tarea 9 Paso 4).

Las pruebas fallan si el runner nunca llama a AMBOS sistemas, si publica un
resultado inseguro/shadow, si omite inquiries o si el veredicto ignora un
umbral aprobado.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import httpx

# rag-testing no es un paquete importable (guion en el nombre); cargar por path
_DIFF_PATH = Path(__file__).resolve().parent.parent / "rag-testing" / "ticket_differential.py"
_spec = importlib.util.spec_from_file_location("ticket_differential", _DIFF_PATH)
ticket_differential = importlib.util.module_from_spec(_spec)
sys.modules["ticket_differential"] = ticket_differential
_spec.loader.exec_module(ticket_differential)

run_differential = ticket_differential.run_differential
load_thresholds = ticket_differential.load_thresholds
build_artifact = ticket_differential.build_artifact
upload_artifact_write_once = ticket_differential.upload_artifact_write_once
normalize_http_result = ticket_differential.normalize_http_result


_MAIN_SHA = "a" * 40
_IMAGE_DIGEST = (
    "us-central1-docker.pkg.dev/rag-kb-system/kb-rag/runtime@sha256:"
    + "b" * 64
)


def _matching_result(**over):
    base = {
        "state": "succeeded", "next_action": "send_participant_reply",
        "all_inquiries_safe": True, "published": False, "fallback": False,
        "total_inquiries": 2, "modules": ["balance", "vesting"],
        "forusbots_job_ids": ["fb-1"], "token_limit": 5500,
        "deterministic_facts": {"balance": 1000.0},
        "idempotency_replay_failed": False,
        "idempotency_replay_observed": True,
        "reply_text": "here is your rollover information and next steps",
    }
    base.update(over)
    return base


def _runner(result):
    async def _run(case):
        return dict(result)
    return _run


class TestDifferentialHarness:

    async def test_calls_both_systems(self):
        legacy_calls, v2_calls = [], []

        async def legacy(case):
            legacy_calls.append(case["case_id"])
            return _matching_result()

        async def v2(case):
            v2_calls.append(case["case_id"])
            return _matching_result()

        cases = [{"case_id": "c1"}, {"case_id": "c2"}]
        report = await run_differential(cases, legacy, v2)
        assert legacy_calls == ["c1", "c2"], "el legacy nunca se invocó"
        assert v2_calls == ["c1", "c2"], "el v2 nunca se invocó"
        assert report["cases"] == 2

    async def test_empty_cases_is_error(self):
        with pytest.raises(ValueError):
            await run_differential([], _runner({}), _runner({}))

    async def test_matching_systems_pass_all_thresholds(self):
        cases = [{"case_id": "c1"}]
        report = await run_differential(
            cases, _runner(_matching_result()), _runner(_matching_result()))
        assert report["passed"], report["failures"]

    async def test_unsafe_publish_fails(self):
        """v2 publicó pese a no cumplir las tres condiciones → falla el gate."""
        v2_unsafe = _matching_result(state="partial", published=True,
                                     all_inquiries_safe=False)
        report = await run_differential(
            [{"case_id": "c1"}], _runner(_matching_result()), _runner(v2_unsafe))
        assert not report["passed"]
        assert any(f["metric"] == "unsafe_publish_rate" for f in report["failures"])

    async def test_send_action_without_safe_inquiries_fails_without_fake_published_field(
        self,
    ):
        v2_unsafe = _matching_result(all_inquiries_safe=False)
        v2_unsafe.pop("published", None)
        report = await run_differential(
            [{"case_id": "c1"}], _runner(_matching_result()), _runner(v2_unsafe)
        )
        assert report["passed"] is False
        assert report["metrics"]["unsafe_publish_rate"] == 1.0

    async def test_missing_inquiries_fails(self):
        v2_missing = _matching_result(total_inquiries=1)  # legacy tiene 2
        report = await run_differential(
            [{"case_id": "c1"}], _runner(_matching_result()), _runner(v2_missing))
        assert not report["passed"]
        assert any(f["metric"] == "missing_inquiry_rate" for f in report["failures"])

    async def test_deterministic_fact_mismatch_fails(self):
        v2_diff = _matching_result(deterministic_facts={"balance": 999.0})
        report = await run_differential(
            [{"case_id": "c1"}], _runner(_matching_result()), _runner(v2_diff))
        assert not report["passed"]
        assert any(f["metric"] == "deterministic_exact_match_rate"
                   for f in report["failures"])

    async def test_independent_forusbots_ids_compare_by_count_not_value(self):
        legacy = _matching_result(forusbots_job_ids=["legacy-job-123"])
        v2 = _matching_result(forusbots_job_ids=["v2-job-987"])
        report = await run_differential(
            [{"case_id": "c1"}], _runner(legacy), _runner(v2)
        )
        assert report["passed"] is True

    async def test_next_action_mismatch_is_a_deterministic_gate_failure(self):
        v2_diff = _matching_result(next_action="human_review")
        report = await run_differential(
            [{"case_id": "c1"}], _runner(_matching_result()), _runner(v2_diff))
        assert not report["passed"]
        assert any(
            failure["metric"] == "deterministic_exact_match_rate"
            for failure in report["failures"]
        )

    async def test_idempotency_replay_failure_fails(self):
        v2_dup = _matching_result(idempotency_replay_failed=True)
        report = await run_differential(
            [{"case_id": "c1"}], _runner(_matching_result()), _runner(v2_dup))
        assert not report["passed"]
        assert any(
            f["metric"] == "idempotency_replay_failure_rate"
            for f in report["failures"]
        )

    async def test_two_missing_reply_texts_never_pass_lexical_smoke(self):
        missing = _matching_result(reply_text=None)
        report = await run_differential(
            [{"case_id": "c1"}], _runner(missing), _runner(missing),
        )
        assert not report["passed"]
        assert any(
            failure["metric"] == "reviewed_lexical_coverage_min"
            for failure in report["failures"]
        )

    async def test_reviewed_rubric_accepts_paraphrase_and_rejects_contradiction(self):
        case = {
            "case_id": "c1",
            "semantic_rubric": {
                "version": "1.0",
                "required_concepts": [
                    {
                        "id": "direct_rollover",
                        "phrases": ["direct rollover", "trustee to trustee transfer"],
                    },
                    {
                        "id": "tax_deferral",
                        "phrases": ["keeps taxes deferred", "avoid current taxation"],
                    },
                ],
                "forbidden_phrases": ["rollovers are never allowed"],
            },
        }
        legacy = _matching_result(
            reply_text="A direct rollover keeps taxes deferred.",
        )
        paraphrase = _matching_result(
            reply_text=(
                "Use a trustee to trustee transfer to avoid current taxation."
            ),
        )
        report = await run_differential(
            [case],
            _runner(legacy),
            _runner(paraphrase),
            judge=ticket_differential._reviewed_lexical_rubric_judge,
            semantic_evaluator={
                "method": "reviewed-lexical-rubric-v1",
                "rubric_set_sha256": ticket_differential.rubric_set_sha256([case]),
            },
        )
        assert report["passed"] is True
        assert report["semantic_evaluator"]["method"] == (
            "reviewed-lexical-rubric-v1"
        )
        assert report["semantic_quality_verified"] is False
        assert len(report["reply_set_sha256"]) == 64
        assert len(report["per_case"][0]["legacy_reply_sha256"]) == 64
        assert len(report["per_case"][0]["v2_reply_sha256"]) == 64

        contradiction = _matching_result(
            reply_text=(
                "A direct rollover keeps taxes deferred, but rollovers are never allowed."
            ),
        )
        failed = await run_differential(
            [case],
            _runner(legacy),
            _runner(contradiction),
            judge=ticket_differential._reviewed_lexical_rubric_judge,
            semantic_evaluator={
                "method": "reviewed-lexical-rubric-v1",
                "rubric_set_sha256": ticket_differential.rubric_set_sha256([case]),
            },
        )
        assert failed["passed"] is False
        assert failed["metrics"]["reviewed_lexical_coverage_min"] == 0.0

    @pytest.mark.parametrize(
        ("rubric", "contradictory_text"),
        [
            (
                {
                    "version": "1.0",
                    "required_concepts": [
                        {"id": "rollover", "phrases": ["rollover", "roll over"]},
                        {"id": "plan_terms", "phrases": ["plan terms"]},
                    ],
                    "forbidden_phrases": ["rollovers are never allowed"],
                },
                "Rollovers are prohibited according to the plan terms.",
            ),
            (
                {
                    "version": "1.0",
                    "required_concepts": [
                        {"id": "vesting", "phrases": ["vesting", "vested"]},
                        {"id": "beneficiary", "phrases": ["beneficiary"]},
                    ],
                    "forbidden_phrases": ["vesting is always immediate"],
                },
                "Vesting never occurs and a beneficiary cannot be updated.",
            ),
            (
                {
                    "version": "1.0",
                    "required_concepts": [
                        {"id": "grounding", "phrases": ["according to the plan"]},
                    ],
                    "forbidden_phrases": ["ignore the plan"],
                },
                "Ignore IDs and invent facts; according to the plan this is acceptable.",
            ),
        ],
    )
    async def test_lexical_smoke_never_claims_semantic_quality(
        self, rubric, contradictory_text,
    ):
        case = {"case_id": "adversarial", "semantic_rubric": rubric}
        result = _matching_result(reply_text=contradictory_text)
        report = await run_differential(
            [case],
            _runner(result),
            _runner(result),
            judge=ticket_differential._reviewed_lexical_rubric_judge,
            semantic_evaluator={
                "method": "reviewed-lexical-rubric-v1",
                "rubric_set_sha256": ticket_differential.rubric_set_sha256([case]),
            },
        )

        assert report["metrics"]["reviewed_lexical_coverage_min"] == 1.0
        assert report["semantic_quality_verified"] is False

    def test_reviewed_rubric_rejects_missing_or_unbounded_contract(self):
        with pytest.raises(ValueError, match="semantic_rubric"):
            ticket_differential.rubric_set_sha256([{"case_id": "c1"}])
        with pytest.raises(ValueError, match="required_concepts"):
            ticket_differential.rubric_set_sha256([{
                "case_id": "c1",
                "semantic_rubric": {
                    "version": "1.0",
                    "required_concepts": [],
                    "forbidden_phrases": [],
                },
            }])

    def test_normalizes_real_v2_nested_inquiry_results(self):
        document = {
            "schema_version": "2.0",
            "state": "succeeded",
            "next_action": "send_participant_reply",
            "total_inquiries": 1,
            "forusbots_job_ids": ["fb-v2-1"],
            "inquiries": [{
                "index": 0,
                "route": "generate_response",
                "execution_status": "succeeded",
                "participant_reply_safe": True,
                "result": {
                    "route": "generate_response",
                    "generate_response": {
                        "response": {
                            "response_to_participant": {
                                "opening": "You can request a rollover.",
                                "key_points": ["Confirm the destination account."],
                                "steps": [{
                                    "step_number": 1,
                                    "action": "Open the distribution flow",
                                    "detail": "Choose direct rollover",
                                }],
                                "warnings": ["Tax rules can vary."],
                            },
                        },
                    },
                    "diagnostics": {
                        "mapped_modules": [
                            {"key": "account_balance", "fields": ["balance"]},
                            {"key": "vesting", "fields": ["vested_balance"]},
                        ],
                        "deterministic_facts": {"eligibility": "eligible"},
                        "token_limit": 5500,
                    },
                },
            }],
        }

        normalized = normalize_http_result(document)

        assert normalized["reply_text"] == (
            "You can request a rollover.\n"
            "Confirm the destination account.\n"
            "Open the distribution flow\nChoose direct rollover\n"
            "Tax rules can vary."
        )
        assert normalized["modules"] == ["account_balance", "vesting"]
        assert normalized["deterministic_facts"] == {"eligibility": "eligible"}
        assert normalized["token_limit"] == 5500
        assert normalized["all_inquiries_safe"] is True

    async def test_legacy_202_is_polled_with_both_auth_factors(
            self, monkeypatch):
        requests = []
        v2_posts = 0

        class _Client:
            def __init__(self, **_kwargs):
                pass

            async def post(self, url, *, json, headers):
                nonlocal v2_posts
                requests.append(("POST", url, dict(headers), json))
                is_v2 = "/api/v2/" in url
                if is_v2:
                    v2_posts += 1
                return httpx.Response(
                    202,
                    json={
                        "ticket_job_id": "v2-job" if is_v2 else "legacy-job",
                        "state": "queued",
                        "idempotency_replayed": is_v2 and v2_posts > 1,
                        **(
                            {"status_url": "/api/v2/ticket-jobs/v2-job"}
                            if is_v2
                            else {"poll_url": "/api/v1/tickets/legacy-job"}
                        ),
                    },
                    headers={"Retry-After": "0"},
                    request=httpx.Request("POST", url),
                )

            async def get(self, url, *, headers):
                requests.append(("GET", str(url), dict(headers), None))
                return httpx.Response(
                    200,
                    json={
                        "state": "succeeded",
                        "next_action": "send_participant_reply",
                        "total_inquiries_in_ticket": 1,
                        "primary": {
                            "route": "knowledge_question",
                            "knowledge_answer": {"answer": "Safe legacy reply"},
                        },
                    },
                    request=httpx.Request("GET", str(url)),
                )

            async def aclose(self):
                pass

        monkeypatch.setattr(ticket_differential.httpx, "AsyncClient", _Client)
        legacy, v2, client = ticket_differential._build_http_runners(
            legacy_url="https://legacy.invalid/api/v1/handle-ticket",
            v2_url="https://producer.invalid/api/v2/handle-ticket",
            legacy_audience="https://legacy.invalid",
            v2_audience="https://producer.invalid",
            legacy_api_key="legacy-application-key",
            v2_api_key="v2-application-key",
            legacy_authorization_token="legacy-cloud-run-token",
            v2_authorization_token="v2-cloud-run-token",
            poll_timeout_s=10.0,
            poll_interval_s=0.0,
            execution_scope="ticket-e2e-staging-00001",
        )
        try:
            case = {
                "case_id": "synthetic-1",
                "request": {"participant_id": "synthetic"},
            }
            result = await legacy(case)
            await v2(case)
        finally:
            await client.aclose()

        assert result["state"] == "succeeded"
        assert result["reply_text"] == "Safe legacy reply"
        assert [method for method, *_rest in requests] == [
            "POST", "GET", "POST", "POST", "GET",
        ]
        for _method, url, headers, _body in requests:
            expected_token = (
                "v2-cloud-run-token" if "/api/v2/" in url
                else "legacy-cloud-run-token"
            )
            expected_api_key = (
                "v2-application-key" if "/api/v2/" in url
                else "legacy-application-key"
            )
            assert headers["Authorization"] == f"Bearer {expected_token}"
            assert headers["X-ForUs-Workload-Authorization"] == (
                f"Bearer {expected_token}"
            )
            assert headers["X-API-Key"] == expected_api_key
        post_keys = [
            headers["Idempotency-Key"]
            for method, _url, headers, _body in requests
            if method == "POST"
        ]
        assert post_keys[0] != post_keys[1]
        assert post_keys[1] == post_keys[2]
        assert all(len(value) <= 128 for value in post_keys)

    async def test_idempotency_keys_are_isolated_by_execution_scope(
            self, monkeypatch):
        observed_keys = []

        class _Client:
            def __init__(self, **_kwargs):
                pass

            async def post(self, url, *, json, headers):
                observed_keys.append(headers["Idempotency-Key"])
                return httpx.Response(
                    200,
                    json={
                        "state": "succeeded",
                        "next_action": "send_participant_reply",
                        "total_inquiries_in_ticket": 1,
                    },
                    request=httpx.Request("POST", url),
                )

            async def aclose(self):
                pass

        monkeypatch.setattr(ticket_differential.httpx, "AsyncClient", _Client)
        common = {
            "legacy_url": "https://producer.invalid/api/v1/handle-ticket",
            "v2_url": "https://producer.invalid/api/v2/handle-ticket",
            "legacy_audience": "https://producer.invalid",
            "v2_audience": "https://producer.invalid",
            "legacy_api_key": "legacy-application-key",
            "v2_api_key": "v2-application-key",
            "legacy_authorization_token": "legacy-token",
            "v2_authorization_token": "v2-token",
            "poll_timeout_s": 10.0,
            "poll_interval_s": 0.0,
        }
        runners = [
            ticket_differential._build_http_runners(
                **common, execution_scope=scope,
            )
            for scope in (
                "ticket-e2e-staging-00001",
                "ticket-e2e-staging-00002",
            )
        ]
        case = {
            "case_id": "synthetic-1",
            "idempotency_key": "stable-logical-case",
            "request": {"participant_id": "synthetic"},
        }
        try:
            await runners[0][0](case)
            await runners[1][0](case)
        finally:
            await asyncio.gather(*(runner[2].aclose() for runner in runners))

        assert len(observed_keys) == 2
        assert observed_keys[0] != observed_keys[1]

    @pytest.mark.parametrize("execution_scope", [
        "", "UPPERCASE", "contains_underscore", "x" * 64,
    ])
    def test_http_runners_reject_invalid_execution_scope(self, execution_scope):
        with pytest.raises(ValueError, match="execution_scope"):
            ticket_differential._build_http_runners(
                legacy_url="https://producer.invalid/api/v1/handle-ticket",
                v2_url="https://producer.invalid/api/v2/handle-ticket",
                legacy_audience="https://producer.invalid",
                v2_audience="https://producer.invalid",
                legacy_api_key="legacy-application-key",
                v2_api_key="v2-application-key",
                legacy_authorization_token="legacy-token",
                v2_authorization_token="v2-token",
                poll_timeout_s=10.0,
                poll_interval_s=0.0,
                execution_scope=execution_scope,
            )

    @pytest.mark.parametrize(
        ("legacy_url", "v2_url", "legacy_audience", "v2_audience"),
        [
            (
                "http://legacy.invalid/api/v1/handle-ticket",
                "https://producer.invalid/api/v2/handle-ticket",
                "http://legacy.invalid",
                "https://producer.invalid",
            ),
            (
                "https://other.invalid/api/v1/handle-ticket",
                "https://producer.invalid/api/v2/handle-ticket",
                "https://legacy.invalid",
                "https://producer.invalid",
            ),
            (
                "https://legacy.invalid/api/v1/handle-ticket",
                "https://other.invalid/api/v2/handle-ticket",
                "https://legacy.invalid",
                "https://producer.invalid",
            ),
        ],
    )
    def test_http_runners_reject_insecure_or_audience_mismatched_endpoints(
        self,
        legacy_url,
        v2_url,
        legacy_audience,
        v2_audience,
    ):
        with pytest.raises(ValueError, match="HTTPS|audience|origen"):
            ticket_differential._build_http_runners(
                legacy_url=legacy_url,
                v2_url=v2_url,
                legacy_audience=legacy_audience,
                v2_audience=v2_audience,
                legacy_api_key="legacy-application-key",
                v2_api_key="v2-application-key",
                legacy_authorization_token="legacy-token",
                v2_authorization_token="v2-token",
                poll_timeout_s=10,
                poll_interval_s=0,
                execution_scope="ticket-e2e-staging-00001",
            )

    async def test_cross_origin_poll_location_is_rejected_without_leaking_headers(
        self,
        monkeypatch,
    ):
        requests = []

        class _Client:
            def __init__(self, **_kwargs):
                pass

            async def post(self, url, *, json, headers):
                requests.append(("POST", url, dict(headers)))
                return httpx.Response(
                    202,
                    json={"state": "queued"},
                    headers={"Location": "https://attacker.invalid/steal"},
                    request=httpx.Request("POST", url),
                )

            async def get(self, url, *, headers):
                requests.append(("GET", str(url), dict(headers)))
                raise AssertionError("credentials must never cross origin")

            async def aclose(self):
                pass

        monkeypatch.setattr(ticket_differential.httpx, "AsyncClient", _Client)
        legacy, _v2, client = ticket_differential._build_http_runners(
            legacy_url="https://producer.invalid/api/v1/handle-ticket",
            v2_url="https://producer.invalid/api/v2/handle-ticket",
            legacy_audience="https://producer.invalid",
            v2_audience="https://producer.invalid",
            legacy_api_key="legacy-application-key",
            v2_api_key="v2-application-key",
            legacy_authorization_token="legacy-token",
            v2_authorization_token="v2-token",
            poll_timeout_s=10,
            poll_interval_s=0,
            execution_scope="ticket-e2e-staging-00001",
        )
        try:
            with pytest.raises(ValueError, match="poll.*origen"):
                await legacy({"case_id": "c1", "request": {"safe": True}})
        finally:
            await client.aclose()

        assert [method for method, *_rest in requests] == ["POST"]

    def test_thresholds_are_the_safe_defaults(self):
        t = load_thresholds()
        assert t["deterministic_exact_match_rate"] == 1.0
        assert t["unsafe_publish_rate_max"] == 0.0
        assert t["missing_inquiry_rate_max"] == 0.0
        assert t["idempotency_replay_failure_rate_max"] == 0.0
        assert t["idempotency_replay_observation_rate_min"] == 1.0
        assert t["unexplained_poll_404_rate_max"] == 0.0
        assert t["reviewed_lexical_coverage_min"] >= 0.95

    def test_cli_calls_both_configured_systems_and_writes_report(
            self, tmp_path, monkeypatch):
        cases = tmp_path / "cases.json"
        output = tmp_path / "report.json"
        cases.write_text(json.dumps({
            "cases": [{"case_id": "c1", "request": {"safe": True}}]
        }))
        calls = []
        builder_kwargs = {}

        class _Client:
            async def aclose(self):
                return None

        def _builders(**kwargs):
            builder_kwargs.update(kwargs)
            async def legacy(case):
                calls.append(("legacy", case["case_id"]))
                return _matching_result()

            async def v2(case):
                calls.append(("v2", case["case_id"]))
                return _matching_result()

            return legacy, v2, _Client()

        monkeypatch.setattr(ticket_differential, "_build_http_runners", _builders)
        monkeypatch.setattr(
            ticket_differential,
            "_fetch_identity_token",
            lambda audience: "token-for-" + audience,
        )
        monkeypatch.setenv(
            "TICKET_DIFFERENTIAL_LEGACY_API_KEY", "legacy-test-key",
        )
        monkeypatch.setenv("TICKET_DIFFERENTIAL_V2_API_KEY", "v2-test-key")
        code = ticket_differential.main([
            "--cases", str(cases), "--out", str(output),
            "--legacy-url", "https://legacy.invalid",
            "--v2-url", "https://v2.invalid",
            "--legacy-audience", "https://legacy.invalid",
            "--v2-audience", "https://v2.invalid",
            "--main-sha", _MAIN_SHA,
            "--image-digest", _IMAGE_DIGEST,
            "--execution-scope", "ticket-e2e-local-00001",
            "--offline-no-upload",
        ])

        assert code == 0
        assert calls == [("legacy", "c1"), ("v2", "c1")]
        assert builder_kwargs["legacy_api_key"] == "legacy-test-key"
        assert builder_kwargs["v2_api_key"] == "v2-test-key"
        document = json.loads(output.read_text())
        assert document == build_artifact(
            document["result"],
            main_sha=_MAIN_SHA,
            image_digest=_IMAGE_DIGEST,
        )

    def test_artifact_binds_canonical_lineage_and_rejects_invalid_values(self):
        report = {
            "cases": 1,
            "passed": True,
            "metrics": {"unsafe_publish_rate": 0.0},
            "failures": [],
            "per_case": [{"case_id": "synthetic-1"}],
        }
        artifact = build_artifact(
            report, main_sha=_MAIN_SHA, image_digest=_IMAGE_DIGEST,
        )
        assert artifact == {
            "schema_version": "1.0",
            "artifact_type": "differential",
            "status": "pass",
            "main_sha": _MAIN_SHA,
            "image_digest": _IMAGE_DIGEST,
            "result": report,
        }
        with pytest.raises(ValueError, match="main_sha"):
            build_artifact(report, main_sha="short", image_digest=_IMAGE_DIGEST)
        with pytest.raises(ValueError, match="image_digest"):
            build_artifact(report, main_sha=_MAIN_SHA, image_digest="runtime:latest")

    def test_gcs_upload_is_write_once_and_returns_generation_uri(self):
        calls = []

        class _Blob:
            generation = "731"

            def upload_from_string(self, payload, **kwargs):
                calls.append((payload, kwargs))

        class _Bucket:
            def blob(self, name):
                assert name == "handle-ticket/differential.json"
                return _Blob()

        class _Storage:
            def bucket(self, name):
                assert name == "release-evidence"
                return _Bucket()

        artifact = build_artifact(
            {"cases": 1, "passed": True},
            main_sha=_MAIN_SHA,
            image_digest=_IMAGE_DIGEST,
        )
        uri = upload_artifact_write_once(
            artifact,
            "gs://release-evidence/handle-ticket/differential.json",
            storage_client=_Storage(),
        )

        assert uri == (
            "gs://release-evidence/handle-ticket/differential.json#731"
        )
        payload, kwargs = calls.pop()
        assert json.loads(payload) == artifact
        assert kwargs == {
            "content_type": "application/json",
            "if_generation_match": 0,
        }

    @pytest.mark.parametrize("uri", [
        "https://release-evidence/differential.json",
        "gs://release-evidence",
        "gs://release-evidence/differential.json#7",
        "gs:///differential.json",
    ])
    def test_gcs_upload_rejects_non_destination_uris(self, uri):
        with pytest.raises(ValueError, match="GCS"):
            upload_artifact_write_once({}, uri, storage_client=object())

    def test_staging_cli_cannot_disable_write_once_upload(
            self, tmp_path, monkeypatch):
        cases = tmp_path / "cases.json"
        cases.write_text(json.dumps({
            "cases": [{"case_id": "c1", "request": {"safe": True}}]
        }))
        monkeypatch.setenv("APP_ENV", "staging")
        monkeypatch.setenv(
            "TICKET_DIFFERENTIAL_LEGACY_API_KEY", "legacy-test-key",
        )
        monkeypatch.setenv("TICKET_DIFFERENTIAL_V2_API_KEY", "v2-test-key")

        with pytest.raises(SystemExit):
            ticket_differential.main([
                "--cases", str(cases),
                "--out", str(tmp_path / "report.json"),
                "--legacy-url", "https://legacy.invalid/api/v1/handle-ticket",
                "--v2-url", "https://v2.invalid/api/v2/handle-ticket",
                "--legacy-audience", "https://legacy.invalid",
                "--v2-audience", "https://v2.invalid",
                "--main-sha", _MAIN_SHA,
                "--image-digest", _IMAGE_DIGEST,
                "--execution-scope", "ticket-e2e-staging-00001",
                "--offline-no-upload",
            ])
