"""Stage 4 provenance contracts: trusted correlation without a raw identifier.

These tests pin the properties that make RAG↔ticket correlation *defensible*
rather than merely plausible:

* an unauthenticated header or body field can only ever produce a candidate;
* a verified workload correlation requires the existing Cloud Run IAM boundary
  **plus** a replay-protected ingress signature over
  ``(timestamp, normalized DON, request digest, idempotency-key hash)``, with
  the ingress and lookup keys distinct;
* no raw DON, display id, or timeline id reaches an execution record, an error
  message, or a log line;
* legacy documents parse as schema v0 and render as gaps, never as facts;
* the public RAG contract and the "logging never breaks a response" property
  are unchanged.

Everything here is synthetic. No organization-specific identifier appears, and
every key is an obvious test value.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from datetime import datetime, timedelta, timezone

import pytest

from api.ticket_review_models import (
    CORRELATION_HMAC_VERSION,
    EXECUTION_LOG_SCHEMA_VERSION,
    INGRESS_SIGNATURE_MAX_SKEW_S,
    LEGACY_EXECUTION_LOG_SCHEMA_VERSION,
    MAX_BROKER_KEY_VERSIONS,
    CorrelationStatus,
    CorrelationTrust,
    EvidenceSourceCollection,
    MissingProvenance,
)
from data_pipeline import ticket_review_provenance as prov
from data_pipeline.execution_logger import (
    WRITE_FAILURE_METRIC,
    ExecutionLogger,
)

# =====================================================================
# Synthetic fixtures
# =====================================================================

# A synthetic DON in DevRev's documented shape. It is the sentinel every
# privacy assertion below searches for.
SYNTHETIC_DON = "don:core:dvrv-us-1:devo/synthetic:ticket/424242"
SYNTHETIC_DISPLAY_ID = "TKT-424242"

INGRESS_KEY_V2 = b"synthetic-ingress-key-v2"
INGRESS_KEY_V3 = b"synthetic-ingress-key-v3"
LOOKUP_KEY_V7 = b"synthetic-lookup-key-v7"
LOOKUP_KEY_V8 = b"synthetic-lookup-key-v8"

T0 = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc)


def _context(
    *,
    don: str = SYNTHETIC_DON,
    timestamp: datetime = T0,
    key_version: int = 2,
    key: bytes = INGRESS_KEY_V2,
    request_body: str = '{"ticket":"synthetic"}',
    idempotency_key: str = "synthetic-idempotency-key",
    signature: str | None = None,
) -> prov.IngressSignatureContext:
    """A correlation context, signed with ``key`` unless overridden."""
    unsigned = prov.IngressSignatureContext(
        devrev_work_id=don,
        timestamp=timestamp,
        request_sha256=prov.request_digest(request_body),
        idempotency_key_sha256=prov.sha256_hex(idempotency_key),
        key_version=key_version,
        signature="0" * 64,
    )
    if signature is None:
        signature = hmac.new(
            key, prov.canonical_ingress_bytes(unsigned), hashlib.sha256
        ).hexdigest()
    return prov.IngressSignatureContext(
        devrev_work_id=unsigned.devrev_work_id,
        timestamp=unsigned.timestamp,
        request_sha256=unsigned.request_sha256,
        idempotency_key_sha256=unsigned.idempotency_key_sha256,
        key_version=unsigned.key_version,
        signature=signature,
    )


def _verify(context, **overrides) -> prov.CorrelationOutcome:
    kwargs = {
        "ingress_keys": {2: INGRESS_KEY_V2, 3: INGRESS_KEY_V3},
        "lookup_key": LOOKUP_KEY_V7,
        "lookup_key_version": 7,
        "now": T0,
        "idempotency_receipt_present": True,
        "workload_binding": "synthetic-workload@example.invalid",
        "principal": "synthetic-caller@example.invalid",
    }
    kwargs.update(overrides)
    return prov.verify_ingress_context(context, **kwargs)


class _Collection:
    """A Firestore collection stub that records the document it was handed."""

    def __init__(self, *, fail: bool = False) -> None:
        self.documents: list[dict] = []
        self.fail = fail

    async def add(self, document):
        if self.fail:
            raise RuntimeError("firestore is unavailable")
        self.documents.append(document)


class _Database:
    def __init__(self, collection: _Collection) -> None:
        self._collection = collection
        self.requested: list[str] = []

    def collection(self, name: str):
        self.requested.append(name)
        return self._collection


def _logger_for_core(collection: _Collection) -> ExecutionLogger:
    """Build a logger without ``__init__``, exactly as the existing suite does."""
    logger = ExecutionLogger.__new__(ExecutionLogger)
    logger.collection = collection
    logger.retention_days = 30
    return logger


def _logger_for_ticket(collection: _Collection) -> ExecutionLogger:
    logger = ExecutionLogger.__new__(ExecutionLogger)
    logger.db = _Database(collection)
    logger.retention_days = 30
    return logger


def _verified_provenance() -> prov.ExecutionProvenance:
    refs, truncated = prov.sanitize_chunk_refs(
        [
            {
                "id": "synthetic-vector-1",
                "article_id": "synthetic-article-1",
                "content": "Contribution limits are reviewed annually.",
                "raw_score": 0.81,
                "score": 0.91,
            }
        ],
        namespace="synthetic-namespace",
    )
    assert not truncated
    template_id, template_sha = prov.prompt_template_artifact("SYSTEM: synthetic template")
    return prov.ExecutionProvenance(
        correlation=_verify(_context()),
        internal_job_id="a" * 32,
        route="knowledge_question",
        model="synthetic-model",
        provider="synthetic-provider",
        config_version="cfg-1",
        prompt_template_id=template_id,
        prompt_template_sha256=template_sha,
        rendered_prompt_trace_sha256=prov.rendered_prompt_trace_hash("rendered synthetic"),
        deployment=prov.deployment_metadata(
            {"K_REVISION": "kb-rag-system-00042-abc", "K_SERVICE": "kb-rag-system"}
        ),
        index_name="synthetic-index",
        namespace="synthetic-namespace",
        source_article_ids=("synthetic-article-1",),
        observed_chunks=tuple(refs),
        response_sha256=prov.response_sha256("A synthetic participant reply."),
    )


# =====================================================================
# 1. An unverified caller can only ever produce a candidate
# =====================================================================


class TestUnverifiedCallersCannotLink:
    def test_a_raw_header_or_body_field_produces_a_candidate_and_never_linked(self):
        outcome = prov.unverified_candidate()

        assert outcome.trust is CorrelationTrust.CANDIDATE
        assert outcome.status is CorrelationStatus.UNAVAILABLE
        assert outcome.source == prov.CORRELATION_SOURCE_UNVERIFIED_HEADER
        # Nothing queryable is derived, so nothing can later be mistaken for a
        # stored link.
        assert outcome.ticket_lookup_hmac is None
        assert outcome.lookup_key_version is None
        assert not outcome.verified

    def test_a_supplied_signature_without_a_configured_key_cannot_verify(self):
        # The caller controls the signature bytes; it must not be able to
        # nominate the key that validates them.
        forged = _context(key=b"attacker-chosen-key")

        outcome = _verify(forged)

        assert not outcome.verified
        assert outcome.reason == prov.REASON_BAD_SIGNATURE
        assert outcome.trust is CorrelationTrust.NONE

    @pytest.mark.parametrize(
        ("overrides", "expected_reason"),
        [
            ({}, prov.REASON_NO_CONTEXT),
        ],
    )
    def test_absent_context_is_explicitly_unavailable(self, overrides, expected_reason):
        outcome = _verify(None, **overrides)

        assert outcome.status is CorrelationStatus.UNAVAILABLE
        assert outcome.reason == expected_reason


# =====================================================================
# 2. A verified workload correlation, and its distinct keys
# =====================================================================


class TestVerifiedWorkloadCorrelation:
    def test_valid_replay_protected_signature_yields_a_versioned_lookup_hmac(self):
        outcome = _verify(_context())

        assert outcome.verified
        assert outcome.trust is CorrelationTrust.VERIFIED_WORKLOAD
        assert outcome.status is CorrelationStatus.LINKED
        assert outcome.source == prov.CORRELATION_SOURCE_N8N_SIGNED
        assert outcome.ticket_lookup_hmac == prov.ticket_lookup_hmac(
            LOOKUP_KEY_V7, SYNTHETIC_DON
        )
        assert outcome.ingress_key_version == 2
        assert outcome.lookup_key_version == 7
        assert outcome.workload_binding_sha256 == prov.workload_binding_hash(
            "synthetic-workload@example.invalid"
        )
        assert outcome.principal_sha256 == prov.principal_hash(
            "synthetic-caller@example.invalid"
        )

    def test_the_ingress_and_lookup_keys_are_distinct_by_construction(self):
        # Verifying with the ingress key must not be able to mint the reference:
        # the digest is keyed by the lookup key alone.
        outcome = _verify(_context())

        assert outcome.ticket_lookup_hmac != prov.ticket_lookup_hmac(
            INGRESS_KEY_V2, SYNTHETIC_DON
        )
        assert outcome.ticket_lookup_hmac == prov.ticket_lookup_hmac(
            LOOKUP_KEY_V7, SYNTHETIC_DON
        )

    def test_the_canonical_bytes_bind_every_field_and_the_version(self):
        base = _context()
        canonical = prov.canonical_ingress_bytes(base).decode("utf-8").split("\n")

        assert canonical[0] == f"v{CORRELATION_HMAC_VERSION}"
        assert canonical[1] == str(int(T0.timestamp()))
        assert canonical[2] == SYNTHETIC_DON
        assert canonical[3] == base.request_sha256
        assert canonical[4] == base.idempotency_key_sha256
        assert canonical[5] == "2"

    def test_a_signature_does_not_transfer_to_another_ticket_or_request(self):
        signed = _context()

        for mutated in (
            _context(
                don="don:core:dvrv-us-1:devo/synthetic:ticket/999",
                signature=signed.signature,
            ),
            _context(request_body='{"ticket":"substituted"}', signature=signed.signature),
            _context(idempotency_key="another-key", signature=signed.signature),
            _context(key_version=3, signature=signed.signature),
        ):
            outcome = _verify(mutated)
            assert not outcome.verified
            assert outcome.reason == prov.REASON_BAD_SIGNATURE

    @pytest.mark.parametrize(
        "drift_s",
        [INGRESS_SIGNATURE_MAX_SKEW_S + 1, -(INGRESS_SIGNATURE_MAX_SKEW_S + 1)],
    )
    def test_a_timestamp_outside_the_five_minute_window_is_rejected(self, drift_s):
        outcome = _verify(_context(), now=T0 + timedelta(seconds=drift_s))

        assert not outcome.verified
        assert outcome.reason == prov.REASON_STALE_TIMESTAMP

    def test_a_timestamp_inside_the_window_is_accepted_at_both_edges(self):
        for drift_s in (INGRESS_SIGNATURE_MAX_SKEW_S, -INGRESS_SIGNATURE_MAX_SKEW_S):
            assert _verify(_context(), now=T0 + timedelta(seconds=drift_s)).verified

    def test_an_unknown_or_retired_ingress_key_version_fails_explicitly(self):
        outcome = _verify(_context(), ingress_keys={9: INGRESS_KEY_V2})

        assert not outcome.verified
        assert outcome.reason == prov.REASON_UNKNOWN_KEY_VERSION

    def test_replay_needs_the_existing_durable_idempotency_receipt(self):
        outcome = _verify(_context(), idempotency_receipt_present=False)

        assert not outcome.verified
        assert outcome.reason == prov.REASON_MISSING_RECEIPT

    def test_a_missing_lookup_key_never_records_a_verified_but_unfindable_link(self):
        for overrides in ({"lookup_key": None}, {"lookup_key_version": None}):
            outcome = _verify(_context(), **overrides)
            assert not outcome.verified
            assert outcome.status is CorrelationStatus.UNAVAILABLE
            assert outcome.reason == prov.REASON_LOOKUP_KEY_UNAVAILABLE


# =====================================================================
# 3. No raw external identifier anywhere
# =====================================================================


class TestNoRawExternalIdentifiers:
    def test_the_lookup_hmac_does_not_contain_or_reveal_the_don(self):
        reference = prov.ticket_lookup_hmac(LOOKUP_KEY_V7, SYNTHETIC_DON)

        assert SYNTHETIC_DON not in reference
        assert SYNTHETIC_DISPLAY_ID not in reference
        assert len(reference) == 64
        assert reference == reference.lower()

    def test_no_raw_identifier_or_key_survives_into_an_execution_document(self):
        document = _verified_provenance().as_document_fields()
        rendered = repr(document)

        for secret in (
            SYNTHETIC_DON,
            SYNTHETIC_DISPLAY_ID,
            INGRESS_KEY_V2.decode(),
            LOOKUP_KEY_V7.decode(),
            "synthetic-idempotency-key",
        ):
            assert secret not in rendered

    def test_hashing_errors_never_echo_the_identifier(self):
        with pytest.raises(ValueError) as excinfo:
            prov.normalized_don("x" * 1024)
        assert "x" * 1024 not in str(excinfo.value)

        with pytest.raises(ValueError) as blank:
            prov.ticket_lookup_hmac(b"", SYNTHETIC_DON)
        assert SYNTHETIC_DON not in str(blank.value)

    def test_existing_request_id_hash_privacy_behaviour_is_unchanged(self):
        collection = _Collection()
        logger = _logger_for_core(collection)
        caller_request_id = f"caller-controlled-{SYNTHETIC_DON}"

        asyncio.run(
            logger.log_execution(
                request_id=caller_request_id,
                endpoint="knowledge_question",
                duration_ms=12.5,
                request_data={"question": SYNTHETIC_DON},
                response_data={},
            )
        )

        document = collection.documents[0]
        assert document["request_id_hash"] == hashlib.sha256(
            caller_request_id.encode("utf-8")
        ).hexdigest()[:24]
        assert len(document["request_id_hash"]) == 24
        assert SYNTHETIC_DON not in repr(document)

    def test_normalization_does_not_case_fold_identifiers(self):
        # Folding case would merge distinct article ids and distinct answers.
        assert prov.sha256_hex("Article-A") != prov.sha256_hex("article-a")
        assert prov.sha256_hex("NO") != prov.sha256_hex("no")
        # It does collapse insignificant whitespace, so equal text hashes equal.
        assert prov.sha256_hex(" a\t b\n") == prov.sha256_hex("a b")


# =====================================================================
# 4-5. Backward compatibility and server-side jobs
# =====================================================================


class TestBackwardCompatibility:
    def test_without_correlation_context_the_document_shape_is_unchanged(self):
        collection = _Collection()
        logger = _logger_for_core(collection)

        asyncio.run(
            logger.log_execution(
                request_id="request",
                endpoint="knowledge_question",
                duration_ms=1.0,
                request_data={},
                response_data={},
            )
        )

        document = collection.documents[0]
        # Additive only: the pre-Stage-4 keys are all still present and no
        # provenance sub-document appears when none was supplied.
        assert "provenance" not in document
        assert document["schema_version"] == EXECUTION_LOG_SCHEMA_VERSION
        assert set(document) >= {
            "request_id_hash",
            "endpoint",
            "timestamp",
            "expires_at",
            "duration_ms",
            "request_shape",
            "response",
            "llm_metadata",
            "failed",
        }

    def test_a_server_side_job_uses_its_validated_internal_id_and_the_same_hmac(self):
        outcome = prov.internal_job_correlation(
            "b" * 32,
            lookup_key=LOOKUP_KEY_V7,
            devrev_work_id=SYNTHETIC_DON,
            lookup_key_version=7,
        )

        assert outcome.verified
        assert outcome.source == prov.CORRELATION_SOURCE_INTERNAL_JOB
        # The same reference the signed n8n path would derive, so one ticket
        # resolves to one evidence set regardless of which producer path ran.
        assert outcome.ticket_lookup_hmac == prov.ticket_lookup_hmac(
            LOOKUP_KEY_V7, SYNTHETIC_DON
        )

    @pytest.mark.parametrize(
        "job_id",
        [
            f"ticket-{SYNTHETIC_DON}",
            "A" * 32,
            "b" * 31,
            "b" * 33,
            "",
            None,
            12345,
        ],
    )
    def test_a_job_id_that_is_not_server_minted_is_refused(self, job_id):
        assert prov.validated_internal_job_id(job_id) is None
        assert not prov.internal_job_correlation(job_id or "").verified

    def test_a_validated_job_id_without_a_lookup_key_is_not_linked(self):
        outcome = prov.internal_job_correlation("c" * 32)

        assert outcome.status is CorrelationStatus.UNAVAILABLE
        assert outcome.ticket_lookup_hmac is None


# =====================================================================
# 6-8. What an execution record includes, excludes, and never claims
# =====================================================================


class TestExecutionRecordContents:
    def test_the_record_includes_every_required_provenance_field(self):
        document = _verified_provenance().as_document_fields()

        assert document["provenance_schema_version"] == EXECUTION_LOG_SCHEMA_VERSION

        correlation = document["correlation"]
        assert correlation["ticket_lookup_hmac"] == prov.ticket_lookup_hmac(
            LOOKUP_KEY_V7, SYNTHETIC_DON
        )
        assert correlation["correlation_source"] == prov.CORRELATION_SOURCE_N8N_SIGNED
        assert correlation["correlation_trust"] == CorrelationTrust.VERIFIED_WORKLOAD.value
        assert correlation["lookup_key_version"] == 7
        assert correlation["ingress_key_version"] == 2
        assert correlation["workload_binding_sha256"]
        assert correlation["principal_sha256"]
        assert correlation["hmac_version"] == CORRELATION_HMAC_VERSION

        assert document["job"]["internal_job_id"] == "a" * 32

        pipeline = document["pipeline"]
        assert pipeline["route"] == "knowledge_question"
        assert pipeline["model"] == "synthetic-model"
        assert pipeline["provider"] == "synthetic-provider"
        assert pipeline["config_version"] == "cfg-1"
        assert pipeline["prompt_template_id"] == prov.PROMPT_TEMPLATE_ID
        assert len(pipeline["prompt_template_sha256"]) == 64
        assert pipeline["deployed_revision"] == "kb-rag-system-00042-abc"
        assert pipeline["index_name"] == "synthetic-index"
        assert pipeline["namespace"] == "synthetic-namespace"

        retrieval = document["retrieval"]
        assert retrieval["source_article_ids"] == ["synthetic-article-1"]
        chunk = retrieval["observed_chunks"][0]
        assert chunk["observed_vector_id"] == "synthetic-vector-1"
        assert chunk["article_id"] == "synthetic-article-1"
        assert len(chunk["content_sha256"]) == 64
        assert chunk["chunk_ordinal"] == 0
        assert chunk["namespace"] == "synthetic-namespace"

        assert len(document["response_sha256"]) == 64

    def test_a_rendered_prompt_trace_hash_is_never_a_semantic_version(self):
        document = _verified_provenance().as_document_fields()
        pipeline = document["pipeline"]

        assert pipeline["rendered_prompt_trace_is_not_a_version"] is True
        # Two functionally identical requests differ, which is exactly why the
        # trace hash cannot be read as a template version.
        assert prov.rendered_prompt_trace_hash("Q: a") != prov.rendered_prompt_trace_hash(
            "Q: b"
        )
        # The template artifact, by contrast, is stable across requests.
        assert prov.prompt_template_artifact("SYSTEM: synthetic template") == (
            prov.PROMPT_TEMPLATE_ID,
            pipeline["prompt_template_sha256"],
        )

    def test_the_record_excludes_secrets_payloads_and_participant_content(self):
        forbidden = (
            "sk-synthetic-api-key",
            "Bearer synthetic-token",
            "My account number is 123456789 and my email is p@example.invalid",
            "Contribution limits are reviewed annually.",
            "A synthetic participant reply.",
        )
        rendered = repr(_verified_provenance().as_document_fields())

        for secret in forbidden:
            assert secret not in rendered

    def test_chunk_refs_are_bounded_and_do_not_claim_reindex_stability(self):
        many = [
            {
                "id": f"synthetic-vector-{index}",
                "article_id": "synthetic-article-1",
                "content": f"chunk {index}",
            }
            for index in range(300)
        ]

        refs, truncated = prov.sanitize_chunk_refs(many, namespace="synthetic-namespace")

        assert truncated
        assert len(refs) == 200
        # The observed id is copied verbatim: nothing here mints or reformats a
        # vector id, and the ordinal is the observed rank, not the KB's
        # per-chunker-run counter.
        assert refs[0].observed_vector_id == "synthetic-vector-0"
        assert [ref.chunk_ordinal for ref in refs[:3]] == [0, 1, 2]

    def test_an_undefendable_chunk_is_dropped_rather_than_guessed(self):
        refs, truncated = prov.sanitize_chunk_refs(
            [
                {"id": "synthetic-vector-1", "content": "no article id"},
                {"article_id": "synthetic-article-1", "content": "no vector id"},
                {"id": "v", "article_id": "a"},
                "not-a-mapping",
            ],
            namespace="synthetic-namespace",
        )

        assert refs == []
        assert truncated

    def test_the_boosted_score_never_replaces_the_observed_similarity(self):
        refs, _ = prov.sanitize_chunk_refs(
            [
                {
                    "id": "v1",
                    "article_id": "a1",
                    "content": "text",
                    "raw_score": 0.5,
                    "score": 0.6,
                }
            ],
            namespace="ns",
        )

        assert refs[0].score == pytest.approx(0.5)

    def test_index_contents_are_never_claimed_to_be_at_a_commit(self):
        document = _verified_provenance().as_document_fields()

        assert document["pipeline"]["index_version"] is None
        assert MissingProvenance.INDEX_VERSION.value in document["missing_provenance"]

    def test_absent_deployment_metadata_is_a_recorded_gap(self):
        metadata = prov.deployment_metadata({})

        assert metadata.revision is None
        assert metadata.commit_sha is None
        assert MissingProvenance.DEPLOYED_REVISION in metadata.missing

    def test_deployment_metadata_reads_only_safe_runtime_variables(self):
        metadata = prov.deployment_metadata(
            {
                "K_REVISION": "kb-rag-system-00042-abc",
                "K_SERVICE": "kb-rag-system",
                "SERVICE_COMMIT_SHA": "d" * 40,
                "SERVICE_IMAGE_DIGEST": "sha256:" + "e" * 64,
                "OPENAI_API_KEY": "sk-synthetic-api-key",
            }
        )

        assert metadata.revision == "kb-rag-system-00042-abc"
        assert metadata.commit_sha == "d" * 40
        assert "sk-synthetic-api-key" not in repr(metadata)

    def test_every_gap_is_listed_when_nothing_could_be_observed(self):
        gaps = prov.ExecutionProvenance().missing_provenance()

        assert set(gaps) == {
            MissingProvenance.INDEX_VERSION,
            MissingProvenance.DEPLOYED_REVISION,
            MissingProvenance.PROMPT_TEMPLATE,
            MissingProvenance.MODEL,
            MissingProvenance.OBSERVED_CHUNKS,
            MissingProvenance.RESPONSE_HASH,
            MissingProvenance.SOURCE_ARTICLES,
        }


# =====================================================================
# 9. Legacy documents parse as schema v0
# =====================================================================


class TestLegacyDocuments:
    def test_a_document_without_the_new_fields_is_schema_v0(self):
        legacy = {
            "request_id_hash": "a" * 24,
            "endpoint": "knowledge_question",
            "timestamp": T0,
            "duration_ms": 42.0,
            "failed": False,
        }

        assert prov.execution_schema_version(legacy) == LEGACY_EXECUTION_LOG_SCHEMA_VERSION

        record = prov.parse_execution_document(
            legacy,
            source_collection=EvidenceSourceCollection.EXECUTION_LOGS,
            evidence_reference="synthetic-reference-1",
        )

        assert record.schema_version == LEGACY_EXECUTION_LOG_SCHEMA_VERSION
        assert MissingProvenance.LEGACY_SCHEMA in record.missing
        assert record.correlation_trust is CorrelationTrust.NONE
        assert record.provenance.correlation_status is CorrelationStatus.UNAVAILABLE
        assert record.provenance.missing_provenance is True
        assert record.provenance.observed_chunks == []
        assert record.lookup_key_version is None

    @pytest.mark.parametrize("bogus", [None, True, -1, "1", 1.5, {}])
    def test_a_malformed_version_reads_as_v0_rather_than_a_corrupt_v1(self, bogus):
        assert (
            prov.execution_schema_version({"provenance_schema_version": bogus})
            == LEGACY_EXECUTION_LOG_SCHEMA_VERSION
        )

    def test_a_v1_document_round_trips_through_the_parser(self):
        document = {
            **_verified_provenance().as_document_fields(),
            "endpoint": "knowledge_question",
            "timestamp": T0,
            "request_id_hash": "b" * 24,
            "failed": False,
            "duration_ms": 12.5,
        }

        record = prov.parse_execution_document(
            document,
            source_collection=EvidenceSourceCollection.EXECUTION_LOGS,
            evidence_reference="synthetic-reference-2",
        )

        assert record.schema_version == EXECUTION_LOG_SCHEMA_VERSION
        assert MissingProvenance.LEGACY_SCHEMA not in record.missing
        assert record.correlation_trust is CorrelationTrust.VERIFIED_WORKLOAD
        assert record.lookup_key_version == 7
        assert record.ingress_key_version == 2
        assert record.internal_job_id == "a" * 32
        assert record.provenance.index_version is None
        assert record.provenance.deployed_revision == "kb-rag-system-00042-abc"
        assert record.provenance.observed_chunks[0].observed_vector_id == "synthetic-vector-1"
        # Re-reading a stored reference is idempotent: the digest and the
        # ordinal are preserved rather than recomputed from absent content.
        assert record.provenance.observed_chunks[0].content_sha256 == prov.sha256_hex(
            "Contribution limits are reviewed annually."
        )
        assert record.provenance.observed_chunks[0].chunk_ordinal == 0
        assert record.source_article_ids == ["synthetic-article-1"]
        assert SYNTHETIC_DON not in repr(record.model_dump())


# =====================================================================
# 11. Logging failure never changes the participant-facing result
# =====================================================================


class TestLoggingFailureIsContained:
    def test_a_failed_core_write_is_swallowed_and_reported_as_a_metric(self, caplog):
        logger = _logger_for_core(_Collection(fail=True))

        with caplog.at_level(logging.ERROR):
            asyncio.run(
                logger.log_execution(
                    request_id="request",
                    endpoint="knowledge_question",
                    duration_ms=1.0,
                    request_data={},
                    response_data={},
                    provenance=_verified_provenance().as_document_fields(),
                )
            )

        assert WRITE_FAILURE_METRIC in caplog.text
        assert "collection=execution_logs" in caplog.text
        assert "error_type=RuntimeError" in caplog.text
        # The failure report itself must not leak the payload it failed to write.
        assert SYNTHETIC_DON not in caplog.text

    def test_a_failed_ticket_write_is_swallowed_and_reported_as_a_metric(self, caplog):
        logger = _logger_for_ticket(_Collection(fail=True))

        with caplog.at_level(logging.ERROR):
            asyncio.run(
                logger.log_ticket_execution(
                    request_id="request",
                    ticket_job_id="a" * 32,
                    mode="knowledge_only",
                    route_summary=[],
                    total_inquiries=1,
                    forusbots_job_ids=[],
                    duration_ms=1.0,
                    provenance=_verified_provenance().as_document_fields(),
                )
            )

        assert WRITE_FAILURE_METRIC in caplog.text
        assert "collection=ticket_executions" in caplog.text

    def test_a_metrics_sink_that_raises_cannot_break_the_request(self):
        logger = _logger_for_core(_Collection(fail=True))

        def _explode(collection: str, error_type: str) -> None:
            raise RuntimeError("the metrics backend is down too")

        logger._metrics_sink = _explode

        # Must simply return: a broken metric pipeline is not a request failure.
        asyncio.run(
            logger.log_execution(
                request_id="request",
                endpoint="knowledge_question",
                duration_ms=1.0,
                request_data={},
                response_data={},
            )
        )

    def test_a_metrics_sink_receives_the_collection_and_error_type(self):
        logger = _logger_for_core(_Collection(fail=True))
        seen: list[tuple[str, str]] = []
        logger._metrics_sink = lambda collection, error_type: seen.append(
            (collection, error_type)
        )

        asyncio.run(
            logger.log_execution(
                request_id="request",
                endpoint="knowledge_question",
                duration_ms=1.0,
                request_data={},
                response_data={},
            )
        )

        assert seen == [("execution_logs", "RuntimeError")]


# =====================================================================
# The provenance argument is the only channel into a log document
# =====================================================================


class TestProvenanceIsNeverScrapedFromAPayload:
    def test_a_caller_supplied_model_or_article_id_never_becomes_provenance(self):
        collection = _Collection()
        logger = _logger_for_core(collection)
        sentinel = "sensitive-participant-158948"

        asyncio.run(
            logger.log_execution(
                request_id="request",
                endpoint="knowledge_question",
                duration_ms=1.0,
                request_data={"question": sentinel},
                response_data={
                    "metadata": {"model": sentinel, "chunks_used": 2},
                    "source_articles": [{"article_id": sentinel}],
                },
            )
        )

        document = collection.documents[0]
        assert sentinel not in repr(document)
        assert "provenance" not in document
        # The pre-existing aggregate counters still work.
        assert document["response"]["chunks_used"] == 2
        assert document["response"]["source_article_count"] == 1

    @pytest.mark.parametrize(
        "hostile",
        [
            {"unexpected_key": "sensitive"},
            {"correlation": {"ok": 1}, "attacker_field": "sensitive"},
            "not-a-mapping",
            None,
            123,
        ],
    )
    def test_only_allowlisted_provenance_keys_reach_the_document(self, hostile):
        collection = _Collection()
        logger = _logger_for_core(collection)

        asyncio.run(
            logger.log_execution(
                request_id="request",
                endpoint="knowledge_question",
                duration_ms=1.0,
                request_data={},
                response_data={},
                provenance=hostile,
            )
        )

        document = collection.documents[0]
        assert "sensitive" not in repr(document)
        assert "attacker_field" not in repr(document)
        assert "unexpected_key" not in repr(document)

    def test_the_ticket_collection_still_refuses_caller_influenced_identifiers(self):
        collection = _Collection()
        logger = _logger_for_ticket(collection)
        sentinel = "sensitive-participant-external-id"

        asyncio.run(
            logger.log_ticket_execution(
                request_id=f"request-{sentinel}",
                ticket_job_id=f"ticket-{sentinel}",
                mode="knowledge_only",
                route_summary=[],
                total_inquiries=1,
                forusbots_job_ids=[f"forusbots-{sentinel}"],
                duration_ms=10,
                error=f"upstream said {sentinel}",
                idempotency_key=f"idem-{sentinel}",
                provenance=_verified_provenance().as_document_fields(),
            )
        )

        document = collection.documents[0]
        assert sentinel not in repr(document)
        assert document["schema_version"] == EXECUTION_LOG_SCHEMA_VERSION
        # The correlation reference is present, and it is a keyed digest.
        assert (
            document["provenance"]["correlation"]["ticket_lookup_hmac"]
            == prov.ticket_lookup_hmac(LOOKUP_KEY_V7, SYNTHETIC_DON)
        )
        assert SYNTHETIC_DON not in repr(document)


# =====================================================================
# 14. Producer rotation and bounded keyring fan-out
# =====================================================================


class TestKeyRotation:
    def test_the_producer_writes_with_exactly_one_current_lookup_version(self):
        first = _verify(_context(), lookup_key=LOOKUP_KEY_V7, lookup_key_version=7)
        rotated = _verify(_context(), lookup_key=LOOKUP_KEY_V8, lookup_key_version=8)

        assert first.lookup_key_version == 7
        assert rotated.lookup_key_version == 8
        # Same ticket, different key version, therefore a different reference —
        # which is exactly why the broker must fan out over active versions.
        assert first.ticket_lookup_hmac != rotated.ticket_lookup_hmac

    def test_a_historical_reference_is_still_reproducible_from_its_old_key(self):
        historical = prov.ticket_lookup_hmac(LOOKUP_KEY_V7, SYNTHETIC_DON)

        assert prov.ticket_lookup_hmac(LOOKUP_KEY_V7, SYNTHETIC_DON) == historical
        assert prov.ticket_lookup_hmac(LOOKUP_KEY_V8, SYNTHETIC_DON) != historical

    def test_keyring_fan_out_is_bounded_normalized_and_deduplicated(self):
        versions = prov.bounded_key_versions([3, 1, 1, 2, 9, 10, 11, 12, 0, -4, True, "2", None])

        assert versions == [1, 2, 3, 9, 10]
        assert len(versions) <= MAX_BROKER_KEY_VERSIONS

    def test_an_ingress_rotation_accepts_both_active_versions_and_no_others(self):
        keyring = {2: INGRESS_KEY_V2, 3: INGRESS_KEY_V3}

        assert _verify(_context(key_version=2, key=INGRESS_KEY_V2), ingress_keys=keyring).verified
        assert _verify(_context(key_version=3, key=INGRESS_KEY_V3), ingress_keys=keyring).verified
        # A retired version is refused rather than silently tried against the
        # remaining keys.
        assert (
            _verify(_context(key_version=1, key=INGRESS_KEY_V2), ingress_keys=keyring).reason
            == prov.REASON_UNKNOWN_KEY_VERSION
        )


# =====================================================================
# The result digest that binds a manual-link candidate
# =====================================================================


class TestEnvelopeDigest:
    def test_the_digest_is_order_independent_but_content_sensitive(self):
        first = prov.envelope_result_digest(["ref-a", "ref-b"])

        assert first == prov.envelope_result_digest(["ref-b", "ref-a"])
        assert first != prov.envelope_result_digest(["ref-a"])
        assert first != prov.envelope_result_digest(["ref-a", "ref-b", "ref-c"])
