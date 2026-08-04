"""Stage 6 Step 7 — the fixture console is isolated, and it is realistic.

Two claims are worth testing here, and they pull in opposite directions.

**Isolated.** A fixture that can reach a real credential, the metadata service, a
real ticket system, or any non-loopback address is a browser test one typo away
from writing to production. The isolation is therefore verified rather than
described: the marker is checked, the credential environment is checked, and the
socket guard is exercised against a real connection attempt.

**Realistic.** A fixture that fakes its way past the product's own rules teaches
nothing. The durable store is the real repository over the in-memory backend, so
the seeded reviews had to walk the real status transition table, and every error
scenario travels the real service, router, and error envelope.
"""

from __future__ import annotations

import ipaddress
import os
import socket

import pytest
from fastapi.testclient import TestClient

from api.ticket_review_routes import API_PREFIX
from api.tickets_console_main import UI_ASSETS_DIRECTORY
from api.tickets_csrf import (
    CSRF_HEADER,
    CURSOR_HEADER,
    FETCH_SITE_HEADER,
    IDEMPOTENCY_HEADER,
    ORIGIN_HEADER,
)
from tests.support import tickets_console_fixture_app as fixture

CONSOLE_ORIGIN = fixture.CONSOLE_ORIGIN


@pytest.fixture
def client() -> TestClient:
    app = fixture.build_fixture_app()
    with TestClient(app, raise_server_exceptions=False) as started:
        yield started


def _write_headers(client: TestClient, *, idempotency: str) -> dict[str, str]:
    """The four headers a browser would supply, plus the token from the session.

    The two the browser owns — the request's origin and its fetch-metadata site —
    are set explicitly here because a test client is not a browser. Production
    code must never try: they are forbidden headers in a browser fetch.
    """
    token = client.get(f"{API_PREFIX}/session").json()["csrf_token"]
    return {
        ORIGIN_HEADER: CONSOLE_ORIGIN,
        FETCH_SITE_HEADER: "same-origin",
        CSRF_HEADER: token,
        IDEMPOTENCY_HEADER: idempotency,
        "Content-Type": "application/json",
    }


# =====================================================================
# Isolation
# =====================================================================


class TestIsolation:

    def test_importing_the_module_activates_fixture_mode(self):
        assert fixture.fixture_mode_active()
        assert os.environ[fixture.FIXTURE_MODE_ENV] == fixture.FIXTURE_MODE_VALUE

    def test_ambient_cloud_credentials_are_unusable(self):
        assert fixture.credentials_are_poisoned()
        assert not os.path.exists(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
        assert os.environ["GCE_METADATA_HOST"] == fixture.DEAD_METADATA_ENDPOINT

    def test_the_egress_guard_is_installed_at_import(self):
        assert fixture.egress_guard_installed()

    @pytest.mark.parametrize(
        "address",
        [
            ("127.0.0.1", 8010),
            ("::1", 8010),
            ("localhost", 8010),
            ("[::1]", 8010),
        ],
    )
    def test_loopback_is_permitted(self, address):
        fixture.assert_loopback(address)

    @pytest.mark.parametrize(
        "address",
        [
            ("169.254.169.254", 80),
            ("10.1.2.3", 443),
            ("93.184.216.34", 443),
            ("metadata.google.internal", 80),
            ("api.devrev.ai", 443),
            None,
            42,
        ],
    )
    def test_everything_else_is_refused(self, address):
        with pytest.raises(fixture.FixtureIsolationError):
            fixture.assert_loopback(address)

    def test_the_metadata_service_address_is_link_local(self):
        """A sanity check on the parametrization above, not on the guard."""
        assert ipaddress.ip_address("169.254.169.254").is_link_local

    def test_a_real_connection_off_the_host_raises(self):
        """The guard is what actually holds, so it is exercised, not inspected.

        No packet leaves: the refusal happens before the syscall, which is also
        why this test does not need the network to be absent.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.25)
            with pytest.raises(fixture.FixtureIsolationError):
                probe.connect(("169.254.169.254", 80))

    def test_connect_ex_is_guarded_too(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.25)
            with pytest.raises(fixture.FixtureIsolationError):
                probe.connect_ex(("93.184.216.34", 443))

    def test_loopback_still_connects_through_the_guard(self):
        """The guard must not break the thing it exists to permit."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as caller:
                caller.settimeout(1)
                caller.connect(("127.0.0.1", port))

    def test_the_app_refuses_to_build_without_fixture_mode(self, monkeypatch):
        monkeypatch.delenv(fixture.FIXTURE_MODE_ENV, raising=False)
        with pytest.raises(fixture.FixtureIsolationError):
            fixture.build_fixture_app()

    def test_the_app_refuses_to_build_with_usable_credentials(self, monkeypatch, tmp_path):
        present = tmp_path / "credentials.json"
        present.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(present))
        with pytest.raises(fixture.FixtureIsolationError):
            fixture.build_fixture_app()

    def test_the_app_refuses_to_build_without_the_egress_guard(self, monkeypatch):
        monkeypatch.setattr(fixture, "egress_guard_installed", lambda: False)
        with pytest.raises(fixture.FixtureIsolationError):
            fixture.build_fixture_app()

    def test_no_real_dependency_is_reachable_by_configuration(self):
        settings = fixture.fixture_settings()
        assert settings.DEVREV_API_BASE == "http://127.0.0.1:1"
        assert settings.EVIDENCE_BROKER_URL == ""
        assert settings.ENVIRONMENT == "local"
        assert settings.FIRESTORE_DATABASE == "tickets-console-emulator"

    def test_the_settings_ignore_the_repository_env_file(self):
        """An untracked ``.env`` for another service must not steer the fixture."""
        settings = fixture.fixture_settings()
        assert settings.CONSOLE_ORIGIN == CONSOLE_ORIGIN

    def test_no_evidence_broker_client_exists(self):
        app = fixture.build_fixture_app()
        assert app.state.evidence_client is None


