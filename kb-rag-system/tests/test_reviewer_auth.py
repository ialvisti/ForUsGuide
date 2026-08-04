"""Stage 5 Step 1 — the console's identity boundary.

Every test here is offline. The IAP claims verifier is injected, so nothing in
this file fetches ``https://www.gstatic.com/iap/verify/public_key`` or needs
ADC: a unit test that reached the network would be both slow and, worse, green
for the wrong reason on a developer laptop that happened to have credentials.

The invariants under test, in the order the master plan states them:

1.  a missing assertion is ``401`` in IAP mode;
2.  a bad signature, issuer, expiry, issued-at, or audience is ``401``;
3.  a usable token carries a non-empty ``sub``, a non-empty ``email``, and the
    IAP issuer -- but *not* a top-level ``email_verified``, which real IAP
    assertions do not guarantee;
4.  domain comparison is normalized and exact;
5.  the unsigned ``X-Goog-Authenticated-User-Email`` header is inert;
6.  identity is derived only from verified claims;
7.  an IAP-authorized but unbound identity is denied;
8.  a role binding cannot be overridden by a query, body, or header;
9.  the role ladder is viewer < reviewer < remediator < admin, with ``agent``
    deliberately off the ladder;
10. local auth requires five independent conditions and can never exist in
    staging or production;
11. authentication logs carry a subject hash, never the assertion.
"""

from __future__ import annotations

import base64
import json
import logging
import os

import pytest

from api.reviewer_auth import (
    AGENT_AUDIENCE_SUFFIX,
    IAP_ASSERTION_HEADER,
    IAP_EMAIL_HEADER,
    IAP_ISSUER,
    IAP_PUBLIC_KEY_URL,
    IAP_USER_ID_HEADER,
    LOCAL_REVIEWER_HEADER,
    ROLE_LADDER,
    AuthConfigurationError,
    AuthenticatedReviewer,
    AuthenticationFailed,
    AuthorizationFailed,
    AuthVerifierUnavailable,
    ReviewerAuthenticator,
    parse_role_bindings,
    role_at_least,
    validate_agent_audience,
    validate_console_auth_startup,
)
from api.ticket_review_models import ReviewerRole
from api.tickets_console_config import TicketConsoleSettings

# A synthetic 32-byte AEAD key. Not a credential: it exists so settings that
# require one can be constructed offline.
TEST_AEAD_KEY_B64 = base64.b64encode(bytes(range(32))).decode("ascii")

CONSOLE_ORIGIN = "https://tickets-console-abc-uc.a.run.app"
PROD_IAP_AUDIENCE = "/projects/1234567890/locations/us-central1/services/tickets-console"
AGENT_SA = "tickets-remediation-agent@rag-kb-system.iam.gserviceaccount.com"
AGENT_AUDIENCE = f"{CONSOLE_ORIGIN}/*"

ADMIN_EMAIL = "admin@example.invalid"
REVIEWER_EMAIL = "reviewer@example.invalid"
REMEDIATOR_EMAIL = "remediator@example.invalid"
VIEWER_EMAIL = "viewer@example.invalid"
UNBOUND_EMAIL = "stranger@example.invalid"

ROLE_BINDINGS = json.dumps(
    {
        ADMIN_EMAIL: "admin",
        REVIEWER_EMAIL: "reviewer",
        REMEDIATOR_EMAIL: "remediator",
        VIEWER_EMAIL: "viewer",
    }
)

# A shape-valid, entirely synthetic assertion. It is never verified by real
# crypto here; the injected verifier decides the outcome. It exists so tests can
# assert that this exact string never reaches a log record.
FAKE_ASSERTION = "eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJzeW50aGV0aWMifQ.c2lnbmF0dXJl"


