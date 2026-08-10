"""Stage 5 Step 1 — unsafe-method protection for the console.

Master-plan requirement 12 in one file: an unsafe request must present an exact
``Origin``, a same-origin ``Sec-Fetch-Site``, a live session-bound CSRF token, a
strict content type, and a valid ``Idempotency-Key``. Anything missing is a
refusal, not a warning.

The documented non-browser exception for the remediation agent is tested from
both sides: it works for the verified service account on an allowlisted route,
and it is unavailable to anything that looks like a browser — a human role, a
request carrying cookies, or a route outside the allowlist.

The staging verification handoff is tested as what it is: an *ordering* token
that carries no authority. Production enablement, tampering, replay, expiry,
reordered phases, a wrong next role, and injected resource ids are all refused.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from api.reviewer_auth import AuthConfigurationError, AuthenticatedReviewer
from api.ticket_review_models import CursorError, ReviewerIdentity, ReviewerRole
from api.tickets_console_config import TicketConsoleSettings
from api.tickets_csrf import (
    COOKIE_HEADER,
    CSRF_HEADER,
    CSRF_TOKEN_VERSION,
    CURSOR_HEADER,
    FETCH_SITE_HEADER,
    IDEMPOTENCY_HEADER,
    JSON_CONTENT_TYPE,
    ORIGIN_HEADER,
    SAFE_METHODS,
    VERIFICATION_HANDOFF_HEADER,
    VERIFICATION_PHASES,
    VERIFICATION_PHASE_HEADER,
    VERIFICATION_RUN_HEADER,
    ContentTypeRejected,
    CsrfTokenRejected,
    FetchMetadataRejected,
    IdempotencyKeyRejected,
    OriginRejected,
    UnsafeRequestPolicy,
    UnsafeRequestRejected,
    VerificationHandoffError,
    assert_unsafe_request_allowed,
    derive_handoff_key,
    handoff_enabled,
    mint_csrf_token,
    mint_verification_handoff,
    next_verification_phase,
    open_verification_handoff,
    policy_from_settings,
    validate_idempotency_key,
    verify_csrf_token,
)

TEST_AEAD_KEY = bytes(range(32))
TEST_AEAD_KEY_B64 = base64.b64encode(TEST_AEAD_KEY).decode("ascii")
CONSOLE_ORIGIN = "https://tickets-console-abc-uc.a.run.app"
CSRF_SECRET = "synthetic-csrf-value"  # pragma: allowlist secret
AGENT_SA = "tickets-remediation-agent@rag-kb-system.iam.gserviceaccount.com"
AGENT_ROUTE = "/api/admin/v1/remediation-batches/abc:claim"

NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc)
SUBJECT = "accounts.google.com:117300000000000000001"
OTHER_SUBJECT = "accounts.google.com:117300000000000000002"
RUN_ID = "3f6b7e2c-9a45-4c31-8f0e-2b1c5d7a9e40"


def _reviewer(
    role: ReviewerRole = ReviewerRole.REVIEWER, subject: str = SUBJECT
) -> AuthenticatedReviewer:
    return AuthenticatedReviewer(
        identity=ReviewerIdentity(subject=subject, email="reviewer@example.invalid"),
        role=role,
    )


def _agent() -> AuthenticatedReviewer:
    return AuthenticatedReviewer(
        identity=ReviewerIdentity(subject="sa-unique-id", email=AGENT_SA),
        role=ReviewerRole.AGENT,
        is_agent=True,
    )


def _policy(**overrides) -> UnsafeRequestPolicy:
    values: dict[str, object] = {
        "console_origin": CONSOLE_ORIGIN,
        "csrf_secret": CSRF_SECRET,
        "csrf_ttl_s": 3600,
        "agent_routes": frozenset({AGENT_ROUTE}),
    }
    values.update(overrides)
    return UnsafeRequestPolicy(**values)  # type: ignore[arg-type]


def _csrf(subject: str = SUBJECT, *, now: datetime = NOW, ttl_s: int = 3600) -> str:
    return mint_csrf_token(CSRF_SECRET, subject=subject, now=now, ttl_s=ttl_s).token


def _headers(**overrides) -> dict[str, str]:
    headers = {
        ORIGIN_HEADER: CONSOLE_ORIGIN,
        FETCH_SITE_HEADER: "same-origin",
        "Content-Type": JSON_CONTENT_TYPE,
        CSRF_HEADER: _csrf(),
        IDEMPOTENCY_HEADER: "idem-0123456789ab",
    }
    for key, value in overrides.items():
        if value is None:
            headers.pop(key, None)
        else:
            headers[key] = value
    return headers


def _check(headers=None, *, reviewer=None, policy=None, path="/api/admin/v1/reviews", **kw):
    return assert_unsafe_request_allowed(
        policy or _policy(),
        method=kw.pop("method", "POST"),
        path=path,
        headers=_headers() if headers is None else headers,
        reviewer=reviewer if reviewer is not None else _reviewer(),
        now=kw.pop("now", NOW),
        **kw,
    )


def _settings(monkeypatch, **overrides) -> TicketConsoleSettings:
    for name in list(os.environ):
        if name.startswith("TICKETS_"):
            monkeypatch.delenv(name, raising=False)
    values: dict[str, object] = {
        "ENVIRONMENT": "staging",
        "AUTH_MODE": "iap",
        "CONSOLE_ORIGIN": CONSOLE_ORIGIN,
        "CSRF_SIGNING_SECRET": CSRF_SECRET,
        "CSRF_TOKEN_TTL_S": 3600,
        "CURSOR_AEAD_KEY": TEST_AEAD_KEY_B64,
        "ENABLE_SYNTHETIC_VERIFICATION": False,
    }
    values.update(overrides)
    return TicketConsoleSettings(_env_file=None, **values)


# =====================================================================
# The session CSRF token
# =====================================================================


class TestCsrfToken:

    def test_mint_returns_a_bounded_token_and_its_expiry(self):
        minted = mint_csrf_token(CSRF_SECRET, subject=SUBJECT, now=NOW, ttl_s=3600)
        assert minted.token.startswith(f"{CSRF_TOKEN_VERSION}.")
        assert minted.expires_at == NOW + timedelta(seconds=3600)
        # Must fit SessionResponse.csrf_token (max_length=256).
        assert 0 < len(minted.token) <= 256

    def test_a_fresh_token_verifies_for_its_own_subject(self):
        verify_csrf_token(CSRF_SECRET, _csrf(), subject=SUBJECT, now=NOW)

    def test_the_token_never_contains_the_subject(self):
        assert SUBJECT not in _csrf()

    def test_a_token_is_session_bound(self):
        with pytest.raises(CsrfTokenRejected):
            verify_csrf_token(CSRF_SECRET, _csrf(), subject=OTHER_SUBJECT, now=NOW)

    def test_an_expired_token_is_rejected(self):
        token = _csrf(ttl_s=60)
        with pytest.raises(CsrfTokenRejected):
            verify_csrf_token(
                CSRF_SECRET, token, subject=SUBJECT, now=NOW + timedelta(seconds=61)
            )

    def test_a_token_at_its_exact_expiry_is_rejected(self):
        token = _csrf(ttl_s=60)
        with pytest.raises(CsrfTokenRejected):
            verify_csrf_token(
                CSRF_SECRET, token, subject=SUBJECT, now=NOW + timedelta(seconds=60)
            )

    def test_a_token_from_another_secret_is_rejected(self):
        other = mint_csrf_token(
            "another-secret", subject=SUBJECT, now=NOW, ttl_s=3600  # pragma: allowlist secret
        ).token
        with pytest.raises(CsrfTokenRejected):
            verify_csrf_token(CSRF_SECRET, other, subject=SUBJECT, now=NOW)

    @pytest.mark.parametrize(
        "token",
        [
            "",
            "   ",
            "garbage",
            "v1.notanumber.abc",
            "v1.99999999999",
            "v2.1800000000.abc",
            "v1..abc",
            "x" * 300,
        ],
    )
    def test_a_malformed_token_is_rejected(self, token):
        with pytest.raises(CsrfTokenRejected):
            verify_csrf_token(CSRF_SECRET, token, subject=SUBJECT, now=NOW)

    def test_a_tampered_expiry_is_rejected(self):
        version, expiry, mac = _csrf().split(".")
        forged = f"{version}.{int(expiry) + 86_400}.{mac}"
        with pytest.raises(CsrfTokenRejected):
            verify_csrf_token(CSRF_SECRET, forged, subject=SUBJECT, now=NOW)

    def test_a_tampered_mac_is_rejected(self):
        version, expiry, mac = _csrf().split(".")
        flipped = ("A" if mac[0] != "A" else "B") + mac[1:]
        with pytest.raises(CsrfTokenRejected):
            verify_csrf_token(CSRF_SECRET, f"{version}.{expiry}.{flipped}", subject=SUBJECT, now=NOW)

    def test_minting_requires_a_secret(self):
        with pytest.raises(AuthConfigurationError):
            mint_csrf_token("", subject=SUBJECT, now=NOW, ttl_s=3600)

    def test_verifying_without_a_secret_never_succeeds(self):
        """A revision with no secret must refuse, not accept everything."""
        with pytest.raises((CsrfTokenRejected, AuthConfigurationError)):
            verify_csrf_token("", _csrf(), subject=SUBJECT, now=NOW)


# =====================================================================
# Idempotency keys
# =====================================================================


class TestIdempotencyKey:

    def test_a_reasonable_key_is_accepted(self):
        assert validate_idempotency_key("idem-0123456789ab") == "idem-0123456789ab"

    def test_a_uuid_is_accepted(self):
        value = str(uuid.uuid4())
        assert validate_idempotency_key(value) == value

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "   ",
            "short",
            "x" * 300,
            "has spaces here",
            "has/slash/chars",
            "newline\ninjected",
            "-leading-dash-is-out",
            "emoji-\U0001f600-key",
        ],
    )
    def test_a_bad_key_is_rejected(self, value):
        with pytest.raises(IdempotencyKeyRejected):
            validate_idempotency_key(value)


# =====================================================================
# 12. The unsafe-request matrix
# =====================================================================


class TestUnsafeRequestMatrix:

    def test_a_complete_request_passes_and_returns_the_key(self):
        assert _check() == "idem-0123456789ab"

    def test_safe_methods_are_not_guarded(self):
        assert SAFE_METHODS == frozenset({"GET", "HEAD", "OPTIONS"})

    def test_missing_origin_is_rejected(self):
        with pytest.raises(OriginRejected):
            _check(_headers(**{ORIGIN_HEADER: None}))

    @pytest.mark.parametrize(
        "origin",
        [
            "https://attacker.example",
            "http://tickets-console-abc-uc.a.run.app",
            "https://tickets-console-abc-uc.a.run.app.attacker.example",
            "https://tickets-console-abc-uc.a.run.app:8443",
            "null",
            "https://tickets-console-abc-uc.a.run.app/",
        ],
    )
    def test_mismatched_origin_is_rejected(self, origin):
        with pytest.raises(OriginRejected):
            _check(_headers(**{ORIGIN_HEADER: origin}))

    def test_origin_comparison_ignores_case_of_scheme_and_host(self):
        assert _check(_headers(**{ORIGIN_HEADER: CONSOLE_ORIGIN.upper()}))

    @pytest.mark.parametrize("site", ["cross-site", "same-site", "none", "", "SAME-ORIGIN "])
    def test_non_same_origin_fetch_site_is_rejected(self, site):
        if site.strip().lower() == "same-origin":
            pytest.skip("normalized value is accepted; covered below")
        with pytest.raises(FetchMetadataRejected):
            _check(_headers(**{FETCH_SITE_HEADER: site}))

    def test_missing_fetch_site_is_rejected(self):
        with pytest.raises(FetchMetadataRejected):
            _check(_headers(**{FETCH_SITE_HEADER: None}))

    @pytest.mark.parametrize(
        "content_type",
        [
            None,
            "",
            "text/plain",
            "application/x-www-form-urlencoded",
            "multipart/form-data; boundary=x",
            "text/csv",
            "application/json-patch+json",
            "application/jsonx",
        ],
    )
    def test_wrong_content_type_is_rejected(self, content_type):
        with pytest.raises(ContentTypeRejected):
            _check(_headers(**{"Content-Type": content_type}))

    @pytest.mark.parametrize(
        "content_type",
        ["application/json", "application/json; charset=utf-8", "Application/JSON"],
    )
    def test_acceptable_json_content_types(self, content_type):
        assert _check(_headers(**{"Content-Type": content_type}))

    def test_a_non_utf8_charset_is_rejected(self):
        with pytest.raises(ContentTypeRejected):
            _check(_headers(**{"Content-Type": "application/json; charset=iso-8859-1"}))

    def test_missing_csrf_token_is_rejected(self):
        with pytest.raises(CsrfTokenRejected):
            _check(_headers(**{CSRF_HEADER: None}))

    def test_another_sessions_csrf_token_is_rejected(self):
        with pytest.raises(CsrfTokenRejected):
            _check(_headers(**{CSRF_HEADER: _csrf(OTHER_SUBJECT)}))

    def test_an_expired_csrf_token_is_rejected(self):
        with pytest.raises(CsrfTokenRejected):
            _check(_headers(**{CSRF_HEADER: _csrf(ttl_s=30)}), now=NOW + timedelta(minutes=5))

    def test_missing_idempotency_key_is_rejected(self):
        with pytest.raises(IdempotencyKeyRejected):
            _check(_headers(**{IDEMPOTENCY_HEADER: None}))

    def test_invalid_idempotency_key_is_rejected(self):
        with pytest.raises(IdempotencyKeyRejected):
            _check(_headers(**{IDEMPOTENCY_HEADER: "no"}))

    def test_every_rejection_is_one_taxonomy(self):
        for exc in (
            OriginRejected,
            FetchMetadataRejected,
            ContentTypeRejected,
            CsrfTokenRejected,
            IdempotencyKeyRejected,
        ):
            assert issubclass(exc, UnsafeRequestRejected)
            assert isinstance(exc("x").code, str) and exc("x").code
            assert isinstance(exc("x").status, int)

    def test_a_cross_site_request_fails_before_the_csrf_token_is_read(self):
        """Order matters: the cheapest, most decisive check runs first.

        A cross-site caller cannot read a response anyway, so there is no reason
        to spend an HMAC on it — and reporting a CSRF failure would tell it that
        its Origin was acceptable.
        """
        headers = _headers(**{ORIGIN_HEADER: "https://attacker.example", CSRF_HEADER: None})
        with pytest.raises(OriginRejected):
            _check(headers)

    def test_a_missing_secret_makes_every_unsafe_request_fail(self):
        with pytest.raises((CsrfTokenRejected, AuthConfigurationError)):
            _check(policy=_policy(csrf_secret=""))

    def test_an_unconfigured_origin_makes_every_unsafe_request_fail(self):
        with pytest.raises((OriginRejected, AuthConfigurationError)):
            _check(policy=_policy(console_origin=""))


# =====================================================================
# The documented non-browser agent exception
# =====================================================================


class TestAgentException:

    def _agent_headers(self, **overrides):
        headers = {
            "Content-Type": JSON_CONTENT_TYPE,
            IDEMPOTENCY_HEADER: "idem-0123456789ab",
        }
        for key, value in overrides.items():
            if value is None:
                headers.pop(key, None)
            else:
                headers[key] = value
        return headers

    def test_the_agent_may_omit_origin_fetch_site_and_csrf(self):
        assert (
            _check(self._agent_headers(), reviewer=_agent(), path=AGENT_ROUTE)
            == "idem-0123456789ab"
        )

    def test_the_agent_still_needs_a_strict_content_type(self):
        with pytest.raises(ContentTypeRejected):
            _check(
                self._agent_headers(**{"Content-Type": "text/plain"}),
                reviewer=_agent(),
                path=AGENT_ROUTE,
            )

    def test_the_agent_still_needs_an_idempotency_key(self):
        with pytest.raises(IdempotencyKeyRejected):
            _check(
                self._agent_headers(**{IDEMPOTENCY_HEADER: None}),
                reviewer=_agent(),
                path=AGENT_ROUTE,
            )

    def test_the_exception_is_route_scoped(self):
        with pytest.raises(UnsafeRequestRejected):
            _check(
                self._agent_headers(), reviewer=_agent(), path="/api/admin/v1/reviews"
            )

    def test_the_exception_is_unavailable_when_no_route_is_allowlisted(self):
        """Stage 5 ships an empty allowlist; the mechanism must then be inert."""
        with pytest.raises(UnsafeRequestRejected):
            _check(
                self._agent_headers(),
                reviewer=_agent(),
                path=AGENT_ROUTE,
                policy=_policy(agent_routes=frozenset()),
            )

    def test_a_cookie_bearing_request_cannot_claim_the_exception(self):
        """Cookies mean a browser, and a browser must prove same-origin.

        This is the confused-deputy case: a page in the reviewer's browser that
        somehow reached an agent route must not inherit the CLI's exemption.
        """
        with pytest.raises(UnsafeRequestRejected):
            _check(
                self._agent_headers(**{COOKIE_HEADER: "session=abc"}),
                reviewer=_agent(),
                path=AGENT_ROUTE,
            )

    @pytest.mark.parametrize("role", list(ReviewerRole))
    def test_only_the_verified_agent_identity_gets_the_exception(self, role):
        if role is ReviewerRole.AGENT:
            pytest.skip("the agent case is covered by its own test")
        with pytest.raises(UnsafeRequestRejected):
            _check(
                self._agent_headers(), reviewer=_reviewer(role), path=AGENT_ROUTE
            )

    def test_a_human_claiming_the_agent_role_without_is_agent_is_refused(self):
        """``is_agent`` is set only by verified service-account authentication."""
        impostor = AuthenticatedReviewer(
            identity=ReviewerIdentity(subject=SUBJECT, email="human@example.invalid"),
            role=ReviewerRole.AGENT,
            is_agent=False,
        )
        with pytest.raises(UnsafeRequestRejected):
            _check(self._agent_headers(), reviewer=impostor, path=AGENT_ROUTE)

    def test_the_agent_may_still_send_a_matching_origin(self):
        headers = self._agent_headers(
            **{ORIGIN_HEADER: CONSOLE_ORIGIN, FETCH_SITE_HEADER: "same-origin"}
        )
        assert _check(headers, reviewer=_agent(), path=AGENT_ROUTE)

    def test_the_agent_cannot_use_a_foreign_origin(self):
        headers = self._agent_headers(**{ORIGIN_HEADER: "https://attacker.example"})
        with pytest.raises(OriginRejected):
            _check(headers, reviewer=_agent(), path=AGENT_ROUTE)


# =====================================================================
# The staging verification handoff
# =====================================================================


class TestHandoffEnablement:

    def test_disabled_by_default(self, monkeypatch):
        assert handoff_enabled(_settings(monkeypatch)) is False

    def test_enabled_in_staging_behind_the_flag(self, monkeypatch):
        settings = _settings(monkeypatch, ENABLE_SYNTHETIC_VERIFICATION=True)
        assert handoff_enabled(settings) is True

    def test_never_enabled_in_production(self, monkeypatch):
        settings = _settings(
            monkeypatch, ENVIRONMENT="production", ENABLE_SYNTHETIC_VERIFICATION=True
        )
        assert handoff_enabled(settings) is False

    def test_enabled_for_a_local_loopback_fixture(self, monkeypatch):
        settings = _settings(
            monkeypatch, ENVIRONMENT="local", ENABLE_SYNTHETIC_VERIFICATION=True
        )
        assert handoff_enabled(settings, client_host="127.0.0.1") is True

    def test_a_local_non_loopback_client_is_not_enough(self, monkeypatch):
        settings = _settings(
            monkeypatch, ENVIRONMENT="local", ENABLE_SYNTHETIC_VERIFICATION=True
        )
        assert handoff_enabled(settings, client_host="10.0.0.7") is False


class TestHandoffKeyDerivation:

    def test_the_subkey_is_not_the_cursor_key(self):
        assert derive_handoff_key(TEST_AEAD_KEY) != TEST_AEAD_KEY

    def test_the_subkey_is_deterministic_and_32_bytes(self):
        first = derive_handoff_key(TEST_AEAD_KEY)
        assert first == derive_handoff_key(TEST_AEAD_KEY)
        assert len(first) == 32

    def test_a_different_cursor_key_yields_a_different_subkey(self):
        assert derive_handoff_key(TEST_AEAD_KEY) != derive_handoff_key(bytes(32))

    def test_a_wrong_length_cursor_key_is_refused(self):
        with pytest.raises(ValueError):
            derive_handoff_key(b"short")


class TestVerificationPhases:

    def test_phases_are_a_closed_ordered_vocabulary(self):
        assert isinstance(VERIFICATION_PHASES, tuple)
        assert len(VERIFICATION_PHASES) == len(set(VERIFICATION_PHASES))
        assert all(isinstance(phase, str) and phase for phase in VERIFICATION_PHASES)

    def test_next_phase_follows_the_order(self):
        for current, expected in zip(VERIFICATION_PHASES, VERIFICATION_PHASES[1:], strict=False):
            assert next_verification_phase(current) == expected

    def test_the_last_phase_has_no_successor(self):
        assert next_verification_phase(VERIFICATION_PHASES[-1]) is None

    def test_an_unknown_phase_is_refused(self):
        with pytest.raises(VerificationHandoffError):
            next_verification_phase("not-a-phase")


class TestVerificationHandoff:

    def _key(self) -> bytes:
        return derive_handoff_key(TEST_AEAD_KEY)

    def _mint(self, **overrides):
        values: dict[str, object] = {
            "environment": "staging",
            "run_id": RUN_ID,
            "phase": VERIFICATION_PHASES[1],
            "next_role": ReviewerRole.REMEDIATOR,
            "prior_token": None,
            "resources": {"batch-1": 2},
            "now": NOW,
            "ttl_s": 300,
        }
        values.update(overrides)
        return mint_verification_handoff(self._key(), **values)  # type: ignore[arg-type]

    def _open(self, token, **overrides):
        values: dict[str, object] = {
            "environment": "staging",
            "run_id": RUN_ID,
            "expected_phase": VERIFICATION_PHASES[1],
            "prior_token": None,
            "now": NOW,
        }
        values.update(overrides)
        return open_verification_handoff(self._key(), token, **values)  # type: ignore[arg-type]

    def test_a_minted_token_round_trips(self):
        payload = self._open(self._mint())
        assert payload["run_id"] == RUN_ID
        assert payload["phase"] == VERIFICATION_PHASES[1]
        assert payload["next_role"] == ReviewerRole.REMEDIATOR.value
        assert payload["resources"] == {"batch-1": 2}

    def test_the_token_is_opaque(self):
        token = self._mint()
        assert RUN_ID not in token
        assert "batch-1" not in token
        assert "staging" not in token

    def test_the_token_carries_no_identity_or_content(self):
        payload = self._open(self._mint())
        flattened = json.dumps(payload)
        assert "@" not in flattened
        assert CSRF_SECRET not in flattened
        assert not any(key in payload for key in ("email", "subject", "token", "secret"))

    def test_a_run_id_must_be_a_uuid(self):
        with pytest.raises(VerificationHandoffError):
            self._mint(run_id="not-a-uuid")

    def test_a_run_id_mismatch_is_refused(self):
        token = self._mint()
        with pytest.raises(VerificationHandoffError):
            self._open(token, run_id=str(uuid.uuid4()))

    def test_an_environment_mismatch_is_refused(self):
        token = self._mint()
        with pytest.raises(VerificationHandoffError):
            self._open(token, environment="local")

    def test_a_phase_mismatch_is_refused(self):
        token = self._mint()
        with pytest.raises(VerificationHandoffError):
            self._open(token, expected_phase=VERIFICATION_PHASES[0])

    def test_a_reordered_phase_is_refused(self):
        """Replaying phase 2's token to satisfy phase 3 must not work."""
        token = self._mint(phase=VERIFICATION_PHASES[1])
        with pytest.raises(VerificationHandoffError):
            self._open(token, expected_phase=VERIFICATION_PHASES[2])

    def test_expiry_is_enforced(self):
        token = self._mint(ttl_s=60)
        with pytest.raises(VerificationHandoffError):
            self._open(token, now=NOW + timedelta(seconds=61))

    def test_a_ttl_beyond_the_ceiling_is_refused(self):
        with pytest.raises(VerificationHandoffError):
            self._mint(ttl_s=86_400)

    def test_tampering_is_refused(self):
        token = self._mint()
        forged = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
        with pytest.raises((VerificationHandoffError, CursorError)):
            self._open(forged)

    def test_the_cursor_key_cannot_open_a_handoff(self):
        """Domain separation is the point: one key, two non-interchangeable uses."""
        token = self._mint()
        with pytest.raises((VerificationHandoffError, CursorError)):
            open_verification_handoff(
                TEST_AEAD_KEY,
                token,
                environment="staging",
                run_id=RUN_ID,
                expected_phase=VERIFICATION_PHASES[1],
                prior_token=None,
                now=NOW,
            )

    def test_a_prior_token_digest_is_bound(self):
        first = self._mint(phase=VERIFICATION_PHASES[1])
        second = self._mint(phase=VERIFICATION_PHASES[2], prior_token=first)
        payload = self._open(
            second, expected_phase=VERIFICATION_PHASES[2], prior_token=first
        )
        assert payload["phase"] == VERIFICATION_PHASES[2]

    def test_a_wrong_prior_token_is_refused(self):
        first = self._mint(phase=VERIFICATION_PHASES[1])
        second = self._mint(phase=VERIFICATION_PHASES[2], prior_token=first)
        other = self._mint(phase=VERIFICATION_PHASES[1], run_id=str(uuid.uuid4()))
        with pytest.raises(VerificationHandoffError):
            self._open(second, expected_phase=VERIFICATION_PHASES[2], prior_token=other)

    def test_a_missing_prior_token_after_the_first_phase_is_refused(self):
        first = self._mint(phase=VERIFICATION_PHASES[1])
        second = self._mint(phase=VERIFICATION_PHASES[2], prior_token=first)
        with pytest.raises(VerificationHandoffError):
            self._open(second, expected_phase=VERIFICATION_PHASES[2], prior_token=None)

    def test_replaying_the_first_phase_token_as_a_prior_is_refused(self):
        """A token cannot be its own predecessor."""
        first = self._mint(phase=VERIFICATION_PHASES[1])
        with pytest.raises(VerificationHandoffError):
            self._mint(phase=VERIFICATION_PHASES[1], prior_token=first, run_id=RUN_ID)

    def test_injected_resource_ids_are_bounded_and_typed(self):
        with pytest.raises(VerificationHandoffError):
            self._mint(resources={f"id-{index}": 1 for index in range(200)})
        with pytest.raises(VerificationHandoffError):
            self._mint(resources={"batch-1": "not-a-version"})
        with pytest.raises(VerificationHandoffError):
            self._mint(resources={"a" * 300: 1})

    def test_a_next_role_must_be_a_real_human_role(self):
        with pytest.raises(VerificationHandoffError):
            self._mint(next_role=ReviewerRole.AGENT)

    def test_the_handoff_grants_no_authority(self):
        """Holding a valid handoff does not satisfy the unsafe-request guard."""
        token = self._mint()
        headers = {
            "Content-Type": JSON_CONTENT_TYPE,
            IDEMPOTENCY_HEADER: "idem-0123456789ab",
            VERIFICATION_RUN_HEADER: RUN_ID,
            VERIFICATION_PHASE_HEADER: VERIFICATION_PHASES[1],
            VERIFICATION_HANDOFF_HEADER: token,
        }
        with pytest.raises(UnsafeRequestRejected):
            _check(headers)


