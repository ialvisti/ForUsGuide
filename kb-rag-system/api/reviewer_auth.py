"""Cryptographically verified reviewer identity for the /tickets console.

Production trusts exactly one thing: a signed IAP JWT in
``X-Goog-IAP-JWT-Assertion`` whose audience is the configured Cloud Run
service. Everything else a request can say about who it is — the unsigned
``X-Goog-Authenticated-User-Email`` convenience headers, a ``role`` claim, a
query parameter, a body field — is inert here.

Why the unsigned headers are refused
------------------------------------
IAP also forwards ``X-Goog-Authenticated-User-Email``. It is *not* signed, so
any client that can reach the service directly (a misconfigured ingress, a
same-VPC caller, a future sidecar) can set it. Reading it would make the whole
IAP verification decorative, so this module never looks at it except to prove,
in tests, that it changes nothing.

Two distinct audiences
----------------------
The remediation CLI (Stage 8) authenticates to *IAP* with a keyless
service-account ID token whose audience is ``https://<console-host>/*`` — a
path wildcard, because the CLI calls several ``/api/admin/v1/**`` paths and a
bare base URL would not cover them. IAP validates that one. This application
then validates the assertion IAP mints, whose audience is
``/projects/{N}/locations/{R}/services/{S}``. Confusing the two would be a
privilege boundary failure in either direction, so
:func:`validate_agent_audience` and :func:`validate_console_auth_startup`
refuse every overlap, and the runtime audience check refuses a token minted for
the wrong one.

Roles
-----
``viewer < reviewer < remediator < admin`` is a ladder. ``agent`` is
deliberately *not* on it: the remediation service account is a different kind
of caller, not a very privileged human, and a leaked agent credential must not
be able to read the review queue. :func:`role_at_least` therefore fails every
comparison in both directions for ``agent``, and Stage 8's batch routes will
ask for that identity explicitly.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from api.ticket_review_models import ReviewerIdentity, ReviewerRole, utc_now
from api.tickets_console_config import (
    STRICT_ENVIRONMENTS,
    VALID_AUTH_MODES,
    VALID_ENVIRONMENTS,
    TicketConsoleSettings,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The documented IAP contract. Pinned as constants so a typo cannot silently
# verify against the wrong keyset or accept the wrong issuer.
# ---------------------------------------------------------------------------
IAP_ASSERTION_HEADER = "X-Goog-IAP-JWT-Assertion"
IAP_ISSUER = "https://cloud.google.com/iap"
IAP_PUBLIC_KEY_URL = "https://www.gstatic.com/iap/verify/public_key"

# Forwarded by IAP, unsigned, and never read for authorization.
IAP_EMAIL_HEADER = "X-Goog-Authenticated-User-Email"
IAP_USER_ID_HEADER = "X-Goog-Authenticated-User-ID"

# The local fixture identity header. Inert unless five independent conditions
# hold; see :meth:`ReviewerAuthenticator._local_identity`.
LOCAL_REVIEWER_HEADER = "X-Tickets-Local-Reviewer"

# Starlette's TestClient synthesizes this peer name, and a local fixture run
# binds loopback. Both are only ever consulted after ENVIRONMENT=local,
# AUTH_MODE=local and ALLOW_LOCAL_AUTH have all been proven true, and
# `validate_console_auth_startup` refuses that combination when deployed.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})

# The programmatic-request audience must end in exactly this.
AGENT_AUDIENCE_SUFFIX = "/*"

#: Privilege ladder, least to most. ``agent`` is intentionally absent.
ROLE_LADDER: tuple[ReviewerRole, ...] = (
    ReviewerRole.VIEWER,
    ReviewerRole.REVIEWER,
    ReviewerRole.REMEDIATOR,
    ReviewerRole.ADMIN,
)

_LADDER_RANK: dict[ReviewerRole, int] = {
    role: index for index, role in enumerate(ROLE_LADDER)
}


# ---------------------------------------------------------------------------
# Typed failures
# ---------------------------------------------------------------------------


class ReviewerAuthError(Exception):
    """Base class for every identity failure in the admin plane."""

    #: Stable public error code. Never carries a reason detail.
    code = "UNAUTHENTICATED"
    #: The HTTP status this failure maps to.
    status = 401


class AuthenticationFailed(ReviewerAuthError):
    """No usable verified identity: absent, malformed, or rejected assertion.

    One type for every cause, and one public message. Telling a caller that the
    signature was fine but the issuer was wrong maps out the boundary for them.
    """

    code = "UNAUTHENTICATED"
    status = 401


class AuthorizationFailed(ReviewerAuthError):
    """The identity is verified but holds no privilege here."""

    code = "FORBIDDEN"
    status = 403


class AuthVerifierUnavailable(ReviewerAuthError):
    """The verifier itself could not run — typically unreachable certificates.

    Deliberately not a 401: an outage is not evidence that the caller is
    anonymous, and answering 401 would invite clients to retry unauthenticated.
    """

    code = "AUTH_VERIFIER_UNAVAILABLE"
    status = 503


class AuthConfigurationError(ReviewerAuthError):
    """This revision must not serve traffic with this configuration."""

    code = "AUTH_MISCONFIGURED"
    status = 500


# ---------------------------------------------------------------------------
# Verified identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthenticatedReviewer:
    """A verified identity plus the role the server bound to it.

    ``repr`` is overridden because this object ends up in log records, exception
    context, and debugger output, and ``ReviewerIdentity`` carries an email
    address.
    """

    identity: ReviewerIdentity
    role: ReviewerRole
    #: True when the console is talking to the remediation service account.
    is_agent: bool = False
    #: Present only in local fixture mode; never in a deployed environment.
    local: bool = False
    _subject_hash: str = field(default="", repr=False, compare=False)

    @property
    def subject_hash(self) -> str:
        """A stable, non-reversing handle for logs and metrics."""
        return self._subject_hash or subject_hash(self.identity.subject)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"AuthenticatedReviewer(role={self.role.value!r}, "
            f"subject_hash={self.subject_hash[:12]!r}, is_agent={self.is_agent})"
        )


def subject_hash(subject: str) -> str:
    """Hash an IAP subject for logging.

    The subject is a low-entropy identifier, so this is a correlation handle
    rather than a secret — but it is not the identifier itself, which is what
    keeps a log export from becoming a list of who reviewed what.
    """
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()


def role_at_least(held: ReviewerRole, minimum: ReviewerRole) -> bool:
    """Return whether ``held`` satisfies ``minimum`` on the human ladder.

    ``agent`` is off the ladder in both directions: it satisfies no human
    minimum, and no human role satisfies a demand for ``agent``.
    """
    if held is ReviewerRole.AGENT or minimum is ReviewerRole.AGENT:
        return False
    try:
        return _LADDER_RANK[held] >= _LADDER_RANK[minimum]
    except KeyError:  # pragma: no cover - defensive; the enum is closed
        return False


# ---------------------------------------------------------------------------
# Role bindings
# ---------------------------------------------------------------------------


def parse_role_bindings(raw: object) -> dict[str, ReviewerRole]:
    """Parse ``ROLE_BINDINGS_JSON`` into a normalized email → role map.

    Rejects, rather than ignores, anything malformed: a binding file that half
    parses would silently demote reviewers to "unbound" and the console would
    look broken rather than misconfigured.

    ``agent`` may not be granted here. It belongs to exactly one configured
    service account, and a JSON binding that handed it to a human would also
    hand them the CSRF and Fetch-Metadata exemption the remediation CLI needs.
    """
    text = _secret_text(raw)
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AuthConfigurationError("ROLE_BINDINGS_JSON must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise AuthConfigurationError(
            "ROLE_BINDINGS_JSON must be an object mapping identity to role"
        )
    bindings: dict[str, ReviewerRole] = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise AuthConfigurationError("ROLE_BINDINGS_JSON entries must be strings")
        email = key.strip().lower()
        if not email:
            raise AuthConfigurationError("ROLE_BINDINGS_JSON has a blank identity")
        try:
            role = ReviewerRole(value.strip().lower())
        except ValueError as exc:
            # The unknown role name is not echoed: the file is a secret.
            raise AuthConfigurationError(
                "ROLE_BINDINGS_JSON names a role that does not exist"
            ) from exc
        if role is ReviewerRole.AGENT:
            raise AuthConfigurationError(
                "the 'agent' role cannot be granted by a role binding; it belongs "
                "to the configured remediation service account only"
            )
        if email in bindings:
            # Two spellings of one address with different roles have no
            # defensible resolution, and picking one silently would be a
            # privilege decision made by dict ordering.
            raise AuthConfigurationError(
                "ROLE_BINDINGS_JSON binds the same identity twice"
            )
        bindings[email] = role
    return bindings


def _secret_text(value: object) -> str:
    """Read a ``SecretStr`` or ``str`` without widening it into a log line."""
    getter = getattr(value, "get_secret_value", None)
    if callable(getter):
        raw = getter()
        return raw.strip() if isinstance(raw, str) else ""
    return value.strip() if isinstance(value, str) else ""


def email_domain(email: str) -> str:
    """Return the normalized domain of an address, or ``""``.

    Splits on the LAST ``@``: an address may legally quote one in its local
    part, and taking the first would compare the wrong string.
    """
    normalized = email.strip().lower()
    if "@" not in normalized:
        return ""
    return normalized.rsplit("@", 1)[1]


def domain_allowed(email: str, domains: object) -> bool:
    """Exact, normalized domain membership.

    Exact is the whole point: ``user@forusall.com.attacker.tld`` has the
    allowed domain as a *prefix* of its own, and a ``startswith``/``in`` test
    would admit it. Subdomains are not implied either — if a deployment needs
    one, it configures it.
    """
    candidate = email_domain(email)
    if not candidate:
        return False
    allowed = {
        str(domain).strip().lower()
        for domain in (domains or ())
        if str(domain).strip()
    }
    return candidate in allowed


# ---------------------------------------------------------------------------
# Audience validation
# ---------------------------------------------------------------------------


def validate_agent_audience(audience: str, *, console_origin: str) -> str:
    """Validate the remediation CLI's IAP target audience, or raise.

    The official path-wildcard flow is ``https://<host>/*`` and nothing else:

    * a bare base URL does not cover ``/api/admin/v1/**``, so the CLI's second
      call would be rejected by IAP after the first succeeded;
    * a narrower path (``/api/admin/v1/*``) is a different audience than the one
      IAP will actually check, which fails the same way but later;
    * a query or fragment is not part of an audience at all;
    * plain http would let the token travel in clear.
    """
    value = (audience or "").strip()
    origin = (console_origin or "").strip().rstrip("/")
    if not value:
        raise AuthConfigurationError("AGENT_IAP_TARGET_AUDIENCE is required")
    if not origin:
        raise AuthConfigurationError(
            "CONSOLE_ORIGIN is required to validate AGENT_IAP_TARGET_AUDIENCE"
        )
    if not value.endswith(AGENT_AUDIENCE_SUFFIX):
        raise AuthConfigurationError(
            "AGENT_IAP_TARGET_AUDIENCE must end with the '/*' path wildcard; a bare "
            "base URL does not cover the CLI's /api/admin/v1 paths"
        )
    parsed = urlparse(value)
    if parsed.scheme != "https":
        raise AuthConfigurationError("AGENT_IAP_TARGET_AUDIENCE must be https")
    if parsed.query or parsed.fragment or parsed.params:
        raise AuthConfigurationError(
            "AGENT_IAP_TARGET_AUDIENCE must carry no query or fragment"
        )
    if parsed.path != AGENT_AUDIENCE_SUFFIX:
        raise AuthConfigurationError(
            "AGENT_IAP_TARGET_AUDIENCE must be exactly the console origin plus '/*'"
        )
    if value != f"{origin}{AGENT_AUDIENCE_SUFFIX}":
        raise AuthConfigurationError(
            "AGENT_IAP_TARGET_AUDIENCE must name the configured console origin"
        )
    return value


def validate_console_origin(origin: str, *, environment: str) -> str:
    """Validate the console's own origin: scheme + host, never a path.

    ``Origin`` headers are scheme/host/port only, so a configured value with a
    path could never match one and every unsafe request would 403.
    """
    value = (origin or "").strip()
    if not value:
        raise AuthConfigurationError("CONSOLE_ORIGIN is required")
    parsed = urlparse(value)
    strict = environment in STRICT_ENVIRONMENTS
    if parsed.scheme not in ({"https"} if strict else {"http", "https"}):
        raise AuthConfigurationError(
            "CONSOLE_ORIGIN must be https" if strict else "CONSOLE_ORIGIN must be http(s)"
        )
    if not parsed.netloc:
        raise AuthConfigurationError("CONSOLE_ORIGIN must name a host")
    if parsed.path.rstrip("/") or parsed.query or parsed.fragment:
        raise AuthConfigurationError(
            "CONSOLE_ORIGIN must be scheme://host[:port] with no path"
        )
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def validate_console_auth_startup(settings: TicketConsoleSettings) -> bool:
    """Refuse to serve when identity configuration is unsafe. Returns ``True``.

    Accumulates every problem so one restart reports all of them, and never
    echoes a secret value.
    """
    errors: list[str] = []
    environment = (settings.ENVIRONMENT or "").strip()
    auth_mode = (settings.AUTH_MODE or "").strip()
    strict = environment in STRICT_ENVIRONMENTS

    if environment not in VALID_ENVIRONMENTS:
        errors.append(f"ENVIRONMENT must be one of {sorted(VALID_ENVIRONMENTS)}")
    if auth_mode not in VALID_AUTH_MODES:
        errors.append(f"AUTH_MODE must be one of {sorted(VALID_AUTH_MODES)}")

    # Local authentication may not exist in a deployed environment in ANY
    # combination — not the mode, not the opt-in, not a leftover default role.
    if strict:
        if auth_mode != "iap":
            errors.append(f"AUTH_MODE must be 'iap' in {environment}")
        if settings.ALLOW_LOCAL_AUTH:
            errors.append(f"ALLOW_LOCAL_AUTH must be false in {environment}")
        if settings.ALLOW_UNBOUND_VIEWERS:
            errors.append(f"ALLOW_UNBOUND_VIEWERS must be false in {environment}")
        if settings.DEFAULT_ROLE is not None:
            errors.append("DEFAULT_ROLE must be unset; an unbound identity is denied")
        if not (settings.IAP_AUDIENCE or "").strip():
            errors.append("IAP_AUDIENCE is required")
        if environment == "production" and settings.ENABLE_SYNTHETIC_VERIFICATION:
            errors.append("ENABLE_SYNTHETIC_VERIFICATION must be false in production")
    elif auth_mode == "local" and not settings.ALLOW_LOCAL_AUTH:
        errors.append("AUTH_MODE=local requires ALLOW_LOCAL_AUTH=true")

    try:
        origin = validate_console_origin(settings.CONSOLE_ORIGIN, environment=environment)
    except AuthConfigurationError as exc:
        origin = ""
        errors.append(str(exc))

    try:
        bindings = parse_role_bindings(settings.ROLE_BINDINGS_JSON)
    except AuthConfigurationError as exc:
        bindings = {}
        errors.append(str(exc))
    if strict and not bindings:
        errors.append("ROLE_BINDINGS_JSON must bind at least one identity")

    if settings.DEFAULT_ROLE is not None:
        try:
            default_role = ReviewerRole(str(settings.DEFAULT_ROLE).strip().lower())
        except ValueError:
            errors.append("DEFAULT_ROLE names a role that does not exist")
        else:
            if default_role is ReviewerRole.AGENT:
                errors.append("DEFAULT_ROLE must never be 'agent'")

    if strict and not domains_configured(settings.ALLOWED_EMAIL_DOMAINS):
        errors.append("ALLOWED_EMAIL_DOMAINS must be a non-empty explicit list")

    # The agent identity and its audience are all-or-nothing: half of the pair
    # is either a caller that cannot reach IAP or an audience nobody may use.
    agent_account = (settings.AGENT_SERVICE_ACCOUNT or "").strip()
    agent_audience = (settings.AGENT_IAP_TARGET_AUDIENCE or "").strip()
    if agent_account and not agent_audience:
        errors.append(
            "AGENT_SERVICE_ACCOUNT requires AGENT_IAP_TARGET_AUDIENCE"
        )
    if agent_audience and not agent_account:
        errors.append(
            "AGENT_IAP_TARGET_AUDIENCE requires AGENT_SERVICE_ACCOUNT"
        )
    if agent_audience and origin:
        try:
            validate_agent_audience(agent_audience, console_origin=origin)
        except AuthConfigurationError as exc:
            errors.append(str(exc))
    if agent_audience and agent_audience == (settings.IAP_AUDIENCE or "").strip():
        errors.append(
            "AGENT_IAP_TARGET_AUDIENCE must differ from IAP_AUDIENCE: one is "
            "validated by IAP, the other by this application"
        )

    if errors:
        raise AuthConfigurationError(
            "Invalid tickets console authentication configuration: " + "; ".join(errors)
        )
    return True


def domains_configured(domains: object) -> bool:
    """Whether an explicit, wildcard-free domain allowlist is present."""
    values = [str(domain).strip().lower() for domain in (domains or ())]
    return bool(values) and all(value and "*" not in value and "." in value for value in values)


# ---------------------------------------------------------------------------
# The verifier seam
# ---------------------------------------------------------------------------

ClaimsVerifier = Callable[[str, str], Mapping[str, Any]]


def default_iap_claims_verifier(token: str, audience: str) -> Mapping[str, Any]:
    """Verify a real IAP assertion against Google's published IAP keys.

    Imported lazily and constructed per call so that importing this module — or
    the app that uses it — never needs network access or ADC. The signature
    matches the evidence broker's verifier seam
    (``api.tickets_evidence_broker_main._default_claims_verifier``) on purpose:
    both are replaced the same way in tests.
    """
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    return id_token.verify_token(
        token,
        google_requests.Request(),
        audience=audience,
        certs_url=IAP_PUBLIC_KEY_URL,
    )


class ReviewerAuthenticator:
    """Turns one request's headers into a verified, role-bound identity."""

    def __init__(
        self,
        settings: TicketConsoleSettings,
        *,
        claims_verifier: Optional[ClaimsVerifier] = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._settings = settings
        self._verify = claims_verifier or default_iap_claims_verifier
        self._clock = clock
        self._bindings = parse_role_bindings(settings.ROLE_BINDINGS_JSON)
        self._agent_account = (settings.AGENT_SERVICE_ACCOUNT or "").strip().lower()

    @classmethod
    def from_settings(
        cls,
        settings: TicketConsoleSettings,
        *,
        claims_verifier: Optional[ClaimsVerifier] = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> "ReviewerAuthenticator":
        return cls(settings, claims_verifier=claims_verifier, clock=clock)

    # -- public API ----------------------------------------------------

    @property
    def auth_mode(self) -> str:
        return (self._settings.AUTH_MODE or "").strip()

    def authenticate(
        self,
        headers: Mapping[str, str],
        *,
        client_host: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> AuthenticatedReviewer:
        """Resolve the caller, or raise a typed failure.

        ``headers`` may be any mapping; lookup is case-insensitive so a plain
        dict in a test behaves like Starlette's ``Headers``.
        """
        lookup = _CaseInsensitive(headers)
        if self._local_mode_active():
            identity = self._local_identity(lookup, client_host=client_host)
            return self._bind(identity, request_id=request_id, local=True)
        return self._bind(
            self._iap_identity(lookup, request_id=request_id), request_id=request_id
        )

    # -- IAP -----------------------------------------------------------

    def _iap_identity(
        self, headers: "_CaseInsensitive", *, request_id: Optional[str]
    ) -> ReviewerIdentity:
        assertion = (headers.get(IAP_ASSERTION_HEADER) or "").strip()
        if not assertion:
            logger.info(
                "console auth rejected; reason=assertion_absent request_id=%s",
                request_id,
            )
            raise AuthenticationFailed("an IAP assertion is required")

        audience = (self._settings.IAP_AUDIENCE or "").strip()
        if not audience:
            # Verifying against an empty audience would accept any IAP-signed
            # token from any service in the organization.
            raise AuthConfigurationError("IAP_AUDIENCE is required to verify a caller")

        try:
            claims = self._verify(assertion, audience)
        except (ValueError, KeyError) as exc:
            # google-auth raises ValueError for a bad signature, a wrong
            # audience, an elapsed exp, and a future iat.
            logger.info(
                "console auth rejected; reason=assertion_invalid error_type=%s "
                "request_id=%s",
                type(exc).__name__,
                request_id,
            )
            raise AuthenticationFailed("the IAP assertion is not valid") from exc
        except Exception as exc:  # noqa: BLE001 - an outage is not a 401
            logger.error(
                "console auth verifier unavailable; error_type=%s request_id=%s",
                type(exc).__name__,
                request_id,
            )
            raise AuthVerifierUnavailable("identity verification is unavailable") from exc

        if not isinstance(claims, Mapping):
            raise AuthenticationFailed("the IAP assertion is not valid")
        return self._identity_from_claims(claims, audience=audience, request_id=request_id)

    def _identity_from_claims(
        self,
        claims: Mapping[str, Any],
        *,
        audience: str,
        request_id: Optional[str],
    ) -> ReviewerIdentity:
        if claims.get("iss") != IAP_ISSUER:
            logger.info(
                "console auth rejected; reason=issuer request_id=%s", request_id
            )
            raise AuthenticationFailed("the IAP assertion is not valid")
        # Re-check the audience even though the verifier was given it: a
        # verifier constructed without one (or a future refactor that drops the
        # argument) would otherwise accept any service's token.
        token_audience = claims.get("aud")
        if not isinstance(token_audience, str) or not hmac.compare_digest(
            token_audience, audience
        ):
            logger.info(
                "console auth rejected; reason=audience request_id=%s", request_id
            )
            raise AuthenticationFailed("the IAP assertion is not valid")

        subject = claims.get("sub")
        email = claims.get("email")
        if not isinstance(subject, str) or not subject.strip():
            raise AuthenticationFailed("the IAP assertion carries no subject")
        if not isinstance(email, str) or not email.strip():
            raise AuthenticationFailed("the IAP assertion carries no email")

        display_name = claims.get("name")
        try:
            identity = ReviewerIdentity(
                subject=subject.strip(),
                email=email,
                display_name=(
                    display_name.strip()[:200]
                    if isinstance(display_name, str) and display_name.strip()
                    else None
                ),
            )
        except ValueError as exc:
            # A malformed email is an authentication failure, not a 500.
            raise AuthenticationFailed("the IAP assertion carries no usable email") from exc

        # A hosted-domain claim, when IAP provides one, must not contradict
        # policy. It is a cross-check, not the policy itself.
        hosted = claims.get("hd")
        if isinstance(hosted, str) and hosted.strip():
            if not self._is_agent(identity.email) and not domain_allowed(
                f"probe@{hosted.strip()}", self._settings.ALLOWED_EMAIL_DOMAINS
            ):
                raise AuthorizationFailed("that identity is not permitted here")
        return identity

    # -- local fixture mode --------------------------------------------

    def _local_mode_active(self) -> bool:
        settings = self._settings
        return (
            (settings.ENVIRONMENT or "").strip() == "local"
            and self.auth_mode == "local"
            and bool(settings.ALLOW_LOCAL_AUTH)
        )

    def _local_identity(
        self, headers: "_CaseInsensitive", *, client_host: Optional[str]
    ) -> ReviewerIdentity:
        """Accept a fixture identity, but only from a loopback peer.

        Four conditions are already proven by ``_local_mode_active``; the peer
        check is the fifth, and ``validate_console_auth_startup`` makes the
        whole path impossible in staging and production.
        """
        host = (client_host or "").strip().lower()
        if host not in LOOPBACK_HOSTS:
            raise AuthenticationFailed("local authentication requires a loopback client")
        raw = (headers.get(LOCAL_REVIEWER_HEADER) or "").strip()
        if not raw:
            raise AuthenticationFailed("a local fixture identity is required")
        try:
            return ReviewerIdentity(
                subject=f"local:{raw.lower()}", email=raw, display_name=None
            )
        except ValueError as exc:
            raise AuthenticationFailed("the local fixture identity is not an email") from exc

    # -- role binding --------------------------------------------------

    def _is_agent(self, email: str) -> bool:
        account = self._agent_account
        # An unconfigured account must not match a blank email, and the compare
        # is constant-time to keep the SA address out of timing reach.
        return bool(account) and hmac.compare_digest(email.strip().lower(), account)

    def _bind(
        self,
        identity: ReviewerIdentity,
        *,
        request_id: Optional[str],
        local: bool = False,
    ) -> AuthenticatedReviewer:
        """Attach the server-side role. The request never influences this."""
        digest = subject_hash(identity.subject)

        # The agent is matched before the human domain policy: a service account
        # lives on iam.gserviceaccount.com and could never satisfy
        # ALLOWED_EMAIL_DOMAINS, so checking the domain first would lock it out.
        if self._is_agent(identity.email):
            logger.info(
                "console auth ok; role=agent subject_hash=%s request_id=%s",
                digest[:12],
                request_id,
            )
            return AuthenticatedReviewer(
                identity=identity,
                role=ReviewerRole.AGENT,
                is_agent=True,
                local=local,
                _subject_hash=digest,
            )

        if not domain_allowed(identity.email, self._settings.ALLOWED_EMAIL_DOMAINS):
            logger.info(
                "console auth denied; reason=domain subject_hash=%s request_id=%s",
                digest[:12],
                request_id,
            )
            raise AuthorizationFailed("that identity is not permitted here")

        role = self._bindings.get(identity.email)
        if role is None:
            role = self._default_role()
        if role is None:
            logger.info(
                "console auth denied; reason=unbound subject_hash=%s request_id=%s",
                digest[:12],
                request_id,
            )
            raise AuthorizationFailed("that identity has no role in this console")

        logger.info(
            "console auth ok; role=%s subject_hash=%s request_id=%s",
            role.value,
            digest[:12],
            request_id,
        )
        return AuthenticatedReviewer(
            identity=identity, role=role, local=local, _subject_hash=digest
        )

    def _default_role(self) -> Optional[ReviewerRole]:
        """The opt-in fallback role, which production cannot enable.

        Both the flag and the role name are required: a deployment that set only
        one of them meant something it did not say.
        """
        settings = self._settings
        if (settings.ENVIRONMENT or "").strip() in STRICT_ENVIRONMENTS:
            return None
        if not settings.ALLOW_UNBOUND_VIEWERS or settings.DEFAULT_ROLE is None:
            return None
        try:
            role = ReviewerRole(str(settings.DEFAULT_ROLE).strip().lower())
        except ValueError:
            return None
        return None if role is ReviewerRole.AGENT else role


class _CaseInsensitive:
    """Case-insensitive read-only view over any header mapping."""

    __slots__ = ("_items",)

    def __init__(self, headers: Mapping[str, str]) -> None:
        self._items = {
            str(key).lower(): value
            for key, value in (
                headers.items() if hasattr(headers, "items") else headers or {}
            )
        }

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        value = self._items.get(name.lower(), default)
        return value if isinstance(value, str) or value is None else str(value)


__all__ = [
    "AGENT_AUDIENCE_SUFFIX",
    "IAP_ASSERTION_HEADER",
    "IAP_EMAIL_HEADER",
    "IAP_ISSUER",
    "IAP_PUBLIC_KEY_URL",
    "IAP_USER_ID_HEADER",
    "LOCAL_REVIEWER_HEADER",
    "LOOPBACK_HOSTS",
    "ROLE_LADDER",
    "AuthConfigurationError",
    "AuthenticatedReviewer",
    "AuthenticationFailed",
    "AuthorizationFailed",
    "AuthVerifierUnavailable",
    "ClaimsVerifier",
    "ReviewerAuthError",
    "ReviewerAuthenticator",
    "default_iap_claims_verifier",
    "domain_allowed",
    "domains_configured",
    "email_domain",
    "parse_role_bindings",
    "role_at_least",
    "subject_hash",
    "validate_agent_audience",
    "validate_console_auth_startup",
    "validate_console_origin",
]