def _settings(monkeypatch, **overrides) -> TicketConsoleSettings:
    """Production-shaped console settings with zero ambient env leakage.

    Mirrors ``tests/test_ticket_review_models.py::_console_settings``: the
    repository ships a gitignored ``.env``, and ``pydantic-settings`` would
    otherwise load it and make these assertions depend on a developer's laptop.
    """
    for name in list(os.environ):
        if name.startswith("TICKETS_"):
            monkeypatch.delenv(name, raising=False)
    values: dict[str, object] = {
        "ENVIRONMENT": "production",
        "AUTH_MODE": "iap",
        "ALLOW_LOCAL_AUTH": False,
        "ALLOW_UNBOUND_VIEWERS": False,
        "ENABLE_SYNTHETIC_VERIFICATION": False,
        "DEFAULT_ROLE": None,
        "IAP_AUDIENCE": PROD_IAP_AUDIENCE,
        "ALLOWED_EMAIL_DOMAINS": ["example.invalid"],
        "ROLE_BINDINGS_JSON": ROLE_BINDINGS,
        "CSRF_SIGNING_SECRET": "synthetic-csrf-value",  # pragma: allowlist secret
        "CURSOR_AEAD_KEY": TEST_AEAD_KEY_B64,
        "GCP_PROJECT": "rag-kb-system",
        "GCP_REGION": "us-central1",
        "FIRESTORE_DATABASE": "tickets-console-prod",
        "CONSOLE_ORIGIN": CONSOLE_ORIGIN,
        "AGENT_SERVICE_ACCOUNT": AGENT_SA,
        "AGENT_IAP_TARGET_AUDIENCE": AGENT_AUDIENCE,
    }
    values.update(overrides)
    return TicketConsoleSettings(_env_file=None, **values)


def _local_settings(monkeypatch, **overrides) -> TicketConsoleSettings:
    values: dict[str, object] = {
        "ENVIRONMENT": "local",
        "AUTH_MODE": "local",
        "ALLOW_LOCAL_AUTH": True,
        "IAP_AUDIENCE": "local-fixture-audience",
        "CONSOLE_ORIGIN": "http://127.0.0.1:8080",
        "AGENT_SERVICE_ACCOUNT": "",
        "AGENT_IAP_TARGET_AUDIENCE": "",
    }
    values.update(overrides)
    return _settings(monkeypatch, **values)


class _FakeVerifier:
    """Stands in for ``google.oauth2.id_token.verify_token``.

    Records every ``(token, audience)`` pair so a test can prove the audience
    came from configuration rather than from a request header.
    """

    def __init__(self, claims: dict | None = None, error: BaseException | None = None):
        self.claims = claims
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def __call__(self, token: str, audience: str) -> dict:
        self.calls.append((token, audience))
        if self.error is not None:
            raise self.error
        return dict(self.claims or {})


def _claims(**overrides) -> dict:
    claims = {
        "iss": IAP_ISSUER,
        "sub": "accounts.google.com:117300000000000000001",
        "email": REVIEWER_EMAIL,
        "aud": PROD_IAP_AUDIENCE,
        "exp": 4_102_444_800,
        "iat": 1_700_000_000,
    }
    claims.update(overrides)
    return claims


def _authenticator(settings, *, claims=None, error=None, verifier=None):
    used = verifier if verifier is not None else _FakeVerifier(claims=claims, error=error)
    return ReviewerAuthenticator.from_settings(settings, claims_verifier=used), used


def _headers(**extra) -> dict[str, str]:
    headers = {IAP_ASSERTION_HEADER: FAKE_ASSERTION}
    headers.update(extra)
    return headers


# =====================================================================
# 1-2. The assertion is mandatory, and every verification failure is 401
# =====================================================================