# =====================================================================
# Identity
# =====================================================================


class TestInjectedIdentity:

    def test_every_page_is_reachable_with_no_request_headers(self, client):
        """A browser cannot attach a header to a navigation.

        This is why the fixture injects an authenticator instead of using the
        product's local-auth path, which requires one.
        """
        for path in ("/tickets", "/tickets/FIX-100", "/tickets/assets/app.js"):
            assert client.get(path).status_code == 200, path

    def test_the_session_route_answers_with_no_request_headers(self, client):
        body = client.get(f"{API_PREFIX}/session").json()
        assert body["identity"]["email"] == fixture.FIXTURE_EMAIL
        assert body["role"] == "reviewer"
        assert body["csrf_token"]

    def test_the_feature_flags_report_the_absent_stages(self, client):
        flags = client.get(f"{API_PREFIX}/session").json()["feature_flags"]
        assert flags["remediation_enabled"] is False
        assert flags["import_export_enabled"] is False

    def test_the_role_is_configurable_for_a_permission_check(self, monkeypatch):
        monkeypatch.setenv(fixture.FIXTURE_ROLE_ENV, "viewer")
        app = fixture.build_fixture_app()
        with TestClient(app, raise_server_exceptions=False) as viewer:
            assert viewer.get(f"{API_PREFIX}/session").json()["role"] == "viewer"
            created = viewer.post(
                f"{API_PREFIX}/tickets/FIX-107/review",
                headers=_write_headers(viewer, idempotency="fixture-viewer-0001"),
                json={},
            )
            assert created.status_code == 403

    def test_the_fixture_status_route_reports_the_parent_nonce(self, monkeypatch, client):
        monkeypatch.setenv(fixture.FIXTURE_NONCE_ENV, "a" * 64)
        body = client.get(fixture.FIXTURE_STATUS_PATH).json()
        assert body["fixture"] is True
        assert body["nonce"] == "a" * 64
        assert set(fixture.SCENARIOS) == set(body["scenarios"])

    def test_the_fixture_route_is_absent_from_the_documented_schema(self, client):
        """The console publishes no schema at all, so this cannot leak into one."""
        assert client.get("/openapi.json").status_code == 404


# =====================================================================
# Seeded data
# =====================================================================


