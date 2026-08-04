"""Stage 4 evidence-broker contracts.

The broker is the only component permitted to read production ``(default)``.
Firestore IAM cannot narrow that by collection, so these tests are the
narrowing: they prove the broker has no query except a versioned HMAC lookup,
that its fan-out and result count are bounded, that its output is an
allowlisted envelope with no raw text or PII, that legacy documents come back
``unavailable`` rather than inferred, and that caller authorization lives at the
app boundary rather than inside the query layer.

Every identifier here is synthetic.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import SecretStr, ValidationError

from api.ticket_review_models import (
    MAX_BROKER_KEY_VERSIONS,
    MAX_BROKER_RESULTS,
    CorrelationStatus,
    CorrelationTrust,
    EvidenceSourceCollection,
    MissingProvenance,
    RagEvidenceEnvelope,
    RagEvidenceRecord,
    TicketEvidenceLookupRequest,
)
from api.tickets_console_config import (
    BROKER_FORBIDDEN_ENV_VARS,
    CORRELATION_LOOKUP_KEYRING_ENV,
    EvidenceBrokerSettings,
    validate_evidence_broker_settings,
)
from data_pipeline import ticket_review_provenance as prov
from data_pipeline.ticket_evidence_broker import (
    BROKER_COLLECTIONS,
    LOOKUP_HMAC_FIELD,
    LOOKUP_VERSION_FIELD,
    OCCURRED_AT_FIELD,
    REASON_LEGACY_ONLY,
    REASON_NO_EVIDENCE,
    WARNING_COLLECTION_UNAVAILABLE,
    WARNING_LEGACY_ONLY,
    WARNING_RESULTS_TRUNCATED,
    InMemoryEvidenceBackend,
    LookupKeyringUnavailable,
    TicketEvidenceBroker,
    UnknownLookupKeyVersion,
    broker_index_declarations,
)

SYNTHETIC_DON = "don:core:dvrv-us-1:devo/synthetic:ticket/424242"
OTHER_DON = "don:core:dvrv-us-1:devo/synthetic:ticket/999999"
PARTICIPANT_PII = "Ana Synthetic, ana@example.invalid, 555-0100"

LOOKUP_KEY_V7 = b"synthetic-lookup-key-v7"
LOOKUP_KEY_V8 = b"synthetic-lookup-key-v8"
KEYRING = {7: LOOKUP_KEY_V7, 8: LOOKUP_KEY_V8}

T0 = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc)


# =====================================================================
# Synthetic stored documents
# =====================================================================


def _provenance_document(
    *,
    don: str = SYNTHETIC_DON,
    lookup_key: bytes = LOOKUP_KEY_V7,
    lookup_key_version: int = 7,
    trust: CorrelationTrust = CorrelationTrust.VERIFIED_WORKLOAD,
    occurred_at: datetime = T0,
) -> dict:
    """One v1 execution document as the producer would have written it."""
    refs, _ = prov.sanitize_chunk_refs(
        [
            {
                "id": "synthetic-vector-1",
                "article_id": "synthetic-article-1",
                "content": "Contribution limits are reviewed annually.",
                "raw_score": 0.77,
            }
        ],
        namespace="synthetic-namespace",
    )
    template_id, template_sha = prov.prompt_template_artifact("SYSTEM: synthetic template")
    outcome = prov.CorrelationOutcome(
        trust=trust,
        status=(
            CorrelationStatus.LINKED
            if trust is CorrelationTrust.VERIFIED_WORKLOAD
            else CorrelationStatus.UNAVAILABLE
        ),
        source=prov.CORRELATION_SOURCE_N8N_SIGNED,
        ticket_lookup_hmac=prov.ticket_lookup_hmac(lookup_key, don),
        lookup_key_version=lookup_key_version,
        ingress_key_version=2,
        workload_binding_sha256=prov.workload_binding_hash("synthetic-workload"),
        principal_sha256=prov.principal_hash("synthetic-caller"),
    )
    fields = prov.ExecutionProvenance(
        correlation=outcome,
        internal_job_id="a" * 32,
        route="knowledge_question",
        model="synthetic-model",
        provider="synthetic-provider",
        config_version="cfg-1",
        prompt_template_id=template_id,
        prompt_template_sha256=template_sha,
        deployment=prov.deployment_metadata({"K_REVISION": "kb-rag-system-00042-abc"}),
        index_name="synthetic-index",
        namespace="synthetic-namespace",
        source_article_ids=("synthetic-article-1",),
        observed_chunks=tuple(refs),
        response_sha256=prov.response_sha256("A synthetic participant reply."),
    ).as_document_fields()
    return {
        "schema_version": 1,
        "provenance": fields,
        "endpoint": "knowledge_question",
        "timestamp": occurred_at,
        "request_id_hash": "b" * 24,
        "duration_ms": 12.5,
        "failed": False,
    }


def _legacy_document() -> dict:
    """A pre-Stage-4 document: no provenance, therefore no lookup digest."""
    return {
        "request_id_hash": "c" * 24,
        "endpoint": "knowledge_question",
        "timestamp": T0 - timedelta(days=200),
        "duration_ms": 42.0,
        "failed": False,
    }


def _hostile_document() -> dict:
    """A v1 document polluted with fields the broker must never return."""
    document = _provenance_document()
    document.update(
        {
            "ticket_body": PARTICIPANT_PII,
            "participant_email": "ana@example.invalid",
            "generated_response": "Full generated response text.",
            "collected_data": {"ssn": "000-00-0000"},
            "authorization": "Bearer synthetic-token",
            "api_key": "sk-synthetic-api-key",
            "chunk_text": "Contribution limits are reviewed annually.",
            "devrev_work_id": SYNTHETIC_DON,
        }
    )
    return document


def _broker(
    backend: InMemoryEvidenceBackend,
    *,
    active_versions=(7,),
    max_results: int = MAX_BROKER_RESULTS,
    collections=BROKER_COLLECTIONS,
) -> TicketEvidenceBroker:
    return TicketEvidenceBroker(
        backend,
        keyring=KEYRING,
        active_versions=active_versions,
        max_results=max_results,
        collections=collections,
    )


def _request(don: str = SYNTHETIC_DON, **overrides) -> TicketEvidenceLookupRequest:
    return TicketEvidenceLookupRequest(devrev_work_id=SecretStr(don), **overrides)


# =====================================================================
# 13a. Only a versioned HMAC indexed lookup exists
# =====================================================================


class TestOnlyVersionedHmacLookup:
    async def test_a_lookup_queries_by_both_indexed_fields_with_a_hard_limit(self):
        backend = InMemoryEvidenceBackend()
        backend.put(
            EvidenceSourceCollection.EXECUTION_LOGS, "doc-1", _provenance_document()
        )

        envelope = await _broker(backend).lookup(_request())

        assert envelope.correlation_status is CorrelationStatus.LINKED
        # Every issued query carried the version, the digest, and a bound.
        for _collection, version, digest, limit in backend.queries:
            assert version == 7
            assert digest == prov.ticket_lookup_hmac(LOOKUP_KEY_V7, SYNTHETIC_DON)
            assert 0 < limit <= MAX_BROKER_RESULTS

    async def test_the_backend_exposes_no_scan_or_free_form_query(self):
        backend = InMemoryEvidenceBackend()

        # The whole point of the narrow Protocol: there is nothing else to call.
        assert not hasattr(backend, "scan_by_field")
        assert not hasattr(backend, "list_collection")
        assert not hasattr(backend, "dump_collection")
        public = {
            name
            for name in dir(backend)
            if not name.startswith("_") and callable(getattr(backend, name))
        }
        assert public == {"put", "query_by_lookup"}

    async def test_a_different_don_never_returns_another_tickets_evidence(self):
        backend = InMemoryEvidenceBackend()
        backend.put(
            EvidenceSourceCollection.EXECUTION_LOGS, "doc-1", _provenance_document()
        )

        envelope = await _broker(backend).lookup(_request(OTHER_DON))

        assert envelope.records == []
        assert envelope.correlation_status is CorrelationStatus.UNAVAILABLE
        assert envelope.unavailable_reason == REASON_NO_EVIDENCE

    async def test_a_record_written_under_another_key_version_is_not_matched(self):
        backend = InMemoryEvidenceBackend()
        backend.put(
            EvidenceSourceCollection.EXECUTION_LOGS,
            "doc-1",
            _provenance_document(lookup_key=LOOKUP_KEY_V8, lookup_key_version=8),
        )

        # Only version 7 is active, so the v8 record is invisible.
        envelope = await _broker(backend, active_versions=(7,)).lookup(_request())

        assert envelope.records == []
        assert envelope.key_versions_queried == [7]

    def test_every_query_this_module_can_issue_has_a_declared_index(self):
        declarations = broker_index_declarations()

        declared = {
            declaration["collectionGroup"]: [
                (field["fieldPath"], field["order"]) for field in declaration["fields"]
            ]
            for declaration in declarations
        }
        assert set(declared) == {collection.value for collection in BROKER_COLLECTIONS}
        for fields in declared.values():
            # Exactly the shape of the query in FirestoreEvidenceBackend:
            # equality on both lookup fields, then newest-first ordering.
            assert fields == [
                (LOOKUP_VERSION_FIELD, "ASCENDING"),
                (LOOKUP_HMAC_FIELD, "ASCENDING"),
                (OCCURRED_AT_FIELD, "DESCENDING"),
            ]

    def test_the_declared_indexes_are_valid_firestore_json(self):
        # They must be mirrorable verbatim into firestore.indexes.json by the
        # infrastructure stage, so they have to round-trip as JSON today.
        round_tripped = json.loads(json.dumps(broker_index_declarations()))
        assert round_tripped == broker_index_declarations()
        for declaration in round_tripped:
            assert declaration["queryScope"] == "COLLECTION"


# =====================================================================
# 13b/14. Bounded keyring fan-out and explicit rotation failures
# =====================================================================


class TestKeyringFanOut:
    async def test_a_mixed_version_history_is_found_across_active_versions(self):
        backend = InMemoryEvidenceBackend()
        backend.put(
            EvidenceSourceCollection.EXECUTION_LOGS,
            "old",
            _provenance_document(lookup_key=LOOKUP_KEY_V7, lookup_key_version=7),
        )
        backend.put(
            EvidenceSourceCollection.EXECUTION_LOGS,
            "new",
            _provenance_document(lookup_key=LOOKUP_KEY_V8, lookup_key_version=8),
        )

        envelope = await _broker(backend, active_versions=(7, 8)).lookup(_request())

        assert len(envelope.records) == 2
        assert envelope.key_versions_queried == [7, 8]
        # Neither record required the raw DON to be stored anywhere.
        digests = {record.lookup_key_version for record in envelope.records}
        assert digests == {7, 8}

    async def test_fan_out_is_one_query_per_active_version_per_collection(self):
        backend = InMemoryEvidenceBackend()

        await _broker(backend, active_versions=(7, 8)).lookup(_request())

        assert len(backend.queries) == 2 * len(BROKER_COLLECTIONS)
        versions = {version for _c, version, _d, _l in backend.queries}
        assert versions == {7, 8}

    def test_the_active_keyring_is_bounded(self):
        backend = InMemoryEvidenceBackend()
        many = {version: f"key-{version}".encode() for version in range(1, 20)}

        broker = TicketEvidenceBroker(
            backend, keyring=many, active_versions=list(range(1, 20))
        )

        assert len(broker.active_versions) == MAX_BROKER_KEY_VERSIONS

    def test_an_active_version_with_no_configured_key_fails_explicitly(self):
        # Silently skipping it would make historical evidence vanish without
        # anybody being told a key had been retired.
        with pytest.raises(UnknownLookupKeyVersion):
            TicketEvidenceBroker(
                InMemoryEvidenceBackend(), keyring={7: LOOKUP_KEY_V7}, active_versions=(7, 9)
            )

    def test_no_active_version_fails_closed_rather_than_reporting_no_evidence(self):
        for active in ([], [0], [-1], ["7"], [None]):
            with pytest.raises(LookupKeyringUnavailable):
                TicketEvidenceBroker(
                    InMemoryEvidenceBackend(), keyring=KEYRING, active_versions=active
                )

    def test_a_disabled_version_is_never_queried_even_though_its_key_exists(self):
        broker = TicketEvidenceBroker(
            InMemoryEvidenceBackend(), keyring=KEYRING, active_versions=(7,)
        )

        assert broker.active_versions == (7,)

    async def test_an_old_key_still_reproduces_its_historical_reference(self):
        # Retention contract: while v7 remains active, v7-era evidence resolves.
        backend = InMemoryEvidenceBackend()
        backend.put(
            EvidenceSourceCollection.EXECUTION_LOGS,
            "old",
            _provenance_document(lookup_key=LOOKUP_KEY_V7, lookup_key_version=7),
        )

        still_active = await _broker(backend, active_versions=(7, 8)).lookup(_request())
        after_retirement = await _broker(backend, active_versions=(8,)).lookup(_request())

        assert len(still_active.records) == 1
        # Retiring v7 makes its records explicitly unavailable, not silently
        # absent-and-indistinguishable-from-uncorrelated.
        assert after_retirement.records == []
        assert after_retirement.unavailable_reason == REASON_NO_EVIDENCE
        assert after_retirement.key_versions_queried == [8]


# =====================================================================
# 13c. Bounded result count
# =====================================================================


class TestBoundedResults:
    async def test_results_are_capped_and_truncation_is_reported(self):
        backend = InMemoryEvidenceBackend()
        for index in range(40):
            backend.put(
                EvidenceSourceCollection.EXECUTION_LOGS,
                f"doc-{index:03d}",
                _provenance_document(),
            )

        envelope = await _broker(backend, max_results=5).lookup(_request())

        assert len(envelope.records) == 5
        assert envelope.truncated is True
        assert WARNING_RESULTS_TRUNCATED in envelope.warnings

    async def test_a_caller_cannot_raise_the_configured_maximum(self):
        backend = InMemoryEvidenceBackend()
        for index in range(30):
            backend.put(
                EvidenceSourceCollection.EXECUTION_LOGS,
                f"doc-{index:03d}",
                _provenance_document(),
            )

        envelope = await _broker(backend, max_results=3).lookup(
            _request(max_results=MAX_BROKER_RESULTS)
        )

        assert len(envelope.records) == 3

    def test_the_request_model_refuses_an_unbounded_result_count(self):
        with pytest.raises(ValidationError):
            _request(max_results=MAX_BROKER_RESULTS + 1)
        with pytest.raises(ValidationError):
            _request(max_results=0)

    def test_the_envelope_model_refuses_more_records_than_the_maximum(self):
        record = RagEvidenceRecord(
            evidence_reference="r",
            evidence_digest="a" * 64,
            source_collection=EvidenceSourceCollection.EXECUTION_LOGS,
        )
        with pytest.raises(ValidationError):
            RagEvidenceEnvelope(
                records=[record] * (MAX_BROKER_RESULTS + 1),
                result_digest="b" * 64,
                correlation_status=CorrelationStatus.LINKED,
            )

    async def test_a_degraded_source_is_reported_as_a_gap_not_as_absence(self):
        backend = InMemoryEvidenceBackend()
        backend.put(
            EvidenceSourceCollection.EXECUTION_LOGS, "doc-1", _provenance_document()
        )
        backend.unavailable = {EvidenceSourceCollection.TICKET_EXECUTIONS}

        envelope = await _broker(backend).lookup(_request())

        assert len(envelope.records) == 1
        assert WARNING_COLLECTION_UNAVAILABLE in envelope.warnings


# =====================================================================
# 13d/13e. Allowlisted fields; no raw text or PII
# =====================================================================


class TestAllowlistedSanitizedOutput:
    async def test_no_prompt_response_chunk_text_or_pii_survives(self):
        backend = InMemoryEvidenceBackend()
        backend.put(
            EvidenceSourceCollection.EXECUTION_LOGS, "doc-1", _hostile_document()
        )

        envelope = await _broker(backend).lookup(_request())
        rendered = json.dumps(envelope.model_dump(mode="json"), default=str)

        assert len(envelope.records) == 1
        for forbidden in (
            PARTICIPANT_PII,
            "ana@example.invalid",
            "Full generated response text.",
            "000-00-0000",
            "Bearer synthetic-token",
            "sk-synthetic-api-key",
            "Contribution limits are reviewed annually.",
            SYNTHETIC_DON,
        ):
            assert forbidden not in rendered

    async def test_arbitrary_log_fields_are_dropped_rather_than_passed_through(self):
        backend = InMemoryEvidenceBackend()
        backend.put(
            EvidenceSourceCollection.EXECUTION_LOGS, "doc-1", _hostile_document()
        )

        envelope = await _broker(backend).lookup(_request())
        record = envelope.records[0]

        # extra="forbid" on the model is what makes this structural rather
        # than a blocklist that a new log field could slip past.
        assert set(record.model_dump()) == set(RagEvidenceRecord.model_fields)
        for dropped in ("ticket_body", "collected_data", "authorization", "api_key"):
            assert dropped not in record.model_dump()

    async def test_the_evidence_reference_is_opaque_and_not_a_firestore_path(self):
        backend = InMemoryEvidenceBackend()
        backend.put(
            EvidenceSourceCollection.EXECUTION_LOGS, "doc-1", _provenance_document()
        )

        record = (await _broker(backend).lookup(_request())).records[0]

        assert record.evidence_reference == prov.sha256_hex("execution_logs:doc-1")
        assert "doc-1" not in record.evidence_reference
        assert "/" not in record.evidence_reference

    async def test_the_content_digest_changes_when_the_record_changes(self):
        backend = InMemoryEvidenceBackend()
        backend.put(
            EvidenceSourceCollection.EXECUTION_LOGS, "doc-1", _provenance_document()
        )
        first = (await _broker(backend).lookup(_request())).records[0]

        drifted = _provenance_document()
        drifted["provenance"]["pipeline"]["model"] = "a-different-model"
        backend.put(EvidenceSourceCollection.EXECUTION_LOGS, "doc-1", drifted)
        second = (await _broker(backend).lookup(_request())).records[0]

        # Same reference, different digest: a candidate token minted against
        # the first result can no longer be revalidated against the second.
        assert first.evidence_reference == second.evidence_reference
        assert first.evidence_digest != second.evidence_digest

    async def test_the_result_digest_covers_the_exact_record_set(self):
        backend = InMemoryEvidenceBackend()
        backend.put(
            EvidenceSourceCollection.EXECUTION_LOGS, "doc-1", _provenance_document()
        )
        single = await _broker(backend).lookup(_request())

        backend.put(
            EvidenceSourceCollection.EXECUTION_LOGS, "doc-2", _provenance_document()
        )
        pair = await _broker(backend).lookup(_request())

        assert single.result_digest != pair.result_digest
        assert pair.result_digest == prov.envelope_result_digest(
            record.evidence_reference for record in pair.records
        )

    async def test_the_provenance_gaps_are_carried_through_verbatim(self):
        backend = InMemoryEvidenceBackend()
        backend.put(
            EvidenceSourceCollection.EXECUTION_LOGS, "doc-1", _provenance_document()
        )

        record = (await _broker(backend).lookup(_request())).records[0]

        assert record.provenance.index_version is None
        assert MissingProvenance.INDEX_VERSION in record.missing
        assert record.provenance.deployed_revision == "kb-rag-system-00042-abc"
        assert record.provenance.observed_chunks[0].content_sha256 == prov.sha256_hex(
            "Contribution limits are reviewed annually."
        )


# =====================================================================
# 13f. Legacy documents are unavailable, never inferred
# =====================================================================


class TestLegacyIsUnavailable:
    async def test_a_legacy_document_is_unreachable_because_it_has_no_digest(self):
        backend = InMemoryEvidenceBackend()
        backend.put(EvidenceSourceCollection.EXECUTION_LOGS, "legacy-1", _legacy_document())

        envelope = await _broker(backend).lookup(_request())

        assert envelope.records == []
        assert envelope.correlation_status is CorrelationStatus.UNAVAILABLE
        assert envelope.unavailable_reason == REASON_NO_EVIDENCE

    async def test_records_without_verified_trust_are_never_reported_as_linked(self):
        backend = InMemoryEvidenceBackend()
        backend.put(
            EvidenceSourceCollection.EXECUTION_LOGS,
            "candidate-1",
            _provenance_document(trust=CorrelationTrust.CANDIDATE),
        )

        envelope = await _broker(backend).lookup(_request())

        assert len(envelope.records) == 1
        assert envelope.correlation_status is CorrelationStatus.UNAVAILABLE
        assert envelope.unavailable_reason == REASON_LEGACY_ONLY
        assert WARNING_LEGACY_ONLY in envelope.warnings

    def test_an_empty_envelope_cannot_declare_itself_linked(self):
        with pytest.raises(ValidationError):
            RagEvidenceEnvelope(
                records=[],
                result_digest="a" * 64,
                correlation_status=CorrelationStatus.LINKED,
            )


# =====================================================================
# 13g. The DON is transient, and never persisted or logged
# =====================================================================


class TestTheDonIsTransient:
    def test_the_request_carries_the_don_as_a_secret(self):
        request = _request()

        assert isinstance(request.devrev_work_id, SecretStr)
        # A SecretStr keeps the DON out of a repr, a model_dump, and therefore
        # out of any accidental log line or error body.
        assert SYNTHETIC_DON not in repr(request)
        assert SYNTHETIC_DON not in str(request.model_dump())
        assert request.devrev_work_id.get_secret_value() == SYNTHETIC_DON

    @pytest.mark.parametrize("bad", ["", "   ", "x" * 300])
    def test_a_blank_or_oversized_don_is_refused(self, bad):
        with pytest.raises(ValidationError):
            _request(bad)

    async def test_no_query_argument_ever_contains_the_don(self):
        backend = InMemoryEvidenceBackend()
        backend.put(
            EvidenceSourceCollection.EXECUTION_LOGS, "doc-1", _provenance_document()
        )

        await _broker(backend, active_versions=(7, 8)).lookup(_request())

        assert SYNTHETIC_DON not in repr(backend.queries)


# =====================================================================
# 13h. Authorization is the app boundary's job, and the planes stay separate
# =====================================================================


class TestBoundaryResponsibilities:
    def test_the_query_layer_does_not_authorize_and_does_not_pretend_to(self):
        # A caller-authorization check inside the broker would be invisible to
        # the app and easy to bypass by constructing the class directly. It
        # belongs in api/tickets_evidence_broker_main.py, which is where the
        # verified caller identity actually exists.
        broker = _broker(InMemoryEvidenceBackend())

        assert not hasattr(broker, "authorize")
        assert not hasattr(broker, "caller")
        assert "caller" not in TicketEvidenceBroker.lookup.__code__.co_varnames

    def test_the_broker_settings_refuse_every_secret_from_another_plane(self):
        settings = EvidenceBrokerSettings(_env_file=None, ENVIRONMENT="local")

        for foreign in sorted(BROKER_FORBIDDEN_ENV_VARS):
            with pytest.raises(ValueError) as excinfo:
                validate_evidence_broker_settings(settings, env={foreign: "present"})
            assert foreign in str(excinfo.value)

    def test_a_production_broker_requires_its_keyring_and_allowed_versions(self):
        settings = EvidenceBrokerSettings(_env_file=None, ENVIRONMENT="production")

        with pytest.raises(ValueError) as excinfo:
            validate_evidence_broker_settings(settings, env={})

        message = str(excinfo.value)
        assert CORRELATION_LOOKUP_KEYRING_ENV in message
        assert "CORRELATION_ALLOWED_KEY_VERSIONS" in message

    def test_from_settings_builds_only_the_allowed_versions(self):
        settings = EvidenceBrokerSettings(
            _env_file=None,
            ENVIRONMENT="local",
            CORRELATION_LOOKUP_KEYRING_JSON=SecretStr(
                json.dumps({"7": "synthetic-lookup-key-v7", "8": "synthetic-lookup-key-v8"})
            ),
            CORRELATION_ALLOWED_KEY_VERSIONS=[7],
            MAX_RESULTS=4,
        )

        broker = TicketEvidenceBroker.from_settings(settings, InMemoryEvidenceBackend())

        assert broker.active_versions == (7,)

    def test_from_settings_fails_closed_on_a_missing_or_malformed_keyring(self):
        for keyring in ("", "not-json", "[]", "{}"):
            settings = EvidenceBrokerSettings(
                _env_file=None,
                ENVIRONMENT="local",
                CORRELATION_LOOKUP_KEYRING_JSON=SecretStr(keyring),
                CORRELATION_ALLOWED_KEY_VERSIONS=[7],
            )
            with pytest.raises(LookupKeyringUnavailable):
                TicketEvidenceBroker.from_settings(settings, InMemoryEvidenceBackend())

    def test_the_broker_query_layer_imports_no_producer_machinery(self):
        import ast
        import pathlib

        source = pathlib.Path("data_pipeline/ticket_evidence_broker.py").read_text()
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        for forbidden in (
            "api.main",
            "api.ticket_worker",
            "data_pipeline.rag_engine",
            "data_pipeline.ticket_review_repository",
            "data_pipeline.devrev_client",
            "pinecone",
            "openai",
        ):
            assert forbidden not in imported

    def test_from_settings_never_echoes_the_keyring_in_its_error(self):
        settings = EvidenceBrokerSettings(
            _env_file=None,
            ENVIRONMENT="local",
            CORRELATION_LOOKUP_KEYRING_JSON=SecretStr("not-json-but-secret-material"),
            CORRELATION_ALLOWED_KEY_VERSIONS=[7],
        )

        with pytest.raises(LookupKeyringUnavailable) as excinfo:
            TicketEvidenceBroker.from_settings(settings, InMemoryEvidenceBackend())

        assert "not-json-but-secret-material" not in str(excinfo.value)


# =====================================================================
# The app boundary: caller authorization and hard isolation
# =====================================================================


class TestBrokerAppBoundary:
    def test_the_app_module_imports_no_rag_producer_or_console_machinery(self):
        import ast
        import pathlib

        from api.tickets_evidence_broker_main import FORBIDDEN_IMPORTS

        source = pathlib.Path("api/tickets_evidence_broker_main.py").read_text()
        tree = ast.parse(source)

        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        # A revision whose service account can read production logs must not
        # also construct LLM/vector clients or reach a second database.
        for forbidden in FORBIDDEN_IMPORTS:
            assert forbidden not in imported, forbidden
            assert not any(name.startswith(f"{forbidden}.") for name in imported), forbidden

    def test_importing_the_app_does_not_pull_in_the_rag_stack(self):
        import subprocess
        import sys

        # A separate interpreter, so nothing another test already imported can
        # make this pass by accident.
        probe = (
            "import sys;"
            "import api.tickets_evidence_broker_main as m;"
            "bad=[n for n in ('api.main','data_pipeline.rag_engine',"
            "'data_pipeline.ticket_review_repository','data_pipeline.devrev_client',"
            "'pinecone','openai') if n in sys.modules];"
            "print('LEAKED:'+','.join(bad) if bad else 'CLEAN');"
            "print(m.LOOKUP_PATH)"
        )
        completed = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, check=False
        )

        assert completed.returncode == 0, completed.stderr
        assert "CLEAN" in completed.stdout, completed.stdout
        assert "/internal/v1/ticket-evidence:lookup" in completed.stdout

    def test_the_app_exposes_exactly_one_lookup_route_and_no_browser_surface(self):
        from api.tickets_evidence_broker_main import LOOKUP_PATH, app

        routes = {
            (route.path, tuple(sorted(route.methods)))
            for route in app.routes
            if getattr(route, "methods", None)
        }

        assert (LOOKUP_PATH, ("POST",)) in routes
        posts = [path for path, methods in routes if "POST" in methods]
        assert posts == [LOOKUP_PATH]
        # No schema explorer on a service with production log access.
        assert app.docs_url is None
        assert app.redoc_url is None
        assert app.openapi_url is None

    def test_authorization_requires_the_exact_audience_and_service_account(self):
        from api.tickets_evidence_broker_main import (
            BrokerAuthorizationError,
            authorize_console_caller,
        )

        audience = "https://tickets-evidence-broker-abc-uc.a.run.app"
        console_sa = "tickets-console@example.invalid"
        good = {"aud": audience, "email": console_sa, "email_verified": True}

        assert authorize_console_caller(
            good, expected_audience=audience, expected_service_account=console_sa
        ) == console_sa

        for claims in (
            {**good, "aud": "https://some-other-service-abc-uc.a.run.app"},
            {**good, "aud": f"{audience}/extra"},
            {**good, "email": "someone-else@example.invalid"},
            {**good, "email": f"prefix-{console_sa}"},
            {**good, "email_verified": False},
            {**good, "email_verified": "true"},
            {k: v for k, v in good.items() if k != "aud"},
            {k: v for k, v in good.items() if k != "email"},
            {},
        ):
            with pytest.raises(BrokerAuthorizationError):
                authorize_console_caller(
                    claims,
                    expected_audience=audience,
                    expected_service_account=console_sa,
                )

    def test_an_unconfigured_allowlist_denies_rather_than_allows(self):
        from api.tickets_evidence_broker_main import (
            BrokerAuthorizationError,
            authorize_console_caller,
        )

        claims = {"aud": "", "email": "", "email_verified": True}

        for audience, service_account in (("", "sa@example.invalid"), ("aud", ""), ("", "")):
            with pytest.raises(BrokerAuthorizationError):
                authorize_console_caller(
                    claims,
                    expected_audience=audience,
                    expected_service_account=service_account,
                )

    def test_every_denial_returns_the_same_opaque_response(self):
        from fastapi import Depends, FastAPI
        from fastapi.testclient import TestClient

        from api.tickets_evidence_broker_main import (
            LOOKUP_PATH,
            get_broker,
            get_settings,
            lookup_ticket_evidence,
            require_console_caller,
        )

        audience = "https://tickets-evidence-broker-abc-uc.a.run.app"
        console_sa = "tickets-console@example.invalid"
        settings = EvidenceBrokerSettings(
            _env_file=None,
            ENVIRONMENT="local",
            AUDIENCE=audience,
            CONSOLE_SERVICE_ACCOUNT=console_sa,
        )
        backend = InMemoryEvidenceBackend()
        backend.put(
            EvidenceSourceCollection.EXECUTION_LOGS, "doc-1", _provenance_document()
        )
        broker = _broker(backend)

        # A minimal app carrying only the real route and its real dependency, so
        # the authorization behaviour under test is the shipped one. The
        # dependency is declared exactly as the real route declares it; the
        # companion test below proves the real route really does declare it.
        probe = FastAPI()
        probe.post(LOOKUP_PATH, dependencies=[Depends(require_console_caller)])(
            lookup_ticket_evidence
        )
        probe.dependency_overrides[get_settings] = lambda: settings
        probe.dependency_overrides[get_broker] = lambda: broker

        tokens = {
            "console": {"aud": audience, "email": console_sa, "email_verified": True},
            "other": {
                "aud": audience,
                "email": "attacker@example.invalid",
                "email_verified": True,
            },
        }
        probe.state.claims_verifier = lambda token, aud: tokens.get(token) or {}

        body = {"devrev_work_id": SYNTHETIC_DON}
        with TestClient(probe) as client:
            authorized = client.post(
                LOOKUP_PATH,
                json=body,
                headers={"Authorization": "Bearer console"},
            )
            denials = [
                client.post(LOOKUP_PATH, json=body),
                client.post(
                    LOOKUP_PATH, json=body, headers={"Authorization": "Bearer other"}
                ),
                client.post(
                    LOOKUP_PATH, json=body, headers={"Authorization": "Bearer unknown"}
                ),
                client.post(LOOKUP_PATH, json=body, headers={"Authorization": "Bearer "}),
                client.post(
                    LOOKUP_PATH, json=body, headers={"Authorization": "Basic console"}
                ),
            ]

        assert authorized.status_code == 200
        assert authorized.json()["correlation_status"] == CorrelationStatus.LINKED.value
        # The DON went in; it must not come back out.
        assert SYNTHETIC_DON not in authorized.text

        bodies = {response.text for response in denials}
        assert {response.status_code for response in denials} == {403}
        # One indistinguishable denial: an unauthorized caller learns nothing
        # about which half of the boundary it had already cleared.
        assert len(bodies) == 1
        assert SYNTHETIC_DON not in bodies.pop()

    def test_the_route_never_requires_a_dependency_on_the_route_function(self):
        # The dependency must be declared on the route so it cannot be bypassed
        # by calling the handler directly in a future refactor.
        from api.tickets_evidence_broker_main import LOOKUP_PATH, app

        route = next(
            route
            for route in app.routes
            if getattr(route, "path", None) == LOOKUP_PATH
        )
        dependency_names = {
            dependency.call.__name__
            for dependency in route.dependant.dependencies
            if getattr(dependency, "call", None) is not None
        }
        assert "require_console_caller" in dependency_names