class TestAssertionRequired:

    def test_missing_assertion_is_unauthenticated(self, monkeypatch):
        auth, verifier = _authenticator(_settings(monkeypatch), claims=_claims())
        with pytest.raises(AuthenticationFailed):
            auth.authenticate({})
        # Nothing was verified: there was nothing to verify.
        assert verifier.calls == []

    def test_blank_assertion_is_unauthenticated(self, monkeypatch):
        auth, _ = _authenticator(_settings(monkeypatch), claims=_claims())
        with pytest.raises(AuthenticationFailed):
            auth.authenticate({IAP_ASSERTION_HEADER: "   "})

    def test_header_lookup_is_case_insensitive(self, monkeypatch):
        auth, _ = _authenticator(
            _settings(monkeypatch), claims=_claims(email=ADMIN_EMAIL)
        )
        reviewer = auth.authenticate({IAP_ASSERTION_HEADER.lower(): FAKE_ASSERTION})
        assert reviewer.role is ReviewerRole.ADMIN

    @pytest.mark.parametrize(
        "error",
        [
            ValueError("Could not verify token signature."),
            ValueError("Token expired"),
            ValueError("Token used too early"),
            ValueError("Audience did not match"),
        ],
    )
    def test_verifier_rejection_is_unauthenticated(self, monkeypatch, error):
        auth, _ = _authenticator(_settings(monkeypatch), error=error)
        with pytest.raises(AuthenticationFailed):
            auth.authenticate(_headers())

    def test_verifier_infrastructure_failure_is_not_a_401(self, monkeypatch):
        """A certificate endpoint outage must not read as "you are anonymous".

        Returning 401 would invite a client to retry as an unauthenticated user;
        this is a 503 so the failure is attributed to the server.
        """
        auth, _ = _authenticator(
            _settings(monkeypatch), error=ConnectionError("certs unreachable")
        )
        with pytest.raises(AuthVerifierUnavailable):
            auth.authenticate(_headers())

    def test_wrong_issuer_is_rejected_even_when_the_verifier_passes(self, monkeypatch):
        auth, _ = _authenticator(
            _settings(monkeypatch), claims=_claims(iss="https://accounts.google.com")
        )
        with pytest.raises(AuthenticationFailed):
            auth.authenticate(_headers())

    def test_audience_is_rechecked_against_configuration(self, monkeypatch):
        """Defense in depth: the app re-compares ``aud`` after google-auth.

        A verifier that was accidentally constructed without an audience would
        otherwise let any IAP-signed token from any service through.
        """
        auth, _ = _authenticator(
            _settings(monkeypatch),
            claims=_claims(aud="/projects/1234567890/locations/us-central1/services/other"),
        )
        with pytest.raises(AuthenticationFailed):
            auth.authenticate(_headers())

    def test_expected_audience_comes_from_settings_not_from_a_header(self, monkeypatch):
        auth, verifier = _authenticator(_settings(monkeypatch), claims=_claims())
        auth.authenticate(
            _headers(**{"X-Goog-IAP-Audience": "/projects/9/locations/x/services/evil"})
        )
        assert [audience for _token, audience in verifier.calls] == [PROD_IAP_AUDIENCE]

    def test_a_missing_audience_configuration_fails_closed(self, monkeypatch):
        auth, _ = _authenticator(
            _settings(monkeypatch, IAP_AUDIENCE=""), claims=_claims()
        )
        with pytest.raises(AuthConfigurationError):
            auth.authenticate(_headers())

    def test_the_documented_iap_certificate_url_and_issuer_are_pinned(self):
        assert IAP_PUBLIC_KEY_URL == "https://www.gstatic.com/iap/verify/public_key"
        assert IAP_ISSUER == "https://cloud.google.com/iap"


# =====================================================================
# 3-6. Claim shape, domain policy, and header inertness
# =====================================================================


