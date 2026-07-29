"""Isolated configuration for the /tickets administrative plane.

This module is deliberately independent of the main RAG configuration. It must
never import ``api.config``: that module builds its ``Settings()`` singleton at
import time and transitively pulls in the OpenAI, Gemini, and httpx client
stack, which the admin plane has no business initializing.

Three settings classes model three different service boundaries, and each one
refuses to start when it is handed a secret belonging to another:

* :class:`TicketConsoleSettings` — the browser-facing admin service. It gets
  the DevRev token, CSRF secret, and cursor key, and never a correlation key.
* :class:`EvidenceBrokerSettings` — the read-only ``(default)`` evidence
  broker. It gets the versioned lookup keyring, and never an ingress key.
* :class:`ProducerCorrelationSettings` — the RAG producer side. It gets the
  ingress-verification key and the single current lookup key, and never the
  broker's keyring.

Every field carries a default, so constructing any of these can never raise at
import time. Cross-field consistency lives in the ``validate_*`` functions,
mirroring the repository's existing ``validate_settings()`` idiom, and app
startup is expected to call them.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from collections.abc import Mapping
from typing import Optional
from urllib.parse import urlparse

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from api.ticket_review_models import (
    AUDIT_RETENTION_DAYS,
    CACHE_TTL_S,
    CSRF_TOKEN_TTL_S,
    CURSOR_AEAD_KEY_BYTES,
    DEFAULT_PAGE_SIZE,
    DEVREV_CONNECT_TIMEOUT_S,
    DEVREV_MAX_ENTRIES,
    DEVREV_MAX_PAGES,
    DEVREV_MAX_RESPONSE_BYTES,
    DEVREV_MAX_RETRIES,
    DEVREV_READ_TIMEOUT_S,
    EVIDENCE_BROKER_MAX_RESPONSE_BYTES,
    IDEMPOTENCY_TTL_S,
    IMPORT_STAGING_TTL_S,
    MAX_BATCH_REVIEWS,
    MAX_CSV_REQUEST_BYTES,
    MAX_CSV_ROWS,
    MAX_JSON_REQUEST_BYTES,
    MAX_PAGE_SIZE,
    MESSAGE_CACHE_TTL_S,
    REMEDIATION_HEARTBEAT_S,
    REMEDIATION_LEASE_S,
    REMEDIATION_MAX_CONTINUOUS_LEASE_S,
    REVIEW_RETENTION_DAYS,
)

DEVREV_OFFICIAL_API_BASE = "https://api.devrev.ai"
DEVREV_PINNED_VERSION = "2022-10-20"
DEFAULT_FIRESTORE_DATABASE = "(default)"
STRICT_ENVIRONMENTS = frozenset({"staging", "production"})
# Only the three environments the master plan's configuration matrix names. A
# fourth value would silently create a non-strict deployment path.
VALID_ENVIRONMENTS = frozenset({"local", "staging", "production"})
VALID_AUTH_MODES = frozenset({"iap", "local"})

# ENVIRONMENT is the switch that turns every production hardening check on, so
# its default must be the STRICTEST value, not the most convenient one. If
# Terraform ever fails to inject TICKETS_ENVIRONMENT, the revision then refuses
# to start instead of validating clean with no secrets and an empty database.
# Local development must opt out explicitly with TICKETS_ENVIRONMENT=local.
FAIL_CLOSED_ENVIRONMENT = "production"

# Environment variable names that belong to another service boundary. A secret
# from one plane appearing in another plane's revision is a deployment error,
# not something to silently ignore.
CORRELATION_INGRESS_KEY_ENV = "TICKETS_CORRELATION_INGRESS_KEY"
CORRELATION_LOOKUP_KEY_ENV = "TICKETS_CORRELATION_LOOKUP_KEY"
CORRELATION_LOOKUP_KEYRING_ENV = "TICKETS_CORRELATION_LOOKUP_KEYRING_JSON"
# This is the NAME of an environment variable, not a credential value. It is
# listed here so the broker and producer planes can refuse to start when a
# DevRev token is delivered to the wrong revision.
DEVREV_TOKEN_ENV = "TICKETS_DEVREV_TOKEN"  # noqa: S105

CSRF_SECRET_ENV = "TICKETS_CSRF_SIGNING_SECRET"  # noqa: S105 - env var NAME
CURSOR_KEY_ENV = "TICKETS_CURSOR_AEAD_KEY"  # noqa: S105 - env var NAME

# Each plane refuses every secret it does not own, in both directions.
CONSOLE_FORBIDDEN_ENV_VARS = frozenset(
    {
        CORRELATION_INGRESS_KEY_ENV,
        CORRELATION_LOOKUP_KEY_ENV,
        CORRELATION_LOOKUP_KEYRING_ENV,
    }
)
BROKER_FORBIDDEN_ENV_VARS = frozenset(
    {
        CORRELATION_INGRESS_KEY_ENV,
        CORRELATION_LOOKUP_KEY_ENV,
        DEVREV_TOKEN_ENV,
        CSRF_SECRET_ENV,
        CURSOR_KEY_ENV,
    }
)
PRODUCER_FORBIDDEN_ENV_VARS = frozenset(
    {
        CORRELATION_LOOKUP_KEYRING_ENV,
        DEVREV_TOKEN_ENV,
        CSRF_SECRET_ENV,
        CURSOR_KEY_ENV,
    }
)


def _secret_text(value: object) -> str:
    """Read a secret without ever returning it to a caller that logs it."""
    if isinstance(value, SecretStr):
        return value.get_secret_value().strip()
    if isinstance(value, str):
        return value.strip()
    return ""


def decode_cursor_aead_key(value: object) -> bytes:
    """Decode the base64 cursor key and prove it is exactly 32 bytes.

    Error messages never echo the supplied value.
    """
    text = _secret_text(value)
    invalid = f"CURSOR_AEAD_KEY must be base64 decoding to exactly {CURSOR_AEAD_KEY_BYTES} bytes"
    if not text:
        raise ValueError(invalid)
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(invalid) from exc
    if len(raw) != CURSOR_AEAD_KEY_BYTES:
        raise ValueError(invalid)
    return raw


def _is_https_url(value: str) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


def _forbidden_boundary_errors(
    env: Mapping[str, str], forbidden: frozenset[str], plane: str
) -> list[str]:
    return [
        f"{name} must not be present in the {plane} revision; it belongs to another "
        "service boundary"
        for name in sorted(forbidden)
        if env.get(name)
    ]


# =====================================================================
# Console settings
# =====================================================================


class TicketConsoleSettings(BaseSettings):
    """Configuration for the standalone `/tickets` admin service."""

    model_config = SettingsConfigDict(
        env_prefix="TICKETS_",
        env_file=".env",
        case_sensitive=True,
        extra="ignore",
    )

    # Rollout and environment. A brand-new admin plane ships dark, and an
    # unset ENVIRONMENT fails closed rather than skipping every hardening rule.
    ENABLED: bool = False
    ENVIRONMENT: str = FAIL_CLOSED_ENVIRONMENT

    # Authentication and authorization.
    AUTH_MODE: str = "iap"
    ALLOW_LOCAL_AUTH: bool = False
    ENABLE_SYNTHETIC_VERIFICATION: bool = False
    ALLOW_UNBOUND_VIEWERS: bool = False
    IAP_AUDIENCE: str = ""
    ALLOWED_EMAIL_DOMAINS: list[str] = Field(default_factory=list)
    # Role bindings map reviewer emails to privilege; the master plan delivers
    # them as a confidential Secret Manager version, so they must never appear
    # in a repr, a model_dump, or a log line.
    ROLE_BINDINGS_JSON: SecretStr = Field(default=SecretStr(""))
    DEFAULT_ROLE: Optional[str] = None
    CSRF_SIGNING_SECRET: SecretStr = Field(default=SecretStr(""))
    CSRF_TOKEN_TTL_S: int = CSRF_TOKEN_TTL_S

    # Google Cloud.
    GCP_PROJECT: str = ""
    # Needed to enforce the plan's rule that a production broker URL must live
    # in the configured project/region rather than an arbitrary host.
    GCP_REGION: str = ""
    FIRESTORE_DATABASE: str = ""
    CURSOR_AEAD_KEY: SecretStr = Field(default=SecretStr(""))
    EVIDENCE_BROKER_URL: str = ""
    EVIDENCE_BROKER_AUDIENCE: str = ""

    # DevRev, read-only in the MVP.
    DEVREV_API_BASE: str = DEVREV_OFFICIAL_API_BASE
    ALLOW_NON_OFFICIAL_DEVREV_BASE: bool = False
    DEVREV_TOKEN: SecretStr = Field(default=SecretStr(""))
    DEVREV_ORG_SLUG: str = ""
    DEVREV_TICKET_URL_TEMPLATE: Optional[str] = None
    DEVREV_AI_AUTHOR_IDS: list[str] = Field(default_factory=list)
    DEVREV_SYSTEM_AUTHOR_IDS: list[str] = Field(default_factory=list)
    DEVREV_HUMAN_AUTHOR_IDS: list[str] = Field(default_factory=list)
    DEVREV_ALLOWED_PART_DONS: list[str] = Field(default_factory=list)
    DEVREV_ALLOWED_TICKET_VISIBILITY_IDS: list[int] = Field(default_factory=list)
    DEVREV_ALLOWED_TIMELINE_VISIBILITIES: list[str] = Field(default_factory=list)
    DEVREV_VERSION: str = DEVREV_PINNED_VERSION
    DEVREV_CONNECT_TIMEOUT_S: float = DEVREV_CONNECT_TIMEOUT_S
    DEVREV_TIMEOUT_S: float = DEVREV_READ_TIMEOUT_S
    DEVREV_MAX_RETRIES: int = DEVREV_MAX_RETRIES
    DEVREV_MAX_RESPONSE_BYTES: int = DEVREV_MAX_RESPONSE_BYTES
    DEVREV_MAX_PAGES: int = DEVREV_MAX_PAGES
    DEVREV_PAGE_SIZE: int = DEFAULT_PAGE_SIZE

    # Caches, TTLs, and retention.
    CACHE_TTL_S: int = CACHE_TTL_S
    MESSAGE_CACHE_TTL_S: int = MESSAGE_CACHE_TTL_S
    IDEMPOTENCY_TTL_S: int = IDEMPOTENCY_TTL_S
    IMPORT_STAGING_TTL_S: int = IMPORT_STAGING_TTL_S
    REVIEW_RETENTION_DAYS: int = REVIEW_RETENTION_DAYS
    AUDIT_RETENTION_DAYS: int = AUDIT_RETENTION_DAYS
    RETENTION_JOB_ENABLED: bool = False

    # Request and payload bounds.
    MAX_TIMELINE_ENTRIES: int = DEVREV_MAX_ENTRIES
    MAX_CSV_BYTES: int = MAX_CSV_REQUEST_BYTES
    MAX_JSON_BYTES: int = MAX_JSON_REQUEST_BYTES
    MAX_CSV_ROWS: int = MAX_CSV_ROWS
    MAX_BATCH_REVIEWS: int = MAX_BATCH_REVIEWS

    # Remediation leases.
    REMEDIATION_LEASE_S: int = REMEDIATION_LEASE_S
    REMEDIATION_HEARTBEAT_S: int = REMEDIATION_HEARTBEAT_S
    REMEDIATION_MAX_CONTINUOUS_LEASE_S: int = REMEDIATION_MAX_CONTINUOUS_LEASE_S

    # Remediation agent identity and repository binding.
    REPO_ID: str = ""
    EXPECTED_BASE_REF: str = ""
    AGENT_SERVICE_ACCOUNT: str = ""
    AGENT_IAP_TARGET_AUDIENCE: str = ""


def validate_ticket_console_settings(
    settings: TicketConsoleSettings,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> bool:
    """Validate the console configuration, or raise ``ValueError``.

    Pure with respect to ``settings``; ``env`` defaults to ``os.environ`` and
    is injectable so tests never depend on the developer's shell. Every error
    is accumulated so a misconfigured revision reports all of its problems at
    once, and no message ever echoes a secret value.
    """
    environment = env if env is not None else os.environ
    errors: list[str] = []

    errors.extend(_forbidden_boundary_errors(environment, CONSOLE_FORBIDDEN_ENV_VARS, "console"))

    if settings.ENVIRONMENT not in VALID_ENVIRONMENTS:
        errors.append(
            f"ENVIRONMENT={settings.ENVIRONMENT!r} is invalid "
            f"(expected one of {sorted(VALID_ENVIRONMENTS)})"
        )
    if settings.AUTH_MODE not in VALID_AUTH_MODES:
        errors.append(
            f"AUTH_MODE={settings.AUTH_MODE!r} is invalid "
            f"(expected one of {sorted(VALID_AUTH_MODES)})"
        )
    if settings.AUTH_MODE == "local" and not settings.ALLOW_LOCAL_AUTH:
        errors.append("AUTH_MODE=local requires ALLOW_LOCAL_AUTH=true")
    if settings.DEVREV_VERSION != DEVREV_PINNED_VERSION:
        errors.append(f"DEVREV_VERSION must be pinned to {DEVREV_PINNED_VERSION}")

    strict = settings.ENVIRONMENT in STRICT_ENVIRONMENTS

    # A non-official DevRev origin is only ever acceptable behind an explicit
    # non-production override, and never in production at all.
    official = settings.DEVREV_API_BASE.rstrip("/") == DEVREV_OFFICIAL_API_BASE
    if not official and not settings.ALLOW_NON_OFFICIAL_DEVREV_BASE:
        errors.append(
            f"DEVREV_API_BASE must be {DEVREV_OFFICIAL_API_BASE} unless "
            "ALLOW_NON_OFFICIAL_DEVREV_BASE is explicitly enabled outside production"
        )
    # The canonical matrix gives local a "fixture server", which may legitimately
    # be plain http on loopback. Everywhere else, and without the explicit
    # override, https is mandatory.
    if not _is_https_url(settings.DEVREV_API_BASE) and (
        strict or not settings.ALLOW_NON_OFFICIAL_DEVREV_BASE
    ):
        errors.append("DEVREV_API_BASE must be an https URL")

    if not strict:
        if errors:
            raise ValueError(f"Invalid tickets console configuration: {'; '.join(errors)}")
        return True

    if settings.ENVIRONMENT == "production" and settings.ALLOW_NON_OFFICIAL_DEVREV_BASE:
        errors.append("ALLOW_NON_OFFICIAL_DEVREV_BASE must be false in production")
    if settings.ENVIRONMENT == "production" and settings.ENABLE_SYNTHETIC_VERIFICATION:
        errors.append("ENABLE_SYNTHETIC_VERIFICATION must be false in production")

    if settings.AUTH_MODE != "iap":
        errors.append(f"AUTH_MODE must be 'iap' in {settings.ENVIRONMENT}")
    if settings.ALLOW_LOCAL_AUTH:
        errors.append(f"ALLOW_LOCAL_AUTH must be false in {settings.ENVIRONMENT}")
    if settings.ALLOW_UNBOUND_VIEWERS:
        errors.append(f"ALLOW_UNBOUND_VIEWERS must be false in {settings.ENVIRONMENT}")
    if settings.DEFAULT_ROLE is not None:
        errors.append("DEFAULT_ROLE must be unset; an unbound identity is denied")
    if not settings.IAP_AUDIENCE.strip():
        errors.append("IAP_AUDIENCE is required")
    role_bindings = _secret_text(settings.ROLE_BINDINGS_JSON)
    if not role_bindings:
        errors.append("ROLE_BINDINGS_JSON is required")
    else:
        try:
            parsed_bindings = json.loads(role_bindings)
        except json.JSONDecodeError:
            errors.append("ROLE_BINDINGS_JSON must be valid JSON")
        else:
            if not isinstance(parsed_bindings, dict) or not parsed_bindings:
                errors.append("ROLE_BINDINGS_JSON must be a non-empty identity-to-role map")

    # A wildcard anywhere in a domain is a wildcard, and a domain with no dot
    # is a bare TLD that would match far more than intended.
    domains = [d.strip().lower() for d in settings.ALLOWED_EMAIL_DOMAINS]
    if not domains or any(not d or "*" in d or "." not in d for d in domains):
        errors.append(
            "ALLOWED_EMAIL_DOMAINS must be a non-empty list of explicit, "
            "wildcard-free, dotted domains"
        )

    if not _secret_text(settings.CSRF_SIGNING_SECRET):
        errors.append("CSRF_SIGNING_SECRET is required")
    try:
        decode_cursor_aead_key(settings.CURSOR_AEAD_KEY)
    except ValueError as exc:
        errors.append(str(exc))
    if not _secret_text(settings.DEVREV_TOKEN):
        errors.append("DEVREV_TOKEN is required")

    if not settings.GCP_PROJECT.strip():
        errors.append("GCP_PROJECT is required")
    database = settings.FIRESTORE_DATABASE.strip()
    if not database:
        errors.append("FIRESTORE_DATABASE is required")
    elif database == DEFAULT_FIRESTORE_DATABASE:
        errors.append(
            "FIRESTORE_DATABASE must be a dedicated named database, never "
            f"{DEFAULT_FIRESTORE_DATABASE}"
        )

    if not settings.GCP_REGION.strip():
        errors.append("GCP_REGION is required")

    broker_url = settings.EVIDENCE_BROKER_URL.strip()
    if not broker_url:
        errors.append("EVIDENCE_BROKER_URL is required")
    elif not _is_https_url(broker_url):
        errors.append("EVIDENCE_BROKER_URL must be an https URL")
    elif settings.GCP_REGION.strip():
        # The broker is a Cloud Run service in the configured region; refuse to
        # let the console be pointed at an arbitrary external host.
        host = urlparse(broker_url).netloc.lower()
        region = settings.GCP_REGION.strip().lower()
        if not host.endswith(".run.app") or region not in host:
            errors.append(
                "EVIDENCE_BROKER_URL must be the Cloud Run host for the configured "
                "project and region"
            )
    if not settings.EVIDENCE_BROKER_AUDIENCE.strip():
        errors.append("EVIDENCE_BROKER_AUDIENCE is required")

    if not settings.DEVREV_ALLOWED_PART_DONS:
        errors.append("DEVREV_ALLOWED_PART_DONS must be a non-empty allowlist")
    if not settings.DEVREV_ALLOWED_TICKET_VISIBILITY_IDS:
        errors.append("DEVREV_ALLOWED_TICKET_VISIBILITY_IDS must be a non-empty allowlist")
    if not settings.DEVREV_ALLOWED_TIMELINE_VISIBILITIES:
        errors.append("DEVREV_ALLOWED_TIMELINE_VISIBILITIES must be a non-empty allowlist")

    if settings.DEVREV_TICKET_URL_TEMPLATE and not settings.DEVREV_TICKET_URL_TEMPLATE.startswith(
        "https://"
    ):
        errors.append("DEVREV_TICKET_URL_TEMPLATE must be an https template when configured")

    errors.extend(_canonical_bound_errors(settings))

    if errors:
        raise ValueError(f"Invalid tickets console configuration: {'; '.join(errors)}")
    return True


def _canonical_bound_errors(settings: TicketConsoleSettings) -> list[str]:
    """Refuse a staging/production override that loosens a canonical limit.

    Every value below is the single source of truth for models, API validation,
    UI counters, and Terraform. Without this check a revision could raise the
    DevRev response cap, stretch a lease past the agreed window, or extend
    retention simply by setting an environment variable.
    """
    errors: list[str] = []
    # (label, actual, canonical maximum)
    not_above = (
        ("DEVREV_PAGE_SIZE", settings.DEVREV_PAGE_SIZE, MAX_PAGE_SIZE),
        ("DEVREV_MAX_PAGES", settings.DEVREV_MAX_PAGES, DEVREV_MAX_PAGES),
        ("DEVREV_MAX_RETRIES", settings.DEVREV_MAX_RETRIES, DEVREV_MAX_RETRIES),
        (
            "DEVREV_MAX_RESPONSE_BYTES",
            settings.DEVREV_MAX_RESPONSE_BYTES,
            DEVREV_MAX_RESPONSE_BYTES,
        ),
        ("MAX_TIMELINE_ENTRIES", settings.MAX_TIMELINE_ENTRIES, DEVREV_MAX_ENTRIES),
        ("MAX_CSV_BYTES", settings.MAX_CSV_BYTES, MAX_CSV_REQUEST_BYTES),
        ("MAX_JSON_BYTES", settings.MAX_JSON_BYTES, MAX_JSON_REQUEST_BYTES),
        ("MAX_CSV_ROWS", settings.MAX_CSV_ROWS, MAX_CSV_ROWS),
        ("MAX_BATCH_REVIEWS", settings.MAX_BATCH_REVIEWS, MAX_BATCH_REVIEWS),
        ("CACHE_TTL_S", settings.CACHE_TTL_S, CACHE_TTL_S),
        ("MESSAGE_CACHE_TTL_S", settings.MESSAGE_CACHE_TTL_S, MESSAGE_CACHE_TTL_S),
        ("IDEMPOTENCY_TTL_S", settings.IDEMPOTENCY_TTL_S, IDEMPOTENCY_TTL_S),
        ("IMPORT_STAGING_TTL_S", settings.IMPORT_STAGING_TTL_S, IMPORT_STAGING_TTL_S),
        ("CSRF_TOKEN_TTL_S", settings.CSRF_TOKEN_TTL_S, CSRF_TOKEN_TTL_S),
        ("DEVREV_TIMEOUT_S", settings.DEVREV_TIMEOUT_S, DEVREV_READ_TIMEOUT_S),
        ("DEVREV_CONNECT_TIMEOUT_S", settings.DEVREV_CONNECT_TIMEOUT_S, DEVREV_CONNECT_TIMEOUT_S),
        ("REMEDIATION_LEASE_S", settings.REMEDIATION_LEASE_S, REMEDIATION_LEASE_S),
        ("REMEDIATION_HEARTBEAT_S", settings.REMEDIATION_HEARTBEAT_S, REMEDIATION_HEARTBEAT_S),
        (
            "REMEDIATION_MAX_CONTINUOUS_LEASE_S",
            settings.REMEDIATION_MAX_CONTINUOUS_LEASE_S,
            REMEDIATION_MAX_CONTINUOUS_LEASE_S,
        ),
        ("REVIEW_RETENTION_DAYS", settings.REVIEW_RETENTION_DAYS, REVIEW_RETENTION_DAYS),
        ("AUDIT_RETENTION_DAYS", settings.AUDIT_RETENTION_DAYS, AUDIT_RETENTION_DAYS),
    )
    for label, actual, ceiling in not_above:
        if actual > ceiling:
            errors.append(f"{label} must not exceed the canonical {ceiling}")
        if actual <= 0:
            errors.append(f"{label} must be positive")
    if settings.REMEDIATION_HEARTBEAT_S >= settings.REMEDIATION_LEASE_S:
        errors.append("REMEDIATION_HEARTBEAT_S must be shorter than REMEDIATION_LEASE_S")
    return errors


# =====================================================================
# Evidence broker settings
# =====================================================================


class EvidenceBrokerSettings(BaseSettings):
    """Configuration for the read-only ``(default)`` evidence broker.

    The broker is the only component permitted to read the production
    ``(default)`` database, and only through one bounded, allowlisted lookup.
    It holds the versioned lookup keyring and must never receive the ingress
    key or a DevRev token.
    """

    model_config = SettingsConfigDict(
        env_prefix="TICKETS_BROKER_",
        env_file=".env",
        case_sensitive=True,
        extra="ignore",
        populate_by_name=True,
    )

    # Fails closed for the same reason the console does: a broker revision
    # missing TICKETS_BROKER_ENVIRONMENT must refuse to start, not validate
    # clean with an empty keyring, audience, and caller allowlist.
    ENVIRONMENT: str = FAIL_CLOSED_ENVIRONMENT
    FIRESTORE_DATABASE: str = ""
    CONSOLE_SERVICE_ACCOUNT: str = ""
    AUDIENCE: str = ""
    MAX_RESPONSE_BYTES: int = EVIDENCE_BROKER_MAX_RESPONSE_BYTES
    MAX_RESULTS: int = 25
    CORRELATION_LOOKUP_KEYRING_JSON: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=CORRELATION_LOOKUP_KEYRING_ENV,
    )
    CORRELATION_ALLOWED_KEY_VERSIONS: list[int] = Field(
        default_factory=list,
        validation_alias="TICKETS_CORRELATION_ALLOWED_KEY_VERSIONS",
    )


def validate_evidence_broker_settings(
    settings: EvidenceBrokerSettings,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> bool:
    """Validate the broker configuration, or raise ``ValueError``."""
    environment = env if env is not None else os.environ
    errors: list[str] = _forbidden_boundary_errors(
        environment, BROKER_FORBIDDEN_ENV_VARS, "evidence broker"
    )

    strict = settings.ENVIRONMENT in STRICT_ENVIRONMENTS
    if strict:
        if not settings.FIRESTORE_DATABASE.strip():
            errors.append("FIRESTORE_DATABASE is required")
        if not settings.CONSOLE_SERVICE_ACCOUNT.strip():
            errors.append("CONSOLE_SERVICE_ACCOUNT is required")
        if not settings.AUDIENCE.strip():
            errors.append("AUDIENCE is required")
        keyring_text = _secret_text(settings.CORRELATION_LOOKUP_KEYRING_JSON)
        if not keyring_text:
            errors.append(f"{CORRELATION_LOOKUP_KEYRING_ENV} is required")
        else:
            try:
                keyring = json.loads(keyring_text)
            except json.JSONDecodeError:
                errors.append(f"{CORRELATION_LOOKUP_KEYRING_ENV} must be valid JSON")
            else:
                if not isinstance(keyring, dict) or not keyring:
                    errors.append(
                        f"{CORRELATION_LOOKUP_KEYRING_ENV} must be a non-empty version map"
                    )
        if not settings.CORRELATION_ALLOWED_KEY_VERSIONS:
            errors.append("CORRELATION_ALLOWED_KEY_VERSIONS must be a non-empty allowlist")

    if settings.MAX_RESPONSE_BYTES > EVIDENCE_BROKER_MAX_RESPONSE_BYTES:
        errors.append(
            f"MAX_RESPONSE_BYTES must not exceed {EVIDENCE_BROKER_MAX_RESPONSE_BYTES}"
        )

    if errors:
        raise ValueError(f"Invalid evidence broker configuration: {'; '.join(errors)}")
    return True


# =====================================================================
# Producer correlation settings
# =====================================================================


class ProducerCorrelationSettings(BaseSettings):
    """Correlation keys held by the RAG producer side only.

    The producer verifies the n8n ingress signature with one key and derives
    the storage/query HMAC with a *different* current lookup key. It never
    receives the broker's multi-version keyring.
    """

    model_config = SettingsConfigDict(
        env_prefix="TICKETS_PRODUCER_",
        env_file=".env",
        case_sensitive=True,
        extra="ignore",
        populate_by_name=True,
    )

    CORRELATION_INGRESS_KEY: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=CORRELATION_INGRESS_KEY_ENV,
    )
    CORRELATION_INGRESS_KEY_VERSION: Optional[int] = Field(
        default=None,
        validation_alias="TICKETS_CORRELATION_INGRESS_KEY_VERSION",
    )
    CORRELATION_LOOKUP_KEY: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=CORRELATION_LOOKUP_KEY_ENV,
    )
    CORRELATION_LOOKUP_KEY_VERSION: Optional[int] = Field(
        default=None,
        validation_alias="TICKETS_CORRELATION_LOOKUP_KEY_VERSION",
    )


def validate_producer_correlation_settings(
    settings: ProducerCorrelationSettings,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> bool:
    """Validate the producer correlation configuration, or raise ``ValueError``."""
    environment = env if env is not None else os.environ
    errors: list[str] = _forbidden_boundary_errors(
        environment, PRODUCER_FORBIDDEN_ENV_VARS, "producer"
    )

    ingress = _secret_text(settings.CORRELATION_INGRESS_KEY)
    lookup = _secret_text(settings.CORRELATION_LOOKUP_KEY)
    if not ingress:
        errors.append(f"{CORRELATION_INGRESS_KEY_ENV} is required")
    if not lookup:
        errors.append(f"{CORRELATION_LOOKUP_KEY_ENV} is required")
    if ingress and lookup and ingress == lookup:
        errors.append("the ingress and lookup keys must differ")
    if settings.CORRELATION_INGRESS_KEY_VERSION is None:
        errors.append("CORRELATION_INGRESS_KEY_VERSION is required")
    if settings.CORRELATION_LOOKUP_KEY_VERSION is None:
        errors.append("CORRELATION_LOOKUP_KEY_VERSION is required")

    if errors:
        raise ValueError(f"Invalid producer correlation configuration: {'; '.join(errors)}")
    return True