# =====================================================================
# Policy construction and header names
# =====================================================================


class TestPolicyFromSettings:

    def test_policy_is_built_from_configuration(self, monkeypatch):
        policy = policy_from_settings(_settings(monkeypatch))
        assert policy.console_origin == CONSOLE_ORIGIN
        assert policy.csrf_secret == CSRF_SECRET
        assert policy.csrf_ttl_s == 3600
        # Stage 5 owns no agent route yet, so the exception is inert.
        assert policy.agent_routes == frozenset()

    def test_an_origin_with_a_path_is_refused(self, monkeypatch):
        settings = _settings(monkeypatch, CONSOLE_ORIGIN=f"{CONSOLE_ORIGIN}/tickets")
        with pytest.raises(AuthConfigurationError):
            policy_from_settings(settings)

    def test_the_policy_never_exposes_the_secret_in_its_repr(self, monkeypatch):
        policy = policy_from_settings(_settings(monkeypatch))
        assert CSRF_SECRET not in repr(policy)

    def test_header_names_match_the_master_plan(self):
        assert CSRF_HEADER == "X-CSRF-Token"
        assert IDEMPOTENCY_HEADER == "Idempotency-Key"
        assert CURSOR_HEADER == "X-Tickets-Cursor"
        assert VERIFICATION_RUN_HEADER == "X-Tickets-Verification-Run"
        assert ORIGIN_HEADER == "Origin"
        assert FETCH_SITE_HEADER == "Sec-Fetch-Site"
        assert JSON_CONTENT_TYPE == "application/json"