class TestVerifiedClaims:

    def test_valid_token_yields_the_verified_identity(self, monkeypatch):
        auth, _ = _authenticator(_settings(monkeypatch), claims=_claims())
        reviewer = auth.authenticate(_headers())
        assert isinstance(reviewer, AuthenticatedReviewer)
        assert reviewer.identity.subject == "accounts.google.com:117300000000000000001"
        assert reviewer.identity.email == REVIEWER_EMAIL
        assert reviewer.role is ReviewerRole.REVIEWER

    @pytest.mark.parametrize("subject", [None, "", "   "])
    def test_empty_subject_is_rejected(self, monkeypatch, subject):
        auth, _ = _authenticator(_settings(monkeypatch), claims=_claims(sub=subject))
        with pytest.raises(AuthenticationFailed):
            auth.authenticate(_headers())

    @pytest.mark.parametrize("email", [None, "", "   ", "not-an-email"])
    def test_empty_or_malformed_email_is_rejected(self, monkeypatch, email):
        auth, _ = _authenticator(_settings(monkeypatch), claims=_claims(email=email))
        with pytest.raises(AuthenticationFailed):
            auth.authenticate(_headers())

    def test_absent_email_verified_claim_is_accepted(self, monkeypatch):
        """Real IAP assertions do not carry a top-level ``email_verified``.

        Requiring one would deny every production reviewer, so this test exists
        to stop a future "harden the claims" change from doing exactly that.
        """
        claims = _claims()
        claims.pop("email_verified", None)
        auth, _ = _authenticator(_settings(monkeypatch), claims=claims)
        assert auth.authenticate(_headers()).identity.email == REVIEWER_EMAIL

    def test_hosted_domain_claim_must_agree_with_policy_when_present(self, monkeypatch):
        auth, _ = _authenticator(
            _settings(monkeypatch), claims=_claims(hd="attacker.tld")
        )
        with pytest.raises(AuthorizationFailed):
            auth.authenticate(_headers())

    def test_agreeing_hosted_domain_claim_is_accepted(self, monkeypatch):
        auth, _ = _authenticator(
            _settings(monkeypatch), claims=_claims(hd="Example.Invalid")
        )
        assert auth.authenticate(_headers()).role is ReviewerRole.REVIEWER

    @pytest.mark.parametrize(
        "email",
        [
            "reviewer@example.invalid.attacker.tld",
            "reviewer@attacker.tld",
            "reviewer@sub.example.invalid",
            "reviewer@example.invalid.",
            "reviewer@xample.invalid",
        ],
    )
    def test_domain_comparison_is_exact(self, monkeypatch, email):
        auth, _ = _authenticator(_settings(monkeypatch), claims=_claims(email=email))
        with pytest.raises((AuthorizationFailed, AuthenticationFailed)):
            auth.authenticate(_headers())

    @pytest.mark.parametrize(
        "email,allowed",
        [
            ("reviewer@example.invalid", True),
            ("REVIEWER@EXAMPLE.INVALID", True),
            ("  reviewer@example.invalid  ", True),
            ("reviewer@sub.example.invalid", False),
            ("reviewer@example.invalid.attacker.tld", False),
            ("reviewer@notexample.invalid", False),
            ("reviewer@example.invalidx", False),
            ("reviewer@xexample.invalid", False),
            ("reviewer@", False),
            ("no-at-sign", False),
            ("a@b@example.invalid", True),
        ],
    )
    def test_domain_allowed_is_exact_on_its_own(self, email, allowed):
        """Pinned directly, not through the role bindings.

        Domain policy and role binding both reject an outsider, so a
        ``startswith``/``endswith`` slip in the domain check is invisible when
        only the end-to-end path is tested — the binding lookup would deny the
        request anyway and the test would still pass. This asserts the predicate
        itself, which is what makes the exactness a real invariant.

        The last case is the reason the domain is taken from the LAST ``@``: an
        address may legally quote one in its local part.
        """
        from api.reviewer_auth import domain_allowed

        assert domain_allowed(email, ["example.invalid"]) is allowed

    def test_domain_allowed_refuses_an_empty_allowlist(self):
        from api.reviewer_auth import domain_allowed

        assert domain_allowed("reviewer@example.invalid", []) is False
        assert domain_allowed("reviewer@example.invalid", None) is False

    def test_domain_comparison_is_case_and_whitespace_normalized(self, monkeypatch):
        auth, _ = _authenticator(
            _settings(monkeypatch), claims=_claims(email="  ADMIN@Example.INVALID ")
        )
        reviewer = auth.authenticate(_headers())
        assert reviewer.identity.email == ADMIN_EMAIL
        assert reviewer.role is ReviewerRole.ADMIN

    def test_unsigned_email_header_alone_is_ignored(self, monkeypatch):
        auth, _ = _authenticator(_settings(monkeypatch), claims=_claims())
        with pytest.raises(AuthenticationFailed):
            auth.authenticate({IAP_EMAIL_HEADER: f"accounts.google.com:{ADMIN_EMAIL}"})

    def test_unsigned_headers_cannot_change_attribution(self, monkeypatch):
        auth, _ = _authenticator(
            _settings(monkeypatch), claims=_claims(email=VIEWER_EMAIL)
        )
        reviewer = auth.authenticate(
            _headers(
                **{
                    IAP_EMAIL_HEADER: f"accounts.google.com:{ADMIN_EMAIL}",
                    IAP_USER_ID_HEADER: "accounts.google.com:999",
                }
            )
        )
        assert reviewer.identity.email == VIEWER_EMAIL
        assert reviewer.identity.subject == "accounts.google.com:117300000000000000001"
        assert reviewer.role is ReviewerRole.VIEWER

    def test_display_name_is_taken_only_from_verified_claims(self, monkeypatch):
        auth, _ = _authenticator(
            _settings(monkeypatch), claims=_claims(name="Verified Person")
        )
        assert auth.authenticate(_headers()).identity.display_name == "Verified Person"


# =====================================================================
# 7-8. Bindings are the only source of privilege
# =====================================================================