class TestSeededData:

    def _all_reviews(self, client) -> list[dict]:
        """Walk the queue to the end, since it no longer fits on one page."""
        items: list[dict] = []
        cursor = None
        for _ in range(20):
            headers = {} if cursor is None else {CURSOR_HEADER: cursor}
            page = client.get(f"{API_PREFIX}/reviews?page_size=100", headers=headers).json()
            items.extend(page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                return items
        raise AssertionError("the queue did not terminate")

    def test_the_fixture_is_larger_than_the_page_the_interface_requests(self):
        """Otherwise neither pagination control can be exercised at all."""
        assert fixture.FIXTURE_TICKET_COUNT > fixture.UI_PAGE_SIZE
        assert len(fixture.seeded_ticket_indexes()) > fixture.UI_PAGE_SIZE

    def test_the_mirrored_page_size_matches_the_interface(self):
        """The counts above are only meaningful if this number is the real one."""
        state = (UI_ASSETS_DIRECTORY / "state.js").read_text(encoding="utf-8")
        assert f"DEFAULT_PAGE_SIZE = {fixture.UI_PAGE_SIZE};" in state

    def test_every_seeded_review_exists(self, client):
        reviews = self._all_reviews(client)
        assert len(reviews) == len(fixture.seeded_ticket_indexes())

    def test_the_queue_holds_every_lifecycle_state_the_seed_declares(self, client):
        statuses = {item["status"] for item in self._all_reviews(client)}
        assert statuses == {"unreviewed", "reviewed", "in_progress", "resolved"}

    def test_the_queue_is_ordered_newest_change_first(self, client):
        items = client.get(f"{API_PREFIX}/reviews?page_size=100").json()["items"]
        stamps = [item["updated_at"] for item in items]
        assert stamps == sorted(stamps, reverse=True)

    def test_both_reviewer_shapes_are_represented(self, client):
        """The interface has to show an assignment and a labelled legacy name."""
        items = client.get(f"{API_PREFIX}/reviews?page_size=100").json()["items"]
        assigned = [item for item in items if item.get("assigned_reviewer")]
        legacy = [item for item in items if item.get("legacy_reviewer_display_name")]
        assert assigned and legacy

    def test_ratings_cover_the_low_end_the_indicator_counts(self, client):
        items = client.get(f"{API_PREFIX}/reviews?page_size=100").json()["items"]
        ratings = {item["rating"] for item in items}
        assert ratings & {1, 2}
        assert ratings & {4, 5}

    def test_a_terminal_review_carries_its_resolution(self, client):
        resolved = [item for item in self._all_reviews(client) if item["status"] == "resolved"]
        assert resolved
        assert resolved[0]["resolution"]["no_change_reason"]

    def test_the_live_list_holds_every_synthetic_ticket(self, client):
        page = client.get(f"{API_PREFIX}/tickets?page_size=100").json()
        assert len(page["items"]) == fixture.FIXTURE_TICKET_COUNT

    def test_every_live_page_mixes_imported_and_unimported_rows(self, client):
        """Interleaving is the point: both row states are reachable everywhere.

        If reviews filled the first block instead, page one would be entirely
        imported and the import action would be unreachable without paging.
        """
        cursor = None
        pages = 0
        while pages < 10:
            headers = {} if cursor is None else {CURSOR_HEADER: cursor}
            page = client.get(f"{API_PREFIX}/tickets?page_size=10", headers=headers).json()
            assert any(item["review"] is None for item in page["items"])
            assert any(item["review"] is not None for item in page["items"])
            pages += 1
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert pages > 1

    def test_no_seeded_string_looks_like_a_real_participant(self, client):
        text = client.get(f"{API_PREFIX}/tickets?page_size=100").text
        assert "forusall" not in text.lower()
        assert "example.invalid" in client.get(f"{API_PREFIX}/session").text


# =====================================================================
# Pagination
# =====================================================================


class TestPagination:

    def test_the_live_list_pages_forward_and_back(self, client):
        first = client.get(f"{API_PREFIX}/tickets?page_size=3").json()
        assert [item["ticket"]["devrev_display_id"] for item in first["items"]] == [
            "FIX-100",
            "FIX-101",
            "FIX-102",
        ]
        assert first["prev_cursor"] is None
        second = client.get(
            f"{API_PREFIX}/tickets?page_size=3&mode=after",
            headers={CURSOR_HEADER: first["next_cursor"]},
        ).json()
        assert [item["ticket"]["devrev_display_id"] for item in second["items"]] == [
            "FIX-103",
            "FIX-104",
            "FIX-105",
        ]
        back = client.get(
            f"{API_PREFIX}/tickets?page_size=3&mode=before",
            headers={CURSOR_HEADER: second["prev_cursor"]},
        ).json()
        assert [item["ticket"]["devrev_display_id"] for item in back["items"]] == [
            "FIX-100",
            "FIX-101",
            "FIX-102",
        ]

    def test_a_forward_token_is_refused_in_the_backward_direction(self, client):
        """The direction is bound into the sealed token, so this is a 422."""
        first = client.get(f"{API_PREFIX}/tickets?page_size=3").json()
        refused = client.get(
            f"{API_PREFIX}/tickets?page_size=3&mode=before",
            headers={CURSOR_HEADER: first["next_cursor"]},
        )
        assert refused.status_code == 422
        assert refused.json()["error"]["code"] == "CURSOR_REJECTED"

    def test_a_cursor_in_the_url_is_refused(self, client):
        refused = client.get(f"{API_PREFIX}/tickets?cursor=anything")
        assert refused.status_code == 422

    def test_the_queue_offers_only_a_forward_cursor(self, client):
        """No backward token at all, which is why the interface keeps a stack."""
        page = client.get(f"{API_PREFIX}/reviews?page_size=2").json()
        assert page["next_cursor"]
        assert page["prev_cursor"] is None
        second = client.get(
            f"{API_PREFIX}/reviews?page_size=2", headers={CURSOR_HEADER: page["next_cursor"]}
        ).json()
        assert second["items"]
        assert second["prev_cursor"] is None
        assert {item["devrev_display_id"] for item in second["items"]}.isdisjoint(
            {item["devrev_display_id"] for item in page["items"]}
        )

    def test_the_interface_page_size_leaves_more_than_one_page_of_each_source(self, client):
        """What makes the browser check able to exercise both controls."""
        live = client.get(f"{API_PREFIX}/tickets?page_size={fixture.UI_PAGE_SIZE}").json()
        queue = client.get(f"{API_PREFIX}/reviews?page_size={fixture.UI_PAGE_SIZE}").json()
        assert live["next_cursor"] is not None
        assert queue["next_cursor"] is not None

    def test_an_exact_identifier_returns_a_singleton_with_no_cursors(self, client):
        page = client.get(f"{API_PREFIX}/tickets?ticket_id=FIX-101").json()
        assert [item["ticket"]["devrev_display_id"] for item in page["items"]] == ["FIX-101"]
        assert page["next_cursor"] is None and page["prev_cursor"] is None

    def test_an_exact_identifier_combined_with_a_filter_is_refused(self, client):
        """Sent as asked, refused by the server; never quietly narrowed."""
        refused = client.get(f"{API_PREFIX}/tickets?ticket_id=FIX-101&stage=resolved")
        assert refused.status_code == 422
        assert refused.json()["error"]["code"] == "UNSUPPORTED_FILTER_COMBINATION"

    def test_an_exact_queue_identifier_combined_with_a_facet_is_refused(self, client):
        refused = client.get(
            f"{API_PREFIX}/reviews?devrev_display_id=FIX-100&facet=severity&facet_value=high"
        )
        assert refused.status_code == 422
        assert refused.json()["error"]["code"] == "UNSUPPORTED_FILTER_COMBINATION"


# =====================================================================
# Deterministic failures
# =====================================================================


class TestScenarios:

    @pytest.mark.parametrize(
        "scenario,status,code",
        [
            (fixture.SCENARIO_UNAUTHENTICATED, 401, "UNAUTHENTICATED"),
            (fixture.SCENARIO_FORBIDDEN, 403, "FORBIDDEN"),
            (fixture.SCENARIO_RATE_LIMITED, 429, "RATE_LIMITED"),
            (fixture.SCENARIO_UPSTREAM_RATE_LIMITED, 429, "UPSTREAM_RATE_LIMITED"),
            (fixture.SCENARIO_UNAVAILABLE, 503, "UPSTREAM_UNAVAILABLE"),
            (fixture.SCENARIO_PROTOCOL, 502, "UPSTREAM_PROTOCOL_ERROR"),
        ],
    )
    def test_each_failure_arrives_in_the_one_envelope(self, client, scenario, status, code):
        response = client.get(f"{API_PREFIX}/tickets?stage={scenario}")
        assert response.status_code == status
        body = response.json()
        assert set(body) == {"error"}
        assert body["error"]["code"] == code
        assert body["error"]["request_id"]

    def test_our_own_rate_limit_always_carries_retry_after(self, client):
        response = client.get(f"{API_PREFIX}/tickets?stage={fixture.SCENARIO_RATE_LIMITED}")
        assert response.headers["Retry-After"] == "8"

    def test_the_upstream_rate_limit_may_omit_retry_after(self, client):
        """Which is why the interface needs a fallback backoff of its own."""
        response = client.get(
            f"{API_PREFIX}/tickets?stage={fixture.SCENARIO_UPSTREAM_RATE_LIMITED}"
        )
        assert "retry-after" not in {name.lower() for name in response.headers}

    def test_the_empty_scenario_is_an_empty_page_not_an_error(self, client):
        page = client.get(f"{API_PREFIX}/tickets?stage={fixture.SCENARIO_EMPTY}").json()
        assert page["items"] == []
        assert page["partial"] is False

    def test_the_partial_scenario_says_so(self, client):
        page = client.get(f"{API_PREFIX}/tickets?stage={fixture.SCENARIO_PARTIAL}").json()
        assert page["partial"] is True
        assert page["warnings"] == ["devrev_unavailable"]
        assert page["items"], "a partial page still carries what it could read"

    def test_a_failure_scenario_reaches_the_queue_tab_too(self, client):
        response = client.get(
            f"{API_PREFIX}/reviews?facet=severity&facet_value={fixture.SCENARIO_UNAVAILABLE}"
        )
        assert response.status_code == 503

    def test_every_security_header_survives_a_scenario_failure(self, client):
        response = client.get(f"{API_PREFIX}/tickets?stage={fixture.SCENARIO_PROTOCOL}")
        assert response.headers["Content-Security-Policy"]
        assert response.headers["Cache-Control"] == "no-store"


# =====================================================================
# Writes
# =====================================================================


class TestWrites:

    def test_an_unimported_ticket_can_be_imported(self, client):
        created = client.post(
            f"{API_PREFIX}/tickets/FIX-107/review",
            headers=_write_headers(client, idempotency="fixture-import-0001"),
            json={},
        )
        assert created.status_code == 201, created.text
        assert created.headers["ETag"] == '"v1"'
        listed = client.get(f"{API_PREFIX}/reviews?page_size=25").json()["items"]
        assert "FIX-107" in {item["devrev_display_id"] for item in listed}

    def test_a_write_without_the_csrf_token_is_refused(self, client):
        headers = _write_headers(client, idempotency="fixture-import-0002")
        headers.pop(CSRF_HEADER)
        assert (
            client.post(f"{API_PREFIX}/tickets/FIX-107/review", headers=headers, json={}).status_code
            == 403
        )

    def test_a_write_from_another_origin_is_refused(self, client):
        headers = _write_headers(client, idempotency="fixture-import-0003")
        headers[ORIGIN_HEADER] = "https://attacker.example"
        assert (
            client.post(f"{API_PREFIX}/tickets/FIX-107/review", headers=headers, json={}).status_code
            == 403
        )

    def test_a_write_with_no_origin_at_all_is_refused(self, client):
        headers = _write_headers(client, idempotency="fixture-import-0004")
        headers.pop(ORIGIN_HEADER)
        assert (
            client.post(f"{API_PREFIX}/tickets/FIX-107/review", headers=headers, json={}).status_code
            == 403
        )

    def test_a_write_without_an_idempotency_key_is_refused(self, client):
        headers = _write_headers(client, idempotency="fixture-import-0005")
        headers.pop(IDEMPOTENCY_HEADER)
        response = client.post(f"{API_PREFIX}/tickets/FIX-107/review", headers=headers, json={})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    def test_a_repeated_import_is_idempotent(self, client):
        headers = _write_headers(client, idempotency="fixture-import-0006")
        first = client.post(f"{API_PREFIX}/tickets/FIX-109/review", headers=headers, json={})
        assert first.status_code == 201
        again = client.post(
            f"{API_PREFIX}/tickets/FIX-109/review",
            headers=_write_headers(client, idempotency="fixture-import-0007"),
            json={},
        )
        assert again.status_code == 201
        assert again.json()["review_id"] == first.json()["review_id"]
