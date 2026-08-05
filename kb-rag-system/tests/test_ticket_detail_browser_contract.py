"""Stage 7 Step 1 — the contract between the browser and the server.

The sibling file asserts what the shipped files *say*. This one asserts what the
server actually *does* when the review workspace talks to it, over the real
middleware stack, the real router, the real service and the real repository, with
only DevRev and the evidence broker faked.

It exists because the workspace's hardest behaviours are all round trips, and
three of them cannot be reached from a local fixture browser at all:

*   a stale-version save has to come back **412** carrying the current version and
    the time it changed, so the conflict panel can describe the disagreement
    rather than guess at it;
*   **412**, **409** and **428** have to stay three different answers. If they ever
    collapse into one, the interface either loses a reviewer's text to a retry or
    reports a client bug as somebody else's edit;
*   a page cursor has to keep failing when it is spelled as a query parameter,
    because that is the one place a token reaches an access log.

The wiring is imported from the Stage 5 route tests rather than rebuilt. A
second, hand-made harness is how two files end up disagreeing about what the app
under test is.
"""

from __future__ import annotations

import pytest

from api.ticket_review_models import (
    CorrelationStatus,
    CorrelationTrust,
    DevRevActor,
    DevRevActorType,
    DevRevTimelineEntry,
    EvidenceSourceCollection,
    MissingProvenance,
    ObservedChunkRef,
    RagEvidenceEnvelope,
    RagEvidenceRecord,
    RagProvenance,
    ReviewerRole,
    TimelineEntryKind,
    TimelinePage,
    TimelineVisibility,
    review_id_for_devrev_work,
)
from api.ticket_review_routes import (
    API_PREFIX,
    CODE_NOT_FOUND,
    CODE_PRECONDITION_MALFORMED,
    CODE_PRECONDITION_REQUIRED,
    CODE_REVIEW_VERSION_CONFLICT,
    CODE_VALIDATION_FAILED,
)
from api.tickets_csrf import CURSOR_HEADER

from tests.test_ticket_review_routes import (
    SYNTHETIC_DISPLAY_ID,
    SYNTHETIC_DON,
    T0,
    _auth_headers,
    _create_review,
    _FakeDevRev,
    _harness,
    _write_headers,
)

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64


# =====================================================================
# Doubles
# =====================================================================


def _actor(kind: DevRevActorType, name: str, actor_id: str) -> DevRevActor:
    return DevRevActor(actor_id=actor_id, actor_type=kind, display_name=name)


def _timeline_entry(
    index: int,
    *,
    kind: TimelineEntryKind = TimelineEntryKind.COMMENT,
    visibility: TimelineVisibility = TimelineVisibility.EXTERNAL,
    author: DevRevActor | None = None,
    body: str | None = "A synthetic message body.",
    body_type: str | None = "text/plain",
    change_summary: str | None = None,
    unsupported_type: str | None = None,
) -> DevRevTimelineEntry:
    return DevRevTimelineEntry(
        entry_id=f"don:core:dvrv-us-1:devo/synthetic:ticket/424242:entry/{index}",
        object_id=SYNTHETIC_DON,
        kind=kind,
        visibility=visibility,
        body=body,
        body_type=body_type,
        author=author,
        change_summary=change_summary,
        unsupported_type=unsupported_type,
        created_at=T0,
    )


class _PagingDevRev(_FakeDevRev):
    """A DevRev whose conversation genuinely has more than one page.

    Built as a real sequence of pages rather than one page repeated: the
    workspace's forward-only paging, its "there is more" wording, and the
    empty-page-with-a-cursor case are all properties of the *sequence*.
    """

    def __init__(self, pages: list[TimelinePage]) -> None:
        super().__init__()
        self.pages = pages

    async def list_timeline_page(self, work_id: str, *, cursor=None, limit=None):
        self.timeline_calls.append((work_id, cursor, limit))
        index = 0 if cursor is None else int(cursor.split("-")[-1])
        return self.pages[min(index, len(self.pages) - 1)]


def _three_conversation_pages() -> list[TimelinePage]:
    participant = _actor(DevRevActorType.REV_USER, "A Participant", "don:identity:x:revu/1")
    agent = _actor(
        DevRevActorType.DEV_USER,
        "An Agent",
        "don:identity:dvrv-us-1:devo/synthetic:devu/human-1",
    )
    assistant = _actor(
        DevRevActorType.DEV_USER,
        "The Assistant",
        "don:identity:dvrv-us-1:devo/synthetic:devu/ai-1",
    )
    return [
        TimelinePage(
            items=[
                _timeline_entry(0, author=participant),
                _timeline_entry(
                    1,
                    author=agent,
                    visibility=TimelineVisibility.INTERNAL,
                    body="An internal note nobody outside should see.",
                ),
            ],
            next_cursor="remote-page-1",
            page_size=25,
        ),
        TimelinePage(
            items=[
                _timeline_entry(2, author=assistant),
                _timeline_entry(
                    3,
                    kind=TimelineEntryKind.CHANGE_EVENT,
                    author=None,
                    body=None,
                    body_type=None,
                    visibility=TimelineVisibility.PRIVATE,
                    change_summary="stage moved to in_progress",
                ),
                _timeline_entry(
                    4,
                    kind=TimelineEntryKind.UNSUPPORTED,
                    author=None,
                    body=None,
                    body_type=None,
                    visibility=TimelineVisibility.PRIVATE,
                    unsupported_type="timeline_shell",
                ),
            ],
            next_cursor="remote-page-2",
            page_size=25,
        ),
        # An empty page that still offers a cursor is a real upstream answer, and
        # treating it as the end would hide every entry after it.
        TimelinePage(items=[], next_cursor="remote-page-3", page_size=25),
    ]