class TestRoleBindings:

    def test_unbound_identity_is_denied(self, monkeypatch):
        auth, _ = _authenticator(
            _settings(monkeypatch), claims=_claims(email=UNBOUND_EMAIL)
        )
        with pytest.raises(AuthorizationFailed):
            auth.authenticate(_headers())

    def test_production_cannot_enable_a_default_viewer_role(self, monkeypatch):
        settings = _settings(
            monkeypatch, ALLOW_UNBOUND_VIEWERS=True, DEFAULT_ROLE="viewer"
        )
        with pytest.raises(AuthConfigurationError):
            validate_console_auth_startup(settings)

    def test_a_local_default_role_still_requires_the_opt_in(self, monkeypatch):
        """``DEFAULT_ROLE`` alone grants nothing without ``ALLOW_UNBOUND_VIEWERS``.

        Verified through the IAP path so the denial is the role decision and not
        the local-mode loopback check.
        """
        settings = _local_settings(
            monkeypatch,
            AUTH_MODE="iap",
            DEFAULT_ROLE="viewer",
            ALLOW_UNBOUND_VIEWERS=False,
            IAP_AUDIENCE=PROD_IAP_AUDIENCE,
        )
        auth, _ = _authenticator(settings, claims=_claims(email=UNBOUND_EMAIL))
        with pytest.raises(AuthorizationFailed):
            auth.authenticate(_headers())

    def test_a_local_opted_in_default_role_applies(self, monkeypatch):
        settings = _local_settings(
            monkeypatch,
            AUTH_MODE="iap",
            ALLOW_UNBOUND_VIEWERS=True,
            DEFAULT_ROLE="viewer",
            IAP_AUDIENCE=PROD_IAP_AUDIENCE,
        )
        auth, _ = _authenticator(settings, claims=_claims(email=UNBOUND_EMAIL))
        assert auth.authenticate(_headers()).role is ReviewerRole.VIEWER

    @pytest.mark.parametrize(
        "attack",
        [
            {"X-Tickets-Role": "admin"},
            {"X-Reviewer-Role": "admin"},
            {"Role": "admin"},
            {"X-Goog-Authenticated-User-Role": "admin"},
        ],
    )
    def test_a_header_cannot_escalate_a_binding(self, monkeypatch, attack):
        auth, _ = _authenticator(
            _settings(monkeypatch), claims=_claims(email=VIEWER_EMAIL)
        )
        assert auth.authenticate(_headers(**attack)).role is ReviewerRole.VIEWER

    def test_a_role_claim_inside_the_token_is_ignored(self, monkeypatch):
        """IAP does not mint application roles, so a ``role`` claim is noise."""
        auth, _ = _authenticator(
            _settings(monkeypatch), claims=_claims(email=VIEWER_EMAIL, role="admin")
        )
        assert auth.authenticate(_headers()).role is ReviewerRole.VIEWER

    def test_bindings_are_matched_on_the_normalized_email(self, monkeypatch):
        bindings = json.dumps({"  ADMIN@EXAMPLE.INVALID ": "admin"})
        auth, _ = _authenticator(
            _settings(monkeypatch, ROLE_BINDINGS_JSON=bindings),
            claims=_claims(email=ADMIN_EMAIL),
        )
        assert auth.authenticate(_headers()).role is ReviewerRole.ADMIN

    def test_parse_role_bindings_rejects_an_unknown_role(self):
        with pytest.raises(AuthConfigurationError):
            parse_role_bindings(json.dumps({ADMIN_EMAIL: "superuser"}))

    def test_parse_role_bindings_refuses_to_grant_the_agent_role(self):
        """``agent`` is an identity, not a grant.

        Only the exact configured service account may hold it; letting a JSON
        binding hand it to a human would give a browser session the CSRF and
        Fetch-Metadata exemption the remediation CLI needs.
        """
        with pytest.raises(AuthConfigurationError):
            parse_role_bindings(json.dumps({ADMIN_EMAIL: "agent"}))

    def test_parse_role_bindings_rejects_a_non_object(self):
        with pytest.raises(AuthConfigurationError):
            parse_role_bindings(json.dumps(["admin@example.invalid"]))

    def test_parse_role_bindings_rejects_invalid_json(self):
        with pytest.raises(AuthConfigurationError):
            parse_role_bindings("{not json")

    def test_parse_role_bindings_rejects_a_duplicate_normalized_email(self):
        raw = json.dumps({ADMIN_EMAIL: "admin", ADMIN_EMAIL.upper(): "viewer"})
        with pytest.raises(AuthConfigurationError):
            parse_role_bindings(raw)


# =====================================================================
# 9. The role ladder
# =====================================================================


