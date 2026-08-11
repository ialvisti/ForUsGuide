"""Stage 7 Step 1 — the contract between the browser and the server.

The sibling file asserts what the shipped files *say*. This one asserts what the
server actually *does* when the review workspace talks to it, over the real
middleware stack, the real router, the real service and the real repository.
DevRev is faked so the test process never opens a production connection.

It exists because the workspace's hardest behaviours are all round trips, and
three of them cannot be reached from a local fixture browser at all:

*   a stale-version save has to come back **412** carrying the current version and
    the time it changed, so the conflict panel can describe the disagreement
    rather than guess at it;
*   **412**, **409** and **428** have to stay three different answers. If they ever
    collapse into one, the interface either loses a reviewer's text to a retry or
    reports a client bug as somebody else's edit;
*   only the RAG ingest boundary may create a review; the browser can update that
    record but cannot discover or manually add a DevRev ticket.

The wiring is imported from the Stage 5 route tests rather than rebuilt. A
second, hand-made harness is how two files end up disagreeing about what the app
under test is.
"""

from __future__ import annotations

import pytest

from api.ticket_review_models import ReviewerRole, review_id_for_devrev_work
from api.ticket_review_routes import (
    API_PREFIX,
    CODE_PRECONDITION_MALFORMED,
    CODE_PRECONDITION_REQUIRED,
    CODE_REVIEW_VERSION_CONFLICT,
)

from tests.test_ticket_review_routes import (
    SYNTHETIC_DISPLAY_ID,
    SYNTHETIC_DON,
    _auth_headers,
    _create_review,
    _harness,
    _ingest_execution,
    _write_headers,
)


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
            "structured.js",
            "answer-presentation.js",
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

    def test_a_viewer_can_read_a_rag_created_workspace(self, monkeypatch):
        harness = _harness(monkeypatch, role=ReviewerRole.VIEWER)
        _ingest_execution(harness)
        assert (
            harness.client.get(
                f"{API_PREFIX}/tickets/job123:0", headers=_auth_headers()
            ).status_code
            == 200
        )
        assert (
            harness.client.get(
                f"{API_PREFIX}/tickets/job123:0/timeline", headers=_auth_headers()
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

    @pytest.mark.parametrize(
        "role", [ReviewerRole.VIEWER, ReviewerRole.REVIEWER, ReviewerRole.ADMIN]
    )
    def test_no_role_can_manually_add_a_ticket(self, monkeypatch, role):
        harness = _harness(monkeypatch, role=role)
        response = harness.client.post(
            f"{API_PREFIX}/tickets/{SYNTHETIC_DISPLAY_ID}/review",
            headers=_write_headers(harness.client, idempotency="idem-viewer-0002"),
            json={},
        )
        assert response.status_code == 404

    def test_rag_ingest_creates_the_review_then_a_patch_applies(self, monkeypatch):
        harness = _harness(monkeypatch)
        body = _create_review(harness)
        patched = harness.client.patch(
            f"{API_PREFIX}/reviews/{body['review_id']}",
            headers=_write_headers(
                harness.client,
                idempotency="idem-rag-patch-02",
                **{"If-Match": f'"v{body["version"]}"'},
            ),
            json={"observation_type": "retrieval_miss", "severity": "high", "status": "reviewed"},
        )
        assert patched.status_code == 200, patched.text
        final = patched.json()
        assert final["observation_type"] == "retrieval_miss"
        assert final["severity"] == "high"
        assert final["status"] == "reviewed"

    def test_replaying_one_rag_execution_creates_one_review(self, monkeypatch):
        harness = _harness(monkeypatch)
        first = _ingest_execution(harness)
        second = _ingest_execution(harness)
        assert first.created is True
        assert second.created is False
        assert second.run.review_id == first.run.review_id

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
        assert "import_export_enabled" not in flags