class _RichBroker:
    """A broker with two records, either verified by the producer or not.

    The split matters: the service only mints *suggestions* when no record
    carries a verified-workload correlation, because a suggestion presented
    beside a proven link would be read as a second proven link. So
    ``verified=True`` exercises the execution fields the evidence panel renders,
    and ``verified=False`` exercises the confirmation flow.
    """

    def __init__(self, *, verified: bool = True) -> None:
        self.calls = 0
        self.verified = verified

    async def lookup(self, devrev_work_id: str, *, max_results: int) -> RagEvidenceEnvelope:
        self.calls += 1
        provenance = RagProvenance(
            correlation_status=CorrelationStatus.LINKED,
            correlation_trust=CorrelationTrust.VERIFIED_WORKLOAD,
            missing_provenance=False,
            index_name="kb-main",
            index_version=None,
            namespace="articles",
            deployed_revision="kb-rag-00042-abc",
            prompt_template_id="answer.v7",
            prompt_template_sha256=DIGEST_A,
            response_sha256=DIGEST_B,
            observed_chunks=[
                ObservedChunkRef(
                    observed_vector_id="vec-1",
                    article_id="article-100",
                    content_sha256=DIGEST_C,
                    chunk_ordinal=3,
                    namespace="articles",
                    score=0.8125,
                )
            ],
        )
        verified = RagEvidenceRecord(
            evidence_reference="ref-verified",
            evidence_digest=DIGEST_A,
            source_collection=EvidenceSourceCollection.TICKET_EXECUTIONS,
            schema_version=1,
            occurred_at=T0,
            endpoint="/answer",
            route="ticket_answer",
            correlation_source="ticket_execution_hmac" if self.verified else None,
            correlation_trust=(
                CorrelationTrust.VERIFIED_WORKLOAD if self.verified else CorrelationTrust.NONE
            ),
            internal_job_id="job-7",
            request_id_hash=DIGEST_B,
            model="claude-opus-5",
            provider="anthropic",
            config_version="cfg-12",
            rendered_prompt_trace_sha256=DIGEST_C,
            deployed_commit_sha="0" * 40,
            provenance=provenance,
            source_article_ids=["article-100", "article-101"],
            duration_ms=1234.5,
            failed=False,
            missing=[MissingProvenance.INDEX_VERSION],
        )
        suggestion = RagEvidenceRecord(
            evidence_reference="ref-suggested",
            evidence_digest=DIGEST_B,
            source_collection=EvidenceSourceCollection.EXECUTION_LOGS,
            schema_version=0,
            occurred_at=T0,
            correlation_trust=CorrelationTrust.NONE,
            provenance=RagProvenance(),
            missing=[MissingProvenance.LEGACY_SCHEMA],
        )
        return RagEvidenceEnvelope(
            correlation_status=(
                CorrelationStatus.LINKED if self.verified else CorrelationStatus.UNAVAILABLE
            ),
            records=[verified, suggestion],
            result_digest=DIGEST_C,
            key_versions_queried=[1],
        )


def _detail_of(harness, ref: str = SYNTHETIC_DISPLAY_ID, **kwargs):
    return harness.client.get(f"{API_PREFIX}/tickets/{ref}", headers=_auth_headers(**kwargs))


# =====================================================================
# 1 — the document a deep link serves
# =====================================================================


class TestDeepLink:

    def test_a_deep_link_serves_the_same_document_as_the_list(self, monkeypatch):
        harness = _harness(monkeypatch)
        index = harness.client.get("/tickets", headers=_auth_headers())
        deep = harness.client.get(f"/tickets/{SYNTHETIC_DISPLAY_ID}", headers=_auth_headers())
        assert index.status_code == deep.status_code == 200
        assert deep.text == index.text

    def test_the_document_never_reflects_the_identifier_it_was_asked_for(self, monkeypatch):
        """The path is a selection the script reads, not content the server echoes.

        Reflecting it would make a shared link a stored-text channel into a page
        that renders remote strings for a living.
        """
        harness = _harness(monkeypatch)
        response = harness.client.get(
            "/tickets/TKT-999999<script>alert(1)</script>", headers=_auth_headers()
        )
        assert response.status_code in {200, 404}
        assert "999999" not in response.text
        assert "<script>alert" not in response.text

    def test_the_document_and_its_assets_are_never_cached(self, monkeypatch):
        harness = _harness(monkeypatch)
        for path in ("/tickets", f"/tickets/{SYNTHETIC_DISPLAY_ID}", "/tickets/assets/detail.js"):
            response = harness.client.get(path, headers=_auth_headers())
            assert response.status_code == 200, path
            assert response.headers["Cache-Control"] == "no-store", path

    def test_every_module_the_workspace_loads_is_served(self, monkeypatch):
        harness = _harness(monkeypatch)
        for name in (
            "app.js",
            "api.js",
            "state.js",
            "render.js",
            "detail.js",
            "evaluation.js",
            "conversation.js",
            "evidence.js",
            "remediation.js",
            "tickets.css",
        ):
            response = harness.client.get(f"/tickets/assets/{name}", headers=_auth_headers())
            assert response.status_code == 200, name

    def test_an_asset_still_needs_a_verified_identity(self, monkeypatch):
        harness = _harness(monkeypatch)
        assert harness.client.get("/tickets/assets/detail.js").status_code == 401


# =====================================================================
# 2 — the detail envelope the workspace hydrates from
# =====================================================================