class TestRoleHierarchy:

    def test_ladder_order(self):
        assert ROLE_LADDER == (
            ReviewerRole.VIEWER,
            ReviewerRole.REVIEWER,
            ReviewerRole.REMEDIATOR,
            ReviewerRole.ADMIN,
        )

    def test_agent_is_not_on_the_ladder(self):
        assert ReviewerRole.AGENT not in ROLE_LADDER

    @pytest.mark.parametrize(
        "held,minimum,expected",
        [
            (ReviewerRole.VIEWER, ReviewerRole.VIEWER, True),
            (ReviewerRole.VIEWER, ReviewerRole.REVIEWER, False),
            (ReviewerRole.REVIEWER, ReviewerRole.VIEWER, True),
            (ReviewerRole.REVIEWER, ReviewerRole.REVIEWER, True),
            (ReviewerRole.REVIEWER, ReviewerRole.REMEDIATOR, False),
            (ReviewerRole.REMEDIATOR, ReviewerRole.REVIEWER, True),
            (ReviewerRole.REMEDIATOR, ReviewerRole.ADMIN, False),
            (ReviewerRole.ADMIN, ReviewerRole.ADMIN, True),
            (ReviewerRole.ADMIN, ReviewerRole.VIEWER, True),
        ],
    )
    def test_role_at_least(self, held, minimum, expected):
        assert role_at_least(held, minimum) is expected

    @pytest.mark.parametrize("minimum", list(ROLE_LADDER))
    def test_agent_never_satisfies_a_human_minimum(self, minimum):
        """The agent is not a very privileged viewer; it is a different kind.

        Remediation batch routes arrive in Stage 8 and will check for the agent
        identity explicitly. Until then -- and after -- ``agent`` must fail every
        ladder comparison, so a leaked agent credential cannot read the queue.
        """
        assert role_at_least(ReviewerRole.AGENT, minimum) is False

    def test_a_human_role_never_satisfies_the_agent_minimum(self):
        for role in ROLE_LADDER:
            assert role_at_least(role, ReviewerRole.AGENT) is False


# =====================================================================
# The remediation agent's service-account identity
# =====================================================================


class TestAgentIdentity:

    def test_the_exact_service_account_email_maps_to_agent(self, monkeypatch):
        auth, _ = _authenticator(
            _settings(monkeypatch), claims=_claims(email=AGENT_SA, sub="sa-unique-id")
        )
        reviewer = auth.authenticate(_headers())
        assert reviewer.role is ReviewerRole.AGENT
        assert reviewer.is_agent is True

    def test_the_agent_identity_bypasses_the_human_domain_allowlist(self, monkeypatch):
        """A service account lives on ``iam.gserviceaccount.com``.

        Its email can never satisfy ``ALLOWED_EMAIL_DOMAINS``, so the exact-email
        match must be evaluated before the domain policy or the agent could never
        authenticate at all.
        """
        settings = _settings(monkeypatch, ALLOWED_EMAIL_DOMAINS=["example.invalid"])
        auth, _ = _authenticator(settings, claims=_claims(email=AGENT_SA))
        assert auth.authenticate(_headers()).role is ReviewerRole.AGENT

    @pytest.mark.parametrize(
        "email",
        [
            "tickets-remediation-agent@rag-kb-system.iam.gserviceaccount.com.attacker.tld",
            "xtickets-remediation-agent@rag-kb-system.iam.gserviceaccount.com",
            "tickets-remediation-agent@rag-kb-system.iam.gserviceaccount.co",
        ],
    )
    def test_a_near_miss_service_account_is_not_the_agent(self, monkeypatch, email):
        auth, _ = _authenticator(_settings(monkeypatch), claims=_claims(email=email))
        with pytest.raises(AuthorizationFailed):
            auth.authenticate(_headers())

    def test_an_unconfigured_agent_account_grants_nothing(self, monkeypatch):
        settings = _settings(
            monkeypatch, AGENT_SERVICE_ACCOUNT="", AGENT_IAP_TARGET_AUDIENCE=""
        )
        auth, _ = _authenticator(settings, claims=_claims(email=AGENT_SA))
        with pytest.raises(AuthorizationFailed):
            auth.authenticate(_headers())

    def test_the_agent_email_match_is_case_normalized(self, monkeypatch):
        auth, _ = _authenticator(
            _settings(monkeypatch), claims=_claims(email=AGENT_SA.upper())
        )
        assert auth.authenticate(_headers()).role is ReviewerRole.AGENT


class TestAgentTargetAudience:
    """The programmatic-request audience is a *different* audience.

    IAP validates ``https://<console-host>/*`` when the CLI presents its
    service-account ID token; the application validates
    ``/projects/N/locations/R/services/S`` on the assertion IAP then mints. The
    two must never be interchangeable, and the path wildcard is mandatory
    because the CLI calls several ``/api/admin/v1/**`` paths.
    """

    def test_the_documented_wildcard_suffix_is_pinned(self):
        assert AGENT_AUDIENCE_SUFFIX == "/*"

    def test_a_valid_audience_is_accepted(self):
        assert validate_agent_audience(AGENT_AUDIENCE, console_origin=CONSOLE_ORIGIN) == (
            AGENT_AUDIENCE
        )

    def test_a_bare_base_url_audience_is_refused(self):
        with pytest.raises(AuthConfigurationError):
            validate_agent_audience(CONSOLE_ORIGIN, console_origin=CONSOLE_ORIGIN)

    def test_a_trailing_slash_without_the_wildcard_is_refused(self):
        with pytest.raises(AuthConfigurationError):
            validate_agent_audience(f"{CONSOLE_ORIGIN}/", console_origin=CONSOLE_ORIGIN)

    def test_a_wrong_host_is_refused(self):
        with pytest.raises(AuthConfigurationError):
            validate_agent_audience(
                "https://attacker.example/*", console_origin=CONSOLE_ORIGIN
            )

    def test_another_service_path_is_refused(self):
        with pytest.raises(AuthConfigurationError):
            validate_agent_audience(
                f"{CONSOLE_ORIGIN}/api/admin/v1/*", console_origin=CONSOLE_ORIGIN
            )

    def test_a_plain_http_audience_is_refused(self):
        with pytest.raises(AuthConfigurationError):
            validate_agent_audience(
                "http://tickets-console-abc-uc.a.run.app/*",
                console_origin=CONSOLE_ORIGIN,
            )

    def test_a_query_or_fragment_is_refused(self):
        for candidate in (f"{CONSOLE_ORIGIN}/*?x=1", f"{CONSOLE_ORIGIN}/*#x"):
            with pytest.raises(AuthConfigurationError):
                validate_agent_audience(candidate, console_origin=CONSOLE_ORIGIN)

    def test_the_iap_assertion_audience_is_not_a_valid_agent_audience(self):
        with pytest.raises(AuthConfigurationError):
            validate_agent_audience(PROD_IAP_AUDIENCE, console_origin=CONSOLE_ORIGIN)

    def test_startup_refuses_the_two_audiences_being_equal(self, monkeypatch):
        settings = _settings(
            monkeypatch,
            IAP_AUDIENCE=AGENT_AUDIENCE,
            AGENT_IAP_TARGET_AUDIENCE=AGENT_AUDIENCE,
        )
        with pytest.raises(AuthConfigurationError):
            validate_console_auth_startup(settings)

    def test_startup_refuses_an_agent_account_without_its_audience(self, monkeypatch):
        settings = _settings(monkeypatch, AGENT_IAP_TARGET_AUDIENCE="")
        with pytest.raises(AuthConfigurationError):
            validate_console_auth_startup(settings)

    def test_startup_refuses_an_agent_audience_without_its_account(self, monkeypatch):
        settings = _settings(monkeypatch, AGENT_SERVICE_ACCOUNT="")
        with pytest.raises(AuthConfigurationError):
            validate_console_auth_startup(settings)

    def test_an_agent_assertion_audience_confusion_is_rejected_at_runtime(
        self, monkeypatch
    ):
        """The CLI's target audience must not verify as an assertion audience."""
        auth, _ = _authenticator(
            _settings(monkeypatch), claims=_claims(email=AGENT_SA, aud=AGENT_AUDIENCE)
        )
        with pytest.raises(AuthenticationFailed):
            auth.authenticate(_headers())


# =====================================================================
# 10. Local authentication is five independent conditions
# =====================================================================