class TestDetailEnvelope:

    def test_the_envelope_carries_every_part_the_workspace_reads(self, monkeypatch):
        harness = _harness(monkeypatch, broker=_RichBroker())
        _create_review(harness)
        body = _detail_of(harness).json()
        assert set(body) >= {
            "ticket_ref",
            "ticket",
            "review",
            "timeline",
            "evidence",
            "partial",
            "cache_state",
            "warnings",
            "diagnostics",
        }
        assert body["timeline"]["messages"], "the classified projection is what the UI renders"
        assert body["review"]["version"] == 1

    def test_a_ticket_with_no_review_is_null_and_not_a_fabricated_one(self, monkeypatch):
        """"Not imported yet" and "imported and unreviewed" are different facts."""
        harness = _harness(monkeypatch)
        assert _detail_of(harness).json()["review"] is None

    def test_a_live_data_outage_still_returns_the_review_and_says_it_is_partial(
        self, monkeypatch
    ):
        from data_pipeline.devrev_client import DevRevTransientError

        harness = _harness(monkeypatch)
        _create_review(harness)
        harness.devrev._get_error = DevRevTransientError("upstream down")
        body = _detail_of(harness).json()
        assert body["partial"] is True
        assert body["review"] is not None
        assert body["ticket"] is None
        # No conversation rather than an empty one: the difference is whether the
        # ticket has no messages or whether we could not read them.
        assert body["timeline"] is None
        assert "devrev_unavailable" in body["warnings"]

    def test_an_unknown_ticket_is_a_uniform_not_found(self, monkeypatch):
        from data_pipeline.devrev_client import DevRevNotFoundError

        harness = _harness(monkeypatch, devrev=_FakeDevRev(get_error=DevRevNotFoundError("no")))
        response = _detail_of(harness)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == CODE_NOT_FOUND

    def test_a_reference_that_is_a_url_never_reaches_the_adapter(self, monkeypatch):
        """The reference is an identifier, never something to fetch."""
        harness = _harness(monkeypatch)
        # A reference containing a slash 404s on *routing*, before the validator
        # is reached, so the interesting cases are the ones that get that far.
        for hostile in ("TKT-1 2", "TKT-1..2", "don:core:x:ticket-1 2"):
            response = harness.client.get(
                f"{API_PREFIX}/tickets/{hostile}", headers=_auth_headers()
            )
            assert response.status_code == 422, hostile
            assert response.json()["error"]["code"] == CODE_VALIDATION_FAILED

    def test_the_envelope_publishes_no_upstream_link_to_follow(self, monkeypatch):
        """Nothing to trust, so the workspace falls back to copying the id.

        If a validated absolute link is ever added it has to arrive with the host
        it is allowed to use; this pins the current state so adding one is a
        deliberate, tested change rather than a field that quietly appears.
        """
        harness = _harness(monkeypatch)
        body = _detail_of(harness).json()
        assert "devrev_url" not in body
        assert "devrev_url" not in (body["ticket"] or {})


# =====================================================================
# 3 — the conversation pages forward, and only forward
# =====================================================================