class TestLocalAuthMode:

    def test_local_fixture_identity_authenticates_on_loopback(self, monkeypatch):
        settings = _local_settings(monkeypatch)
        auth, _ = _authenticator(settings)
        reviewer = auth.authenticate(
            {LOCAL_REVIEWER_HEADER: ADMIN_EMAIL}, client_host="127.0.0.1"
        )
        assert reviewer.identity.email == ADMIN_EMAIL
        assert reviewer.role is ReviewerRole.ADMIN

    def test_local_mode_still_requires_a_binding(self, monkeypatch):
        auth, _ = _authenticator(_local_settings(monkeypatch))
        with pytest.raises(AuthorizationFailed):
            auth.authenticate(
                {LOCAL_REVIEWER_HEADER: UNBOUND_EMAIL}, client_host="127.0.0.1"
            )

    def test_local_mode_refuses_a_non_loopback_client(self, monkeypatch):
        auth, _ = _authenticator(_local_settings(monkeypatch))
        with pytest.raises(AuthenticationFailed):
            auth.authenticate(
                {LOCAL_REVIEWER_HEADER: ADMIN_EMAIL}, client_host="10.0.0.7"
            )

    def test_local_mode_requires_the_opt_in_flag(self, monkeypatch):
        settings = _local_settings(monkeypatch, ALLOW_LOCAL_AUTH=False)
        auth, _ = _authenticator(settings)
        with pytest.raises(AuthenticationFailed):
            auth.authenticate(
                {LOCAL_REVIEWER_HEADER: ADMIN_EMAIL}, client_host="127.0.0.1"
            )

    def test_local_mode_requires_the_local_environment(self, monkeypatch):
        settings = _local_settings(monkeypatch, ENVIRONMENT="staging")
        auth, _ = _authenticator(settings)
        with pytest.raises(AuthenticationFailed):
            auth.authenticate(
                {LOCAL_REVIEWER_HEADER: ADMIN_EMAIL}, client_host="127.0.0.1"
            )

    def test_local_header_is_inert_in_iap_mode(self, monkeypatch):
        settings = _local_settings(monkeypatch, AUTH_MODE="iap")
        auth, _ = _authenticator(settings)
        with pytest.raises(AuthenticationFailed):
            auth.authenticate(
                {LOCAL_REVIEWER_HEADER: ADMIN_EMAIL}, client_host="127.0.0.1"
            )

    @pytest.mark.parametrize("environment", ["staging", "production"])
    @pytest.mark.parametrize(
        "overrides",
        [
            {"AUTH_MODE": "local", "ALLOW_LOCAL_AUTH": True},
            {"AUTH_MODE": "local", "ALLOW_LOCAL_AUTH": False},
            {"AUTH_MODE": "iap", "ALLOW_LOCAL_AUTH": True},
        ],
    )
    def test_every_deployed_local_auth_combination_fails_at_startup(
        self, monkeypatch, environment, overrides
    ):
        settings = _settings(monkeypatch, ENVIRONMENT=environment, **overrides)
        with pytest.raises(AuthConfigurationError):
            validate_console_auth_startup(settings)

    def test_a_valid_production_configuration_passes_startup(self, monkeypatch):
        assert validate_console_auth_startup(_settings(monkeypatch)) is True

    def test_a_valid_local_configuration_passes_startup(self, monkeypatch):
        assert validate_console_auth_startup(_local_settings(monkeypatch)) is True

    @pytest.mark.parametrize("environment", ["staging", "production"])
    def test_synthetic_verification_cannot_be_enabled_when_deployed(
        self, monkeypatch, environment
    ):
        """Staging may enable it; production may not, and neither may skip IAP.

        The plan allows the synthetic-verification handoff in staging, so this
        asserts the *production* refusal plus the fact that enabling it never
        relaxes authentication mode.
        """
        settings = _settings(
            monkeypatch, ENVIRONMENT=environment, ENABLE_SYNTHETIC_VERIFICATION=True
        )
        if environment == "production":
            with pytest.raises(AuthConfigurationError):
                validate_console_auth_startup(settings)
        else:
            assert validate_console_auth_startup(settings) is True


# =====================================================================
# 11. Logs carry a subject hash, never the assertion
# =====================================================================


class TestAuthLogging:

    def test_a_successful_authentication_logs_no_token_or_email(
        self, monkeypatch, caplog
    ):
        auth, _ = _authenticator(_settings(monkeypatch), claims=_claims())
        with caplog.at_level(logging.DEBUG, logger="api.reviewer_auth"):
            reviewer = auth.authenticate(_headers(), request_id="req-1")
        text = "\n".join(record.getMessage() for record in caplog.records)
        assert FAKE_ASSERTION not in text
        assert REVIEWER_EMAIL not in text
        assert reviewer.identity.subject not in text

    def test_a_rejection_logs_the_reason_without_the_token(self, monkeypatch, caplog):
        auth, _ = _authenticator(
            _settings(monkeypatch), error=ValueError("Could not verify token signature.")
        )
        with caplog.at_level(logging.DEBUG, logger="api.reviewer_auth"):
            with pytest.raises(AuthenticationFailed):
                auth.authenticate(_headers(), request_id="req-2")
        text = "\n".join(record.getMessage() for record in caplog.records)
        assert FAKE_ASSERTION not in text
        assert "req-2" in text

    def test_the_subject_hash_is_stable_and_not_the_subject(self, monkeypatch):
        auth, _ = _authenticator(_settings(monkeypatch), claims=_claims())
        first = auth.authenticate(_headers())
        second = auth.authenticate(_headers())
        assert first.subject_hash == second.subject_hash
        assert len(first.subject_hash) == 64
        assert first.identity.subject not in first.subject_hash

    def test_the_reviewer_repr_hides_the_email(self, monkeypatch):
        auth, _ = _authenticator(_settings(monkeypatch), claims=_claims())
        reviewer = auth.authenticate(_headers())
        assert REVIEWER_EMAIL not in repr(reviewer)