class TestConversationPaging:

    def _paging_harness(self, monkeypatch):
        return _harness(monkeypatch, devrev=_PagingDevRev(_three_conversation_pages()))

    def test_the_first_page_arrives_inside_the_detail_envelope(self, monkeypatch):
        harness = self._paging_harness(monkeypatch)
        timeline = _detail_of(harness).json()["timeline"]
        assert len(timeline["messages"]) == 2
        assert timeline["next_cursor"], "a sealed forward token"
        assert timeline["prev_cursor"] is None

    def test_a_later_page_is_fetched_with_the_cursor_in_a_header(self, monkeypatch):
        harness = self._paging_harness(monkeypatch)
        first = _detail_of(harness).json()["timeline"]
        second = harness.client.get(
            f"{API_PREFIX}/tickets/{SYNTHETIC_DISPLAY_ID}/timeline",
            headers=_auth_headers(**{CURSOR_HEADER: first["next_cursor"]}),
        )
        assert second.status_code == 200, second.text
        body = second.json()
        assert len(body["messages"]) == 3
        assert body["next_cursor"] != first["next_cursor"]

    def test_an_empty_page_that_still_has_a_cursor_is_not_the_end(self, monkeypatch):
        """Three pages, and the third is empty but continues.

        A workspace that stopped here would silently show two thirds of a
        conversation and label it complete.
        """
        harness = self._paging_harness(monkeypatch)
        cursor = _detail_of(harness).json()["timeline"]["next_cursor"]
        seen = 0
        for _ in range(3):
            page = harness.client.get(
                f"{API_PREFIX}/tickets/{SYNTHETIC_DISPLAY_ID}/timeline",
                headers=_auth_headers(**{CURSOR_HEADER: cursor}),
            ).json()
            seen += len(page["messages"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert seen == 3
        assert cursor is not None, "the last page offers a cursor with no items"

    def test_a_cursor_in_the_url_is_refused(self, monkeypatch):
        harness = self._paging_harness(monkeypatch)
        cursor = _detail_of(harness).json()["timeline"]["next_cursor"]
        response = harness.client.get(
            f"{API_PREFIX}/tickets/{SYNTHETIC_DISPLAY_ID}/timeline?cursor={cursor}",
            headers=_auth_headers(),
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == CODE_VALIDATION_FAILED

    def test_a_conversation_cursor_does_not_work_on_another_ticket(self, monkeypatch):
        """The token is sealed to its subject and its ticket."""
        harness = self._paging_harness(monkeypatch)
        cursor = _detail_of(harness).json()["timeline"]["next_cursor"]
        response = harness.client.get(
            f"{API_PREFIX}/tickets/TKT-111111/timeline",
            headers=_auth_headers(**{CURSOR_HEADER: cursor}),
        )
        assert response.status_code == 422

    def test_the_five_author_classes_all_arrive_classified(self, monkeypatch):
        """The workspace renders `actor_class`; this proves the server sets it.

        A participant, a human agent, the assistant and a change event have to
        come back as four different classes from one page, because the interface's
        whole safety story on that panel is that it does not have to guess.
        """
        harness = self._paging_harness(monkeypatch)
        first = _detail_of(harness).json()["timeline"]["messages"]
        second = harness.client.get(
            f"{API_PREFIX}/tickets/{SYNTHETIC_DISPLAY_ID}/timeline",
            headers=_auth_headers(
                **{CURSOR_HEADER: _detail_of(harness).json()["timeline"]["next_cursor"]}
            ),
        ).json()["messages"]
        classes = {message["actor_class"] for message in first + second}
        assert {"participant", "human_agent", "ai_or_system", "event"} <= classes
        for message in first + second:
            assert message["basis"], "a classification with no stated basis"

    def test_an_internal_entry_is_never_marked_participant_facing(self, monkeypatch):
        harness = self._paging_harness(monkeypatch)
        messages = _detail_of(harness).json()["timeline"]["messages"]
        internal = [message for message in messages if message["internal"]]
        assert internal, "the fixture carries an internal note"
        for message in internal:
            assert message["participant_facing"] is False

    def test_an_unmodelled_entry_carries_no_body_to_render(self, monkeypatch):
        harness = self._paging_harness(monkeypatch)
        cursor = _detail_of(harness).json()["timeline"]["next_cursor"]
        messages = harness.client.get(
            f"{API_PREFIX}/tickets/{SYNTHETIC_DISPLAY_ID}/timeline",
            headers=_auth_headers(**{CURSOR_HEADER: cursor}),
        ).json()["messages"]
        placeholders = [m for m in messages if m["rendering"] == "placeholder"]
        assert placeholders
        for message in placeholders:
            assert message["body"] is None
            assert message["entry_id"], "something a support request can name"


# =====================================================================
# 4 — evidence: the fields Stage 7 renders have to survive the round trip
# =====================================================================


class TestEvidenceEnvelope:

    #: Every field the evidence panel renders off an execution record.
    RENDERED = (
        "endpoint",
        "route",
        "model",
        "provider",
        "config_version",
        "internal_job_id",
        "request_id_hash",
        "rendered_prompt_trace_sha256",
        "deployed_commit_sha",
        "occurred_at",
        "duration_ms",
        "failed",
        "source_article_ids",
        "missing",
        "evidence_reference",
        "evidence_digest",
        "schema_version",
    )

    def test_the_summary_carries_whole_execution_records(self, monkeypatch):
        """The retrieval half alone cannot answer "was this answer any good".

        Before Stage 7 the service projected each record down to its
        ``provenance`` and dropped the rest, so which model on which route
        answered — and whether it failed — never reached the reviewer.
        """
        harness = _harness(monkeypatch, broker=_RichBroker())
        _create_review(harness)
        evidence = _detail_of(harness).json()["evidence"]
        assert evidence["executions"], "no execution records reached the client"
        assert len(evidence["executions"]) == len(evidence["provenance"])

    @pytest.mark.parametrize("field", RENDERED)
    def test_each_rendered_field_is_present_on_the_wire(self, monkeypatch, field):
        harness = _harness(monkeypatch, broker=_RichBroker())
        _create_review(harness)
        record = _detail_of(harness).json()["evidence"]["executions"][0]
        assert field in record, field

    def test_the_retrieval_side_still_arrives_too(self, monkeypatch):
        harness = _harness(monkeypatch, broker=_RichBroker())
        _create_review(harness)
        record = _detail_of(harness).json()["evidence"]["executions"][0]
        provenance = record["provenance"]
        for field in (
            "index_name",
            "index_version",
            "namespace",
            "deployed_revision",
            "prompt_template_id",
            "prompt_template_sha256",
            "response_sha256",
            "observed_chunks",
        ):
            assert field in provenance, field
        chunk = provenance["observed_chunks"][0]
        assert set(chunk) == {
            "observed_vector_id",
            "article_id",
            "content_sha256",
            "chunk_ordinal",
            "namespace",
            "score",
        }

    def test_an_absent_index_version_arrives_as_null_and_is_named_as_missing(
        self, monkeypatch
    ):
        """A gap the server knows about, said twice: as a null and as a name."""
        harness = _harness(monkeypatch, broker=_RichBroker())
        _create_review(harness)
        record = _detail_of(harness).json()["evidence"]["executions"][0]
        assert record["provenance"]["index_version"] is None
        assert "index_version" in record["missing"]

    def test_no_prompt_response_or_chunk_text_is_ever_returned(self, monkeypatch):
        """The record is the sanitized boundary shape, and this proves it stayed one."""
        harness = _harness(monkeypatch, broker=_RichBroker())
        _create_review(harness)
        serialized = _detail_of(harness).text
        for forbidden in (
            "chunk_text",
            "chunk_content",
            "page_content",
            "prompt_text",
            "response_text",
            "completion",
        ):
            assert forbidden not in serialized, forbidden

    def test_a_broker_outage_is_a_named_gap_and_not_a_failure(self, monkeypatch):
        from tests.test_ticket_review_routes import _FailingBroker

        harness = _harness(monkeypatch, broker=_FailingBroker())
        _create_review(harness)
        response = _detail_of(harness)
        assert response.status_code == 200
        evidence = response.json()["evidence"]
        assert evidence["correlation_status"] == "unavailable"
        assert evidence["unavailable_reason"]
        assert evidence["executions"] == []

    def test_no_broker_at_all_says_so_differently(self, monkeypatch):
        """Two gaps with two causes: an old ticket, or an unconfigured service."""
        harness = _harness(monkeypatch, broker=None)
        _create_review(harness)
        evidence = _detail_of(harness).json()["evidence"]
        assert evidence["unavailable_reason"] == "evidence_broker_not_configured"
        assert evidence["broker_available"] is False

    def test_a_suggestion_arrives_as_a_sealed_token_and_never_an_execution_id(
        self, monkeypatch
    ):
        harness = _harness(monkeypatch, broker=_RichBroker(verified=False))
        _create_review(harness)
        evidence = _detail_of(harness).json()["evidence"]
        assert evidence["candidate_links"], "nothing verified, so both records are suggestions"
        candidate = evidence["candidate_links"][0]
        assert candidate["correlation_trust"] == "candidate"
        assert candidate["candidate_token"]
        assert candidate["expires_at"]
        # Nothing the client could edit into another ticket's evidence.
        assert "execution_id" not in candidate
        assert "document_path" not in candidate

    def test_a_forged_suggestion_token_is_refused(self, monkeypatch):
        harness = _harness(monkeypatch, broker=_RichBroker(verified=False))
        review = _create_review(harness)
        response = harness.client.post(
            f"{API_PREFIX}/reviews/{review['review_id']}/evidence-links",
            headers=_write_headers(
                harness.client, idempotency="idem-forged-00001", **{"If-Match": '"v1"'}
            ),
            json={"broker_candidate_token": "forged", "reason": "it looks right"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "EVIDENCE_LINK_REJECTED"

    def test_a_suggestion_can_be_confirmed_and_becomes_a_reasoned_link(self, monkeypatch):
        """The whole point of the candidate flow, end to end."""
        harness = _harness(monkeypatch, broker=_RichBroker(verified=False))
        review = _create_review(harness)
        candidate = _detail_of(harness).json()["evidence"]["candidate_links"][0]
        current = harness.client.get(
            f"{API_PREFIX}/reviews/{review['review_id']}", headers=_auth_headers()
        ).json()
        response = harness.client.post(
            f"{API_PREFIX}/reviews/{review['review_id']}/evidence-links",
            headers=_write_headers(
                harness.client,
                idempotency="idem-confirm-0001",
                **{"If-Match": f'"v{current["version"]}"'},
            ),
            json={
                "broker_candidate_token": candidate["candidate_token"],
                "reason": "The timestamps and the article match the participant's question.",
            },
        )
        assert response.status_code == 201, response.text
        # The write advances the version by more than one: linking the evidence
        # and recording the resulting correlation status are two audited writes
        # chained by the service. What the workspace needs is that the returned
        # ETag is the version it must send next, not that it advanced by exactly
        # one — an assertion of +1 would encode a coincidence.
        assert response.headers["ETag"] == f'"v{response.json()["version"]}"'
        assert response.json()["version"] > current["version"]
        assert response.json()["correlation_status"] == "manual"
        links = harness.client.get(
            f"{API_PREFIX}/reviews/{review['review_id']}/evidence-links",
            headers=_auth_headers(),
        ).json()
        assert len(links["items"]) == 1
        link = links["items"][0]
        assert link["reason"].startswith("The timestamps")
        assert link["correlation_trust"] == "manual_reviewer"
        assert link["linked_by"]["email"]

    def test_unlinking_needs_the_current_version_and_a_reason(self, monkeypatch):
        harness = _harness(monkeypatch, broker=_RichBroker(verified=False))
        review = _create_review(harness)
        candidate = _detail_of(harness).json()["evidence"]["candidate_links"][0]
        linked = harness.client.post(
            f"{API_PREFIX}/reviews/{review['review_id']}/evidence-links",
            headers=_write_headers(
                harness.client, idempotency="idem-link-000001", **{"If-Match": '"v1"'}
            ),
            json={"broker_candidate_token": candidate["candidate_token"], "reason": "matches"},
        )
        assert linked.status_code == 201, linked.text
        link_id = harness.client.get(
            f"{API_PREFIX}/reviews/{review['review_id']}/evidence-links",
            headers=_auth_headers(),
        ).json()["items"][0]["link_id"]

        missing_precondition = harness.client.request(
            "DELETE",
            f"{API_PREFIX}/reviews/{review['review_id']}/evidence-links/{link_id}",
            headers=_write_headers(harness.client, idempotency="idem-unlink-0001"),
            json={"reason": "wrong ticket"},
        )
        assert missing_precondition.status_code == 428
        assert missing_precondition.json()["error"]["code"] == CODE_PRECONDITION_REQUIRED

        no_reason = harness.client.request(
            "DELETE",
            f"{API_PREFIX}/reviews/{review['review_id']}/evidence-links/{link_id}",
            headers=_write_headers(
                harness.client, idempotency="idem-unlink-0002", **{"If-Match": '"v2"'}
            ),
            json={},
        )
        assert no_reason.status_code == 422


# =====================================================================
# 5 — the audit ledger the history panel renders
# =====================================================================


class TestAuditLedger:

    def test_the_ledger_returns_one_bounded_page_and_no_forward_token(self, monkeypatch):
        """Which is why the workspace must not build a pager for it."""
        harness = _harness(monkeypatch)
        review = _create_review(harness)
        page = harness.client.get(
            f"{API_PREFIX}/reviews/{review['review_id']}/audit-events", headers=_auth_headers()
        )
        assert page.status_code == 200
        assert page.json()["next_cursor"] is None

    def test_each_event_carries_what_the_timeline_shows(self, monkeypatch):
        harness = _harness(monkeypatch)
        review = _create_review(harness)
        harness.client.patch(
            f"{API_PREFIX}/reviews/{review['review_id']}",
            headers=_write_headers(
                harness.client, idempotency="idem-patch-000001", **{"If-Match": '"v1"'}
            ),
            json={"topic": "Contributions", "rating": 4},
        )
        events = harness.client.get(
            f"{API_PREFIX}/reviews/{review['review_id']}/audit-events", headers=_auth_headers()
        ).json()["items"]
        assert len(events) >= 2
        for event in events:
            for field in (
                "event_type",
                "actor_subject_hash",
                "occurred_at_unix_us",
                "changed_fields",
                "previous_event_hash",
                "event_hash",
            ):
                assert field in event, field

    def test_the_chain_links_and_starts_at_genesis(self, monkeypatch):
        """The exact property the history panel checks in the browser."""
        harness = _harness(monkeypatch)
        review = _create_review(harness)
        for index, patch in enumerate(({"topic": "A"}, {"comments": "B"}, {"rating": 5})):
            response = harness.client.patch(
                f"{API_PREFIX}/reviews/{review['review_id']}",
                headers=_write_headers(
                    harness.client,
                    idempotency=f"idem-chain-{index:06d}",
                    **{"If-Match": f'"v{index + 1}"'},
                ),
                json=patch,
            )
            assert response.status_code == 200, response.text
        events = sorted(
            harness.client.get(
                f"{API_PREFIX}/reviews/{review['review_id']}/audit-events",
                headers=_auth_headers(),
            ).json()["items"],
            key=lambda event: event["occurred_at_unix_us"],
        )
        assert events[0]["previous_event_hash"] == "0" * 64
        for parent, child in zip(events, events[1:]):
            assert child["previous_event_hash"] == parent["event_hash"]

    def test_the_ledger_records_which_fields_changed_and_not_their_contents(
        self, monkeypatch
    ):
        """So the panel is right not to reconstruct an old comment."""
        harness = _harness(monkeypatch)
        review = _create_review(harness)
        harness.client.patch(
            f"{API_PREFIX}/reviews/{review['review_id']}",
            headers=_write_headers(
                harness.client, idempotency="idem-secret-0001", **{"If-Match": '"v1"'}
            ),
            json={"comments": "a-distinctive-comment-body"},
        )
        page = harness.client.get(
            f"{API_PREFIX}/reviews/{review['review_id']}/audit-events", headers=_auth_headers()
        )
        assert "a-distinctive-comment-body" not in page.text
        assert "comments" in page.json()["items"][-1]["changed_fields"]

    def test_the_ledger_needs_only_a_viewer(self, monkeypatch):
        harness = _harness(monkeypatch, role=ReviewerRole.VIEWER)
        review_id = review_id_for_devrev_work(SYNTHETIC_DON)
        response = harness.client.get(
            f"{API_PREFIX}/reviews/{review_id}/audit-events", headers=_auth_headers()
        )
        # 404 because no review exists for a viewer-only harness; the point is
        # that reading history is not gated behind the write role.
        assert response.status_code == 404


# =====================================================================
# 6 — concurrency, and the three answers that must stay distinct
# =====================================================================


class TestVersionedSaves:

    def test_a_save_returns_the_next_version_as_a_quoted_etag(self, monkeypatch):
        harness = _harness(monkeypatch)
        review = _create_review(harness)
        response = harness.client.patch(
            f"{API_PREFIX}/reviews/{review['review_id']}",
            headers=_write_headers(
                harness.client, idempotency="idem-save-000001", **{"If-Match": '"v1"'}
            ),
            json={"topic": "Withdrawals"},
        )
        assert response.status_code == 200
        assert response.headers["ETag"] == '"v2"'
        assert response.json()["version"] == 2

    def test_a_missing_precondition_is_428_and_means_a_client_bug(self, monkeypatch):
        harness = _harness(monkeypatch)
        review = _create_review(harness)
        response = harness.client.patch(
            f"{API_PREFIX}/reviews/{review['review_id']}",
            headers=_write_headers(harness.client, idempotency="idem-no-match-01"),
            json={"topic": "Loans"},
        )
        assert response.status_code == 428
        assert response.json()["error"]["code"] == CODE_PRECONDITION_REQUIRED

    def test_a_malformed_precondition_is_422_and_not_a_conflict(self, monkeypatch):
        harness = _harness(monkeypatch)
        review = _create_review(harness)
        for bad in ("v1", '"1"', "*", 'W/"v1"'):
            response = harness.client.patch(
                f"{API_PREFIX}/reviews/{review['review_id']}",
                headers=_write_headers(
                    harness.client, idempotency="idem-bad-match-1", **{"If-Match": bad}
                ),
                json={"topic": "Loans"},
            )
            assert response.status_code == 422, bad
            assert response.json()["error"]["code"] == CODE_PRECONDITION_MALFORMED

    def test_a_stale_save_is_412_and_carries_what_the_panel_needs(self, monkeypatch):
        """The exact payload the conflict panel is built around.

        Without ``current_version`` the panel cannot say what the reviewer is
        choosing between, and without ``changed_at`` it cannot say when.
        """
        harness = _harness(monkeypatch)
        review = _create_review(harness)
        first = harness.client.patch(
            f"{API_PREFIX}/reviews/{review['review_id']}",
            headers=_write_headers(
                harness.client, idempotency="idem-first-00001", **{"If-Match": '"v1"'}
            ),
            json={"topic": "Enrollment"},
        )
        assert first.status_code == 200
        stale = harness.client.patch(
            f"{API_PREFIX}/reviews/{review['review_id']}",
            headers=_write_headers(
                harness.client, idempotency="idem-stale-00001", **{"If-Match": '"v1"'}
            ),
            json={"comments": "written against the version I loaded"},
        )
        assert stale.status_code == 412
        error = stale.json()["error"]
        assert error["code"] == CODE_REVIEW_VERSION_CONFLICT
        assert error["current_version"] == 2
        assert error["changed_at"]

    def test_a_stale_save_changes_nothing(self, monkeypatch):
        """The refusal has to be total: a partial write would be the worst answer."""
        harness = _harness(monkeypatch)
        review = _create_review(harness)
        harness.client.patch(
            f"{API_PREFIX}/reviews/{review['review_id']}",
            headers=_write_headers(
                harness.client, idempotency="idem-win-0000001", **{"If-Match": '"v1"'}
            ),
            json={"topic": "Kept"},
        )
        harness.client.patch(
            f"{API_PREFIX}/reviews/{review['review_id']}",
            headers=_write_headers(
                harness.client, idempotency="idem-lose-000001", **{"If-Match": '"v1"'}
            ),
            json={"topic": "Lost", "comments": "Lost too"},
        )
        current = harness.client.get(
            f"{API_PREFIX}/reviews/{review['review_id']}", headers=_auth_headers()
        ).json()
        assert current["topic"] == "Kept"
        assert current["comments"] is None
        assert current["version"] == 2

    def test_a_patch_only_touches_the_fields_it_names(self, monkeypatch):
        """Which is why the form sends changed fields only."""
        harness = _harness(monkeypatch)
        review = _create_review(harness)
        harness.client.patch(
            f"{API_PREFIX}/reviews/{review['review_id']}",
            headers=_write_headers(
                harness.client, idempotency="idem-topic-00001", **{"If-Match": '"v1"'}
            ),
            json={"topic": "Contributions", "comments": "Original comment"},
        )
        harness.client.patch(
            f"{API_PREFIX}/reviews/{review['review_id']}",
            headers=_write_headers(
                harness.client, idempotency="idem-rating-0001", **{"If-Match": '"v2"'}
            ),
            json={"rating": 3},
        )
        current = harness.client.get(
            f"{API_PREFIX}/reviews/{review['review_id']}", headers=_auth_headers()
        ).json()
        assert current["comments"] == "Original comment"
        assert current["rating"] == 3

    def test_an_explicit_null_clears_a_field(self, monkeypatch):
        """A present key is a value to write. The form relies on this both ways."""
        harness = _harness(monkeypatch)
        review = _create_review(harness)
        harness.client.patch(
            f"{API_PREFIX}/reviews/{review['review_id']}",
            headers=_write_headers(
                harness.client, idempotency="idem-set-0000001", **{"If-Match": '"v1"'}
            ),
            json={"topic": "Something"},
        )
        harness.client.patch(
            f"{API_PREFIX}/reviews/{review['review_id']}",
            headers=_write_headers(
                harness.client, idempotency="idem-clear-000001", **{"If-Match": '"v2"'}
            ),
            json={"topic": None},
        )
        current = harness.client.get(
            f"{API_PREFIX}/reviews/{review['review_id']}", headers=_auth_headers()
        ).json()
        assert current["topic"] is None

    def test_a_field_over_its_bound_is_refused(self, monkeypatch):
        """So the counter is a courtesy and the server is the authority."""
        from api.ticket_review_models import MAX_COMMENTS_LENGTH

        harness = _harness(monkeypatch)
        review = _create_review(harness)
        response = harness.client.patch(
            f"{API_PREFIX}/reviews/{review['review_id']}",
            headers=_write_headers(
                harness.client, idempotency="idem-toolong-001", **{"If-Match": '"v1"'}
            ),
            json={"comments": "x" * (MAX_COMMENTS_LENGTH + 1)},
        )
        assert response.status_code == 422


# =====================================================================
# 7 — the transition table, from the outside
# =====================================================================


class TestTransitions:

    def _at(self, harness, review_id: str, statuses: list[str], start: int = 1) -> int:
        version = start
        for index, status in enumerate(statuses):
            response = harness.client.patch(
                f"{API_PREFIX}/reviews/{review_id}",
                headers=_write_headers(
                    harness.client,
                    idempotency=f"idem-walk-{index:06d}",
                    **{"If-Match": f'"v{version}"'},
                ),
                json={"status": status},
            )
            assert response.status_code == 200, f"{status}: {response.text}"
            version = response.json()["version"]
        return version

    def test_a_move_the_table_forbids_is_refused(self, monkeypatch):
        """Which is why the form offers only the reachable ones."""
        harness = _harness(monkeypatch)
        review = _create_review(harness)
        response = harness.client.patch(
            f"{API_PREFIX}/reviews/{review['review_id']}",
            headers=_write_headers(
                harness.client, idempotency="idem-illegal-001", **{"If-Match": '"v1"'}
            ),
            json={"status": "resolved"},
        )
        assert response.status_code == 409

    def test_closing_without_a_defensible_resolution_is_refused(self, monkeypatch):
        harness = _harness(monkeypatch)
        review = _create_review(harness)
        version = self._at(
            harness,
            review["review_id"],
            ["reviewed", "triaged", "planned", "in_progress", "changes_proposed", "verifying"],
        )
        bare = harness.client.patch(
            f"{API_PREFIX}/reviews/{review['review_id']}",
            headers=_write_headers(
                harness.client, idempotency="idem-bare-000001", **{"If-Match": f'"v{version}"'}
            ),
            json={"status": "resolved"},
        )
        assert bare.status_code == 409

    def test_closing_with_a_written_rationale_is_accepted(self, monkeypatch):
        """The only route a browser has: no machine evidence, so a reason in words.

        This is what the resolution fieldset collects, and it is the reason the
        rationale is required rather than optional there.
        """
        harness = _harness(monkeypatch)
        review = _create_review(harness)
        version = self._at(
            harness,
            review["review_id"],
            ["reviewed", "triaged", "planned", "in_progress", "changes_proposed", "verifying"],
        )
        closed = harness.client.patch(
            f"{API_PREFIX}/reviews/{review['review_id']}",
            headers=_write_headers(
                harness.client, idempotency="idem-close-00001", **{"If-Match": f'"v{version}"'}
            ),
            json={
                "status": "resolved",
                "resolution": {
                    "outcome": "no_change",
                    "verification_summary": "Re-ran the question against the current index.",
                    "no_change_reason": "The answer is correct under the current knowledge base.",
                },
            },
        )
        assert closed.status_code == 200, closed.text
        body = closed.json()
        assert body["status"] == "resolved"
        assert body["resolved_at"]

    def test_a_closed_review_refuses_an_ordinary_reopen(self, monkeypatch):
        harness = _harness(monkeypatch, role=ReviewerRole.REVIEWER)
        review = _create_review(harness)
        version = self._at(
            harness,
            review["review_id"],
            ["reviewed", "triaged", "planned", "in_progress", "changes_proposed", "verifying"],
        )
        closed = harness.client.patch(
            f"{API_PREFIX}/reviews/{review['review_id']}",
            headers=_write_headers(
                harness.client, idempotency="idem-close-00002", **{"If-Match": f'"v{version}"'}
            ),
            json={
                "status": "resolved",
                "resolution": {"outcome": "no_change", "no_change_reason": "correct as is"},
            },
        )
        assert closed.status_code == 200, closed.text
        version = closed.json()["version"]
        response = harness.client.patch(
            f"{API_PREFIX}/reviews/{review['review_id']}",
            headers=_write_headers(
                harness.client, idempotency="idem-reopen-0001", **{"If-Match": f'"v{version}"'}
            ),
            json={"status": "triaged"},
        )
        assert response.status_code == 409

    def test_only_an_admin_may_reopen_and_only_onto_triaged(self, monkeypatch):
        harness = _harness(monkeypatch, role=ReviewerRole.ADMIN)
        review = _create_review(harness)
        version = self._at(
            harness,
            review["review_id"],
            ["reviewed", "triaged", "planned", "in_progress", "changes_proposed", "verifying"],
        )
        closed = harness.client.patch(
            f"{API_PREFIX}/reviews/{review['review_id']}",
            headers=_write_headers(
                harness.client, idempotency="idem-close-00003", **{"If-Match": f'"v{version}"'}
            ),
            json={
                "status": "resolved",
                "resolution": {"outcome": "no_change", "no_change_reason": "correct as is"},
            },
        )
        version = closed.json()["version"]
        reopened = harness.client.patch(
            f"{API_PREFIX}/reviews/{review['review_id']}?admin_reopen=true",
            headers=_write_headers(
                harness.client, idempotency="idem-reopen-0002", **{"If-Match": f'"v{version}"'}
            ),
            json={"status": "triaged"},
        )
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["status"] == "triaged"
        assert reopened.json()["resolved_at"] is None


# =====================================================================
# 8 — what each role may do
# =====================================================================


class TestRoles:

    def test_a_viewer_can_read_the_whole_workspace(self, monkeypatch):
        harness = _harness(monkeypatch, role=ReviewerRole.VIEWER)
        assert _detail_of(harness).status_code == 200
        assert (
            harness.client.get(
                f"{API_PREFIX}/tickets/{SYNTHETIC_DISPLAY_ID}/timeline", headers=_auth_headers()
            ).status_code
            == 200
        )

    def test_a_viewer_cannot_save(self, monkeypatch):
        harness = _harness(monkeypatch, role=ReviewerRole.VIEWER)
        review_id = review_id_for_devrev_work(SYNTHETIC_DON)
        response = harness.client.patch(
            f"{API_PREFIX}/reviews/{review_id}",
            headers=_write_headers(
                harness.client, idempotency="idem-viewer-0001", **{"If-Match": '"v1"'}
            ),
            json={"topic": "nope"},
        )
        assert response.status_code == 403

    def test_a_viewer_cannot_import_a_ticket(self, monkeypatch):
        harness = _harness(monkeypatch, role=ReviewerRole.VIEWER)
        response = harness.client.post(
            f"{API_PREFIX}/tickets/{SYNTHETIC_DISPLAY_ID}/review",
            headers=_write_headers(harness.client, idempotency="idem-viewer-0002"),
            json={},
        )
        assert response.status_code == 403

    def test_the_first_save_imports_and_then_the_patch_applies(self, monkeypatch):
        """The two-call sequence the form performs on an unimported ticket.

        The import route accepts the four spreadsheet fields; everything else has
        to travel as a patch against the version the import returned.
        """
        harness = _harness(monkeypatch)
        created = harness.client.post(
            f"{API_PREFIX}/tickets/{SYNTHETIC_DISPLAY_ID}/review",
            headers=_write_headers(harness.client, idempotency="idem-import-0001"),
            json={"topic": "Contributions", "legacy_type": "Answer quality", "rating": 2},
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["topic"] == "Contributions"
        assert body["rating"] == 2
        patched = harness.client.patch(
            f"{API_PREFIX}/reviews/{body['review_id']}",
            headers=_write_headers(
                harness.client,
                idempotency="idem-import-0002",
                **{"If-Match": f'"v{body["version"]}"'},
            ),
            json={"observation_type": "retrieval_miss", "severity": "high", "status": "reviewed"},
        )
        assert patched.status_code == 200, patched.text
        final = patched.json()
        assert final["observation_type"] == "retrieval_miss"
        assert final["severity"] == "high"
        assert final["status"] == "reviewed"

    def test_importing_twice_with_one_key_creates_one_review(self, monkeypatch):
        harness = _harness(monkeypatch)
        first = harness.client.post(
            f"{API_PREFIX}/tickets/{SYNTHETIC_DISPLAY_ID}/review",
            headers=_write_headers(harness.client, idempotency="idem-double-0001"),
            json={},
        )
        second = harness.client.post(
            f"{API_PREFIX}/tickets/{SYNTHETIC_DISPLAY_ID}/review",
            headers=_write_headers(harness.client, idempotency="idem-double-0001"),
            json={},
        )
        assert first.status_code == 201
        assert second.status_code in {200, 201}
        assert second.json()["review_id"] == first.json()["review_id"]
        assert second.json()["version"] == first.json()["version"]

    def test_a_reviewer_may_take_an_unassigned_review(self, monkeypatch):
        harness = _harness(monkeypatch, role=ReviewerRole.REVIEWER)
        review = _create_review(harness)
        identity = harness.client.get(
            f"{API_PREFIX}/session", headers=_auth_headers()
        ).json()["identity"]
        response = harness.client.patch(
            f"{API_PREFIX}/reviews/{review['review_id']}",
            headers=_write_headers(
                harness.client, idempotency="idem-assign-0001", **{"If-Match": '"v1"'}
            ),
            json={"assigned_reviewer": identity},
        )
        assert response.status_code == 200, response.text
        assert response.json()["assigned_reviewer"]["email"] == identity["email"]

    def test_a_reviewer_may_not_hand_a_review_to_someone_else(self, monkeypatch):
        """Which is why the form offers self-assignment and not a picker."""
        harness = _harness(monkeypatch, role=ReviewerRole.REVIEWER)
        review = _create_review(harness)
        response = harness.client.patch(
            f"{API_PREFIX}/reviews/{review['review_id']}",
            headers=_write_headers(
                harness.client, idempotency="idem-assign-0002", **{"If-Match": '"v1"'}
            ),
            json={
                "assigned_reviewer": {
                    "subject": "accounts.google.com:someone-else",
                    "email": "someone-else@example.invalid",
                    "display_name": "Someone Else",
                }
            },
        )
        assert response.status_code == 403

    def test_an_assignment_can_be_released(self, monkeypatch):
        harness = _harness(monkeypatch, role=ReviewerRole.REVIEWER)
        review = _create_review(harness)
        identity = harness.client.get(
            f"{API_PREFIX}/session", headers=_auth_headers()
        ).json()["identity"]
        harness.client.patch(
            f"{API_PREFIX}/reviews/{review['review_id']}",
            headers=_write_headers(
                harness.client, idempotency="idem-assign-0003", **{"If-Match": '"v1"'}
            ),
            json={"assigned_reviewer": identity},
        )
        released = harness.client.patch(
            f"{API_PREFIX}/reviews/{review['review_id']}",
            headers=_write_headers(
                harness.client, idempotency="idem-assign-0004", **{"If-Match": '"v2"'}
            ),
            json={"assigned_reviewer": None},
        )
        assert released.status_code == 200, released.text
        assert released.json()["assigned_reviewer"] is None

    def test_the_session_tells_the_workspace_what_is_switched_off(self, monkeypatch):
        """The workspace branches on these rather than discovering a 404."""
        harness = _harness(monkeypatch)
        flags = harness.client.get(f"{API_PREFIX}/session", headers=_auth_headers()).json()[
            "feature_flags"
        ]
        assert flags["remediation_enabled"] is False
        assert flags["import_export_enabled"] is False
