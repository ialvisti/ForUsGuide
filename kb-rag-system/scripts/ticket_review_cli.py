"""Repository-side CLI for the ticket-review remediation handoff.

What this is
------------
The one program a coding agent uses to enumerate low-rated reviews, claim a
remediation batch, read the frozen observations, record what it did, and hand
the work back to a human. It speaks only to the console's admin API over IAP.

What this deliberately is not
-----------------------------
*   **It is not a Firestore client.** There is no code path here that imports
    ``google.cloud.firestore``, and no command accepts a project, database,
    collection, or document path. Every business rule — versions, leases,
    transitions, the audit chain — lives behind the API, so a CLI that could
    reach the database directly would be a way around all of them.
*   **It is not an approver.** Writes are dry-run by default. ``--apply`` is
    required to send one, and the destination, mode, and batch are printed
    first.
*   **It cannot finish the job.** ``submit`` moves a batch to
    ``changes_proposed`` and gives up the lease. There is no command that
    verifies or completes a batch, because a human does that.

Authentication
--------------
In a deployed environment the caller's own ADC is used for exactly one thing:
calling IAM Credentials ``signJwt`` to sign a short-lived assertion for the
configured remediation-agent service account. No key file is downloaded and none
exists. The signed JWT's audience is the validated console origin plus ``/*`` —
never the app's IAP signed-header resource audience, and never a bare base URL.

Against a loopback fixture console, and only there, the CLI uses the fixture's
explicit local-agent hook instead. That path refuses any non-loopback URL.

The lease
---------
A claim yields an opaque lease token, once, in a response header. It is written
to ``<git-dir>/codex-ticket-leases/<batch-id>.json`` with mode ``0600``, never
printed, and removed on release or submission. The directory is resolved by
asking Git, because the mandatory linked worktree makes ``.git`` a file rather
than a directory, and it is never taken from the caller.

Exit codes
----------
``0`` success, ``2`` validation, ``3`` auth, ``4`` not found, ``5``
version/business conflict, ``6`` lease lost, ``7`` upstream failure, ``8`` unsafe
environment.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol, Sequence, cast
from urllib.parse import urlencode, urlsplit

import httpx

# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_VALIDATION = 2
EXIT_AUTH = 3
EXIT_NOT_FOUND = 4
EXIT_CONFLICT = 5
EXIT_LEASE_LOST = 6
EXIT_UPSTREAM = 7
EXIT_UNSAFE = 8

# ---------------------------------------------------------------------------
# Constants that must agree with the server. Duplicated rather than imported so
# the CLI does not pull FastAPI, pydantic, and the whole console into a developer
# shell; ``tests/test_ticket_review_cli.py`` asserts they still agree.
# ---------------------------------------------------------------------------

API_PREFIX = "/api/admin/v1"
BATCHES_PATH = "/remediation-batches"
REVIEWS_PATH = "/reviews"
LEASE_TOKEN_HEADER = "X-Tickets-Lease-Token"
IDEMPOTENCY_HEADER = "Idempotency-Key"
LOCAL_REVIEWER_HEADER = "X-Tickets-Local-Reviewer"
CURSOR_HEADER = "X-Tickets-Cursor"
AGENT_AUDIENCE_SUFFIX = "/*"

REMEDIATION_LEASE_S = 15 * 60
REMEDIATION_HEARTBEAT_S = 5 * 60
REMEDIATION_MAX_CONTINUOUS_LEASE_S = 2 * 60 * 60

VALID_ENVIRONMENTS = ("local", "staging", "production")
REVIEW_STATUSES = (
    "unreviewed",
    "reviewed",
    "triaged",
    "planned",
    "in_progress",
    "changes_proposed",
    "verifying",
    "resolved",
    "blocked",
    "wont_fix",
)
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

#: The directory Git is asked for. Never joined from caller input.
LEASE_DIRECTORY_NAME = "codex-ticket-leases"

BATCH_ID_PATTERN = re.compile(
    r"^(?:[0-9a-f]{32}"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\Z"
)
REVIEW_ID_PATTERN = re.compile(r"^[0-9a-f]{64}\Z")
COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}\Z")
GIT_REF_PATTERN = re.compile(r"^(?!/)(?!.*//)(?!.*\.\.)[A-Za-z0-9._/-]+(?<!/)(?<!\.)\Z")
REPO_PATH_PATTERN = re.compile(r"^(?!/)(?!.*//)(?!.*\.\.)[A-Za-z0-9._/-]+(?<!/)\Z")
IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z")

HTTP_CONNECT_TIMEOUT_S = 5.0
HTTP_READ_TIMEOUT_S = 30.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_REVIEW_CURSOR_LENGTH = 2_048
MAX_REVIEW_QUERY_PAGES = 1_000

IAM_CREDENTIALS_HOST = "iamcredentials.googleapis.com"
JWT_LIFETIME_S = 300

AGENT_SERVICE_ACCOUNT_ENV = "TICKETS_AGENT_SERVICE_ACCOUNT"

#: Keys whose *value* is a credential. Redacted wherever they appear as
#: ``"key": "value"`` or ``key=value``/``key: value``.
_REDACT_KEYS = ("lease_token", "assertion", "signedJwt", "token")

#: Keys that introduce a credential and everything after them on the line.
#: ``Authorization: Bearer ey...`` is two words before the secret starts, so a
#: rule that consumed one token would leave the interesting part behind — which is
#: exactly what a value-shaped rule does here.
_REDACT_TO_END_OF_LINE = ("authorization", "proxy-authorization", "bearer")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CliError(Exception):
    """A failure already resolved to an exit code and a safe message."""

    exit_code = EXIT_UPSTREAM

    def __init__(self, message: str, *, hint: Optional[str] = None) -> None:
        super().__init__(message)
        self.hint = hint


class ValidationError(CliError):
    exit_code = EXIT_VALIDATION


class AuthError(CliError):
    exit_code = EXIT_AUTH


class NotFoundError(CliError):
    exit_code = EXIT_NOT_FOUND


class ConflictError(CliError):
    exit_code = EXIT_CONFLICT


class LeaseLostError(CliError):
    exit_code = EXIT_LEASE_LOST


class UpstreamError(CliError):
    exit_code = EXIT_UPSTREAM


class UnsafeEnvironmentError(CliError):
    exit_code = EXIT_UNSAFE


def redact(text: str) -> str:
    """Strip anything that looks like a credential out of ``text``.

    Deliberately blunt. A precise redactor that understood every shape a token
    can take would be the thing that eventually misses one; collapsing any
    ``key=value``/``"key": "value"`` whose key smells like a credential is
    coarse, cheap, and hard to get wrong.
    """
    result = text
    for key in _REDACT_KEYS:
        result = re.sub(
            rf'("{key}"\s*:\s*)"[^"]*"',
            r'\1"[redacted]"',
            result,
            flags=re.IGNORECASE,
        )
        result = re.sub(
            rf"\b({key})[=:\s]+\S+",
            r"\1=[redacted]",
            result,
            flags=re.IGNORECASE,
        )
    for key in _REDACT_TO_END_OF_LINE:
        result = re.sub(
            rf"\b({key})\b.*",
            r"\1 [redacted]",
            result,
            flags=re.IGNORECASE,
        )
    return result


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validated_environment(value: str) -> str:
    candidate = (value or "").strip().lower()
    if candidate not in VALID_ENVIRONMENTS:
        raise ValidationError(
            f"--environment must be one of {', '.join(VALID_ENVIRONMENTS)}"
        )
    return candidate


def is_loopback(url: str) -> bool:
    host = (urlsplit(url).hostname or "").strip().lower()
    return host in LOOPBACK_HOSTS


def validated_console_url(value: str, *, environment: str) -> str:
    """Accept an exact console origin, and nothing that could be redirected.

    A path, query, fragment, or embedded credential is refused rather than
    normalized away: this value becomes the JWT audience, and an audience that
    does not match what the operator thinks they typed is the whole attack.
    """
    raw = (value or "").strip()
    if not raw:
        raise ValidationError("--console-url is required")
    parsed = urlsplit(raw.rstrip("/"))
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("--console-url has an invalid port") from exc
    host = (parsed.hostname or "").strip().lower()
    if not host or parsed.username or parsed.password:
        raise ValidationError("--console-url must be a bare scheme://host[:port]")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValidationError("--console-url must carry no path, query, or fragment")
    if parsed.scheme == "http":
        if host not in LOOPBACK_HOSTS:
            raise UnsafeEnvironmentError(
                "plain http is only allowed for a loopback fixture console"
            )
        if environment != "local":
            raise UnsafeEnvironmentError(
                f"a loopback console is not valid for --environment {environment}"
            )
    elif parsed.scheme != "https":
        raise ValidationError("--console-url must be http (loopback) or https")
    if parsed.scheme == "https" and host in LOOPBACK_HOSTS:
        raise ValidationError("a loopback host cannot serve the deployed console")
    netloc = host if port is None else f"{host}:{port}"
    return f"{parsed.scheme}://{netloc}"


def validated_batch_id(value: str) -> str:
    candidate = (value or "").strip()
    if not BATCH_ID_PATTERN.match(candidate):
        raise ValidationError("--batch-id is not a server-minted batch id")
    return candidate


def validated_review_id(value: str) -> str:
    candidate = (value or "").strip()
    if not REVIEW_ID_PATTERN.match(candidate):
        raise ValidationError(f"{candidate[:16]!r} is not a review id")
    return candidate


def validated_ref(value: str, *, label: str) -> str:
    candidate = (value or "").strip()
    if not candidate or len(candidate) > 255 or not GIT_REF_PATTERN.match(candidate):
        raise ValidationError(f"{label} is not a well-formed git ref")
    return candidate


def validated_repo_path(value: str) -> str:
    candidate = (value or "").strip()
    if not candidate or len(candidate) > 512 or not REPO_PATH_PATTERN.match(candidate):
        raise ValidationError(f"{value!r} is not a repository-relative path")
    return candidate


def validated_commit_sha(value: str) -> str:
    candidate = (value or "").strip().lower()
    if not COMMIT_SHA_PATTERN.match(candidate):
        raise ValidationError("--commit-sha must be a full 40-character object name")
    return candidate


def new_idempotency_key(prefix: str) -> str:
    """A fresh key in the server's accepted alphabet.

    The server's pattern allows ``[A-Za-z0-9._:-]`` after a leading alphanumeric
    and caps the whole thing at 128 characters, so the prefix is truncated rather
    than trusted to be short.
    """
    key = f"{prefix[:24]}-{uuid.uuid4().hex}"
    if not IDEMPOTENCY_PATTERN.match(key):  # pragma: no cover - defensive
        raise ValidationError("could not build a valid idempotency key")
    return key


# ---------------------------------------------------------------------------
# Git-resolved lease storage
# ---------------------------------------------------------------------------

GitRunner = Callable[[Sequence[str]], str]


def _run_git(args: Sequence[str]) -> str:
    """Run one ``git`` command with no shell and a bounded timeout."""
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ValidationError("git is not available on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValidationError("git did not respond") from exc
    if completed.returncode != 0:
        raise ValidationError(
            "git refused: " + redact((completed.stderr or "").strip()[:200])
        )
    return (completed.stdout or "").strip()


@dataclass(frozen=True)
class LeaseStore:
    """Where a lease token and its keeper's bookkeeping live.

    Both paths come from Git, are canonicalized independently, and are then
    proven to sit inside the resolved git directory. A caller-supplied lease path
    is never accepted: it would be a way to make this program write a 0600 file
    anywhere the developer can write, with content the developer cannot see.
    """

    git_dir: Path
    lease_dir: Path

    @classmethod
    def resolve(cls, *, runner: GitRunner = _run_git) -> "LeaseStore":
        git_dir = Path(
            runner(["rev-parse", "--path-format=absolute", "--git-dir"])
        ).resolve()
        lease_dir = Path(
            runner(
                [
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-path",
                    LEASE_DIRECTORY_NAME,
                ]
            )
        ).resolve()
        if not git_dir.is_dir():
            raise ValidationError("git did not report a usable git directory")
        try:
            lease_dir.relative_to(git_dir)
        except ValueError as exc:
            raise UnsafeEnvironmentError(
                "the resolved lease directory is outside the git directory"
            ) from exc
        if lease_dir == git_dir:
            raise UnsafeEnvironmentError("the lease directory must be its own directory")
        return cls(git_dir=git_dir, lease_dir=lease_dir)

    def ensure(self) -> Path:
        self.lease_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        # A pre-existing directory keeps its mode, so tighten it explicitly.
        os.chmod(self.lease_dir, 0o700)
        return self.lease_dir

    def token_path(self, batch_id: str) -> Path:
        return self.ensure() / f"{validated_batch_id(batch_id)}.json"

    def keeper_path(self, batch_id: str) -> Path:
        return self.ensure() / f"{validated_batch_id(batch_id)}.keeper.json"

    def _write_atomic(self, path: Path, payload: Mapping[str, Any]) -> None:
        """Write 0600 without ever leaving a readable window.

        The descriptor is opened ``O_EXCL`` with the final mode, so there is no
        moment where the file exists with the process umask applied, and the
        rename onto the target is atomic within the directory.
        """
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(dict(payload), handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        os.replace(temporary, path)
        os.chmod(path, 0o600)

    def _read(self, path: Path) -> Optional[dict[str, Any]]:
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            return None
        if mode & 0o077:
            raise UnsafeEnvironmentError(
                f"{path.name} is group- or world-readable; remove it and re-claim"
            )
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValidationError(f"{path.name} is not readable state") from exc

    def save_token(self, batch_id: str, token: str, *, expires_at: str) -> None:
        self._write_atomic(
            self.token_path(batch_id),
            {"batch_id": batch_id, "lease_token": token, "lease_expires_at": expires_at},
        )

    def load_token(self, batch_id: str) -> Optional[str]:
        payload = self._read(self.token_path(batch_id))
        if payload is None:
            return None
        token = payload.get("lease_token")
        return token if isinstance(token, str) and token else None

    def require_token(self, batch_id: str) -> str:
        token = self.load_token(batch_id)
        if token is None:
            raise LeaseLostError(
                "no lease is stored for this batch",
                hint="run `batch claim --apply` first",
            )
        return token

    def save_keeper(self, batch_id: str, payload: Mapping[str, Any]) -> None:
        self._write_atomic(self.keeper_path(batch_id), payload)

    def load_keeper(self, batch_id: str) -> Optional[dict[str, Any]]:
        return self._read(self.keeper_path(batch_id))

    def forget(self, batch_id: str) -> list[str]:
        """Remove only this batch's two exact files, and report what went."""
        removed: list[str] = []
        for path in (self.token_path(batch_id), self.keeper_path(batch_id)):
            if path.exists():
                path.unlink()
                removed.append(path.name)
        return removed


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class TokenSigner(Protocol):
    """Produces the ``Authorization`` value for one request."""

    def authorization(self) -> str: ...

    def describe(self) -> dict[str, Any]: ...


@dataclass
class LocalFixtureSigner:
    """The fixture console's explicit local-agent hook. Loopback only.

    Two independent conditions, both checked here rather than only at the call
    site: the environment must be ``local`` and the console must be a loopback
    address. The fixture app additionally refuses to start outside fixture mode,
    so there are three layers between this and a deployed console.
    """

    console_url: str
    environment: str
    agent_email: str

    def __post_init__(self) -> None:
        if self.environment != "local":
            raise UnsafeEnvironmentError(
                "fixture authentication is only available for --environment local"
            )
        if not is_loopback(self.console_url):
            raise UnsafeEnvironmentError(
                "fixture authentication cannot be used against a non-loopback console"
            )

    def authorization(self) -> str:
        return ""

    def headers(self) -> dict[str, str]:
        return {LOCAL_REVIEWER_HEADER: self.agent_email}

    def describe(self) -> dict[str, Any]:
        return {
            "mode": "local-fixture",
            "console_url": self.console_url,
            "agent": self.agent_email,
        }


@dataclass
class IamCredentialsSigner:
    """Keyless IAP authentication via IAM Credentials ``signJwt``.

    The caller's ADC is used for exactly one thing: authorizing a ``signJwt``
    call on the *one* configured service account. That is why the deployment
    grants Token Creator on the service account itself and never project-wide —
    a project-wide grant would let this same code sign for every identity in the
    project.

    No key material is downloaded, cached, or written to disk, and the signed
    assertion is never printed.
    """

    console_url: str
    environment: str
    service_account: str
    transport: Optional[httpx.BaseTransport] = None
    credentials_factory: Optional[Callable[[], tuple[Any, Optional[str]]]] = None
    clock: Callable[[], float] = time.time
    _cached: Optional[tuple[float, str]] = field(default=None, repr=False)

    @property
    def audience(self) -> str:
        """The console origin plus ``/*``, and nothing else.

        Never the app's IAP signed-header resource audience: that value
        identifies the *backend* to IAP, and reusing it here would make an
        assertion minted for this CLI acceptable somewhere it was not intended.
        """
        return f"{self.console_url}{AGENT_AUDIENCE_SUFFIX}"

    def _adc_token(self) -> str:
        factory = self.credentials_factory
        if factory is None:  # pragma: no cover - requires real ADC
            def factory() -> tuple[Any, Optional[str]]:
                import google.auth
                from google.auth.transport.requests import Request

                credentials, project = google.auth.default(
                    scopes=["https://www.googleapis.com/auth/cloud-platform"]
                )
                credentials.refresh(Request())
                return credentials, project

        credentials, _ = factory()
        token = getattr(credentials, "token", None)
        if not isinstance(token, str) or not token:
            raise AuthError(
                "application default credentials produced no access token",
                hint="run `gcloud auth application-default login`",
            )
        return token

    def _sign(self, claims: Mapping[str, Any]) -> str:
        url = (
            f"https://{IAM_CREDENTIALS_HOST}/v1/projects/-/serviceAccounts/"
            f"{self.service_account}:signJwt"
        )
        body = {"payload": json.dumps(dict(claims), sort_keys=True)}
        with httpx.Client(
            timeout=httpx.Timeout(HTTP_READ_TIMEOUT_S, connect=HTTP_CONNECT_TIMEOUT_S),
            follow_redirects=False,
            transport=self.transport,
        ) as client:
            response = client.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {self._adc_token()}"},
            )
        if response.status_code in {401, 403}:
            raise AuthError(
                "signJwt refused this caller",
                hint=(
                    "grant roles/iam.serviceAccountTokenCreator on "
                    f"{self.service_account} to your own identity"
                ),
            )
        if response.status_code != 200:
            raise UpstreamError(
                f"signJwt returned {response.status_code}",
            )
        signed = response.json().get("signedJwt")
        if not isinstance(signed, str) or not signed:
            raise UpstreamError("signJwt returned no assertion")
        return signed

    def claims(self, *, now: Optional[float] = None) -> dict[str, Any]:
        issued = int(now if now is not None else self.clock())
        return {
            "iss": self.service_account,
            "sub": self.service_account,
            "aud": self.audience,
            "iat": issued,
            "exp": issued + JWT_LIFETIME_S,
        }

    def authorization(self) -> str:
        now = self.clock()
        if self._cached is not None and self._cached[0] > now + 30:
            return f"Bearer {self._cached[1]}"
        assertion = self._sign(self.claims(now=now))
        self._cached = (now + JWT_LIFETIME_S, assertion)
        return f"Bearer {assertion}"

    def headers(self) -> dict[str, str]:
        return {"Authorization": self.authorization()}

    def describe(self) -> dict[str, Any]:
        """Everything an operator needs, and no token.

        The assertion is deliberately absent even in the diagnostic output: an
        ``auth doctor`` that printed one would be the most convenient place in
        the whole system from which to copy a live credential.
        """
        return {
            "mode": "iam-credentials-signjwt",
            "service_account": self.service_account,
            "audience": self.audience,
            "console_url": self.console_url,
        }


def build_signer(
    *,
    console_url: str,
    environment: str,
    service_account: Optional[str] = None,
    transport: Optional[httpx.BaseTransport] = None,
    credentials_factory: Optional[Callable[[], tuple[Any, Optional[str]]]] = None,
) -> TokenSigner:
    """Pick the only signer this (environment, URL) pair permits."""
    if environment == "local" and is_loopback(console_url):
        return LocalFixtureSigner(
            console_url=console_url,
            environment=environment,
            agent_email=service_account
            or os.environ.get(AGENT_SERVICE_ACCOUNT_ENV, "")
            or "fixture-agent@example.invalid",
        )
    account = (service_account or os.environ.get(AGENT_SERVICE_ACCOUNT_ENV, "")).strip()
    if not account:
        raise ValidationError(
            f"{AGENT_SERVICE_ACCOUNT_ENV} is required outside a loopback fixture"
        )
    if not account.endswith(".iam.gserviceaccount.com"):
        raise ValidationError(
            f"{AGENT_SERVICE_ACCOUNT_ENV} must name a service account"
        )
    return IamCredentialsSigner(
        console_url=console_url,
        environment=environment,
        service_account=account,
        transport=transport,
        credentials_factory=credentials_factory,
    )


# ---------------------------------------------------------------------------
# The console client
# ---------------------------------------------------------------------------


@dataclass
class ConsoleResponse:
    status: int
    body: Any
    headers: Mapping[str, str]


class ConsoleClient:
    """Every request this CLI makes, and every failure it can turn into.

    Redirects are refused rather than followed: a redirect from the console
    origin is either a misconfiguration or an attempt to move an
    ``Authorization`` header somewhere else, and neither is worth silently
    tolerating in a program that carries a lease token.
    """

    def __init__(
        self,
        *,
        console_url: str,
        signer: TokenSigner,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.console_url = console_url
        self.signer = signer
        self._transport = transport

    def _headers(self, *, idempotency: Optional[str], cursor: Optional[str]) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        extra = getattr(self.signer, "headers", None)
        if callable(extra):
            headers.update(extra())
        if idempotency is not None:
            headers[IDEMPOTENCY_HEADER] = idempotency
            headers["Content-Type"] = "application/json"
        if cursor is not None:
            headers[CURSOR_HEADER] = cursor
        return {name: value for name, value in headers.items() if value}

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        idempotency: Optional[str] = None,
        cursor: Optional[str] = None,
        expect_text: bool = False,
    ) -> ConsoleResponse:
        url = f"{self.console_url}{path}"
        try:
            with httpx.Client(
                timeout=httpx.Timeout(
                    HTTP_READ_TIMEOUT_S, connect=HTTP_CONNECT_TIMEOUT_S
                ),
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = client.request(
                    method,
                    url,
                    json=body if body is not None else None,
                    headers=self._headers(idempotency=idempotency, cursor=cursor),
                )
        except httpx.HTTPError as exc:
            raise UpstreamError(
                f"the console could not be reached: {redact(type(exc).__name__)}"
            ) from exc

        if response.status_code in {301, 302, 303, 307, 308}:
            raise UnsafeEnvironmentError(
                "the console redirected this request; refusing to follow it"
            )
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise UpstreamError("the console response is larger than this CLI accepts")

        if expect_text:
            payload: Any = response.text
        else:
            try:
                payload = response.json() if response.content else None
            except ValueError:
                payload = None
        return ConsoleResponse(
            status=response.status_code, body=payload, headers=dict(response.headers)
        )

    def call(self, method: str, path: str, **kwargs: Any) -> ConsoleResponse:
        """Request, then convert any non-2xx into the right exit code."""
        response = self.request(method, path, **kwargs)
        if 200 <= response.status < 300:
            return response
        raise self._as_error(response)

    @staticmethod
    def _as_error(response: ConsoleResponse) -> CliError:
        error = {}
        if isinstance(response.body, Mapping):
            candidate = response.body.get("error")
            if isinstance(candidate, Mapping):
                error = dict(candidate)
        code = str(error.get("code") or "")
        message = redact(str(error.get("message") or f"HTTP {response.status}"))
        current = error.get("current_version")
        if code == "BATCH_LEASE_LOST":
            return LeaseLostError(message, hint="re-claim the batch, or stop the keeper")
        if response.status in {401, 403}:
            return AuthError(message)
        if response.status == 404:
            return NotFoundError(message)
        if response.status in {409, 412, 428, 422}:
            hint = None
            if current is not None:
                hint = f"the current version is {current}"
            if response.status == 422:
                return ValidationError(message, hint=hint)
            return ConflictError(message, hint=hint)
        if response.status == 429:
            return UpstreamError(message, hint="slow down and retry")
        if response.status == 503:
            return UpstreamError(message, hint="the console reports itself unavailable")
        return UpstreamError(message)

    def reviews_with_rating(
        self, rating: int, *, status: Optional[str] = None
    ) -> list[Mapping[str, Any]]:
        params: list[tuple[str, str]] = [
            ("facet", "rating"),
            ("facet_value", str(rating)),
            ("page_size", "100"),
        ]
        if status:
            params.append(("statuses", status))
        path = f"{API_PREFIX}{REVIEWS_PATH}?{urlencode(params)}"
        items: list[Mapping[str, Any]] = []
        cursor: Optional[str] = None
        seen_cursors: set[str] = set()
        for _page in range(MAX_REVIEW_QUERY_PAGES):
            response = self.call("GET", path, cursor=cursor)
            body = response.body
            if not isinstance(body, Mapping) or not isinstance(body.get("items"), list):
                raise UpstreamError("the review queue response has no item list")
            items.extend(item for item in body["items"] if isinstance(item, Mapping))

            next_cursor = body.get("next_cursor")
            if next_cursor is None:
                return items
            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or len(next_cursor) > MAX_REVIEW_CURSOR_LENGTH
                or next_cursor in seen_cursors
            ):
                raise UpstreamError("the review queue returned an invalid page cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise UpstreamError("the review queue exceeded the pagination safety limit")

    # -- the batch surface ------------------------------------------------

    def batch_path(self, batch_id: str, suffix: str = "") -> str:
        return f"{API_PREFIX}{BATCHES_PATH}/{validated_batch_id(batch_id)}{suffix}"

    def show(self, batch_id: str) -> Mapping[str, Any]:
        return self.call("GET", self.batch_path(batch_id)).body

    def claim(self, batch_id: str) -> tuple[Mapping[str, Any], str]:
        response = self.call(
            "POST",
            self.batch_path(batch_id, "/claim"),
            idempotency=new_idempotency_key("claim"),
        )
        token = response.headers.get(LEASE_TOKEN_HEADER.lower()) or response.headers.get(
            LEASE_TOKEN_HEADER
        )
        if not token:
            raise UpstreamError(
                "the console did not return a lease token for this claim"
            )
        return response.body, token

    def heartbeat(self, batch_id: str, *, version: int, token: str) -> Mapping[str, Any]:
        return self.call(
            "POST",
            self.batch_path(batch_id, "/heartbeat"),
            body={"expected_version": version, "lease_token": token},
            idempotency=new_idempotency_key("beat"),
        ).body

    def materialize(
        self,
        batch_id: str,
        *,
        version: int,
        token: str,
        include_conversation: bool = False,
        cursor: Optional[str] = None,
    ) -> Mapping[str, Any]:
        return self.call(
            "POST",
            self.batch_path(batch_id, ":materialize"),
            body={
                "expected_version": version,
                "lease_token": token,
                "include_conversation": bool(include_conversation),
            },
            idempotency=new_idempotency_key("mat"),
            cursor=cursor,
        ).body

    def patch(self, batch_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.call(
            "PATCH",
            self.batch_path(batch_id),
            body=dict(payload),
            idempotency=new_idempotency_key("patch"),
        ).body

    def release(
        self, batch_id: str, *, version: int, token: str, disposition: str, reason: str
    ) -> Mapping[str, Any]:
        return self.call(
            "POST",
            self.batch_path(batch_id, ":release"),
            body={
                "expected_version": version,
                "lease_token": token,
                "disposition": disposition,
                "reason": reason,
            },
            idempotency=new_idempotency_key("release"),
        ).body


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

#: Fields a human view of a batch shows. Everything else is available with
#: ``--json``. The reviewer's own comments are absent on purpose: the default
#: output of a command an agent runs in a shared terminal must not include
#: ticket narrative.
_SUMMARY_FIELDS = (
    "batch_id",
    "status",
    "version",
    "item_count",
    "branch",
    "commit_sha",
    "uncommitted_reason",
    "plan_artifact",
    "verification_summary",
    "prompt_template_version",
)

_BELOW_RATING_FIELDS = (
    "review_id",
    "devrev_display_id",
    "rating",
    "observation_type",
    "severity",
    "remediation_target",
    "status",
    "comments",
    "expected_behavior",
    "modified_surfaces",
    "remediation_summary",
    "ticket_job_ids",
    "request_id_hashes",
    "source_article_ids",
)


def emit(payload: Any, *, as_json: bool, stream=None) -> None:
    out = stream if stream is not None else sys.stdout
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str), file=out)
        return
    if isinstance(payload, Mapping):
        for key in _SUMMARY_FIELDS:
            if key in payload and payload[key] not in (None, "", []):
                print(f"{key}: {payload[key]}", file=out)
        lease = payload.get("lease")
        if isinstance(lease, Mapping):
            print(f"lease_expires_at: {lease.get('expires_at')}", file=out)
        for key in ("changed_files", "planned_review_ids", "unchanged_review_ids"):
            values = payload.get(key)
            if values:
                print(f"{key}: {len(values)}", file=out)
        return
    print(payload, file=out)


def announce(
    *, console_url: str, environment: str, repo_id: str, base_ref: str,
    batch_id: Optional[str], apply: bool, stream=None,
) -> None:
    """State where a write is going, and in which mode, before it goes.

    Printed for reads too. The failure this exists to prevent is an operator who
    believes they are pointed at staging.
    """
    out = stream if stream is not None else sys.stderr
    target = batch_id or "-"
    mode = "APPLY" if apply else "dry-run"
    print(
        f"console={console_url} environment={environment} repo={repo_id} "
        f"base={base_ref} batch={target} mode={mode}",
        file=out,
    )


# ---------------------------------------------------------------------------
# The lease keeper
# ---------------------------------------------------------------------------


@dataclass
class KeeperResult:
    reason: str
    beats: int


class LeaseKeeper:
    """Renews one claim, in one process, for at most the continuous cap.

    Everything it needs is injected — the client, the clock, the sleeper, and a
    stop predicate — because the interesting behaviour is entirely about *when*
    it stops: on lease loss, on a stop signal, on a terminal batch state, and at
    the two-hour cap. A keeper that only worked in a real process would leave all
    four of those untested.

    It stores a PID, a nonce, and a start time. It never stores an IAP assertion:
    those last five minutes and are re-minted per request, so persisting one
    would be a credential on disk for no benefit.
    """

    #: Statuses after which there is nothing left to renew.
    TERMINAL = frozenset(
        {"changes_proposed", "verifying", "completed", "blocked", "cancelled", "ready", "expired"}
    )

    def __init__(
        self,
        *,
        client: ConsoleClient,
        store: LeaseStore,
        batch_id: str,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        interval_s: int = REMEDIATION_HEARTBEAT_S,
        cap_s: int = REMEDIATION_MAX_CONTINUOUS_LEASE_S,
    ) -> None:
        self.client = client
        self.store = store
        self.batch_id = batch_id
        self.clock = clock
        self.sleeper = sleeper
        self.interval_s = interval_s
        self.cap_s = cap_s
        self._stop = False

    def request_stop(self, *_args: Any) -> None:
        self._stop = True

    def run(self) -> KeeperResult:
        started = self.clock()
        beats = 0
        while True:
            if self._stop:
                return KeeperResult("stopped", beats)
            if self.clock() - started >= self.cap_s:
                return KeeperResult("continuous_cap_reached", beats)
            token = self.store.load_token(self.batch_id)
            if token is None:
                return KeeperResult("lease_file_removed", beats)
            try:
                batch = self.client.show(self.batch_id)
            except (NotFoundError, AuthError):
                return KeeperResult("batch_unreadable", beats)
            status = str(batch.get("status") or "")
            if status in self.TERMINAL:
                return KeeperResult(f"batch_{status}", beats)
            try:
                self.client.heartbeat(
                    self.batch_id, version=int(batch["version"]), token=token
                )
            except LeaseLostError:
                return KeeperResult("lease_lost", beats)
            except ConflictError:
                # A version race is normal: the agent wrote between the read and
                # the beat. Re-read on the next tick rather than giving up a
                # perfectly good claim.
                pass
            except UpstreamError:
                pass
            else:
                beats += 1
            remaining = self.cap_s - (self.clock() - started)
            if remaining <= 0:
                return KeeperResult("continuous_cap_reached", beats)
            self.sleeper(min(float(self.interval_s), remaining))


def _process_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def keeper_health(store: LeaseStore, batch_id: str) -> dict[str, Any]:
    """Whether a keeper for this batch is running, and why not if it is not."""
    record = store.load_keeper(batch_id)
    if record is None:
        return {"healthy": False, "reason": "no keeper is recorded for this batch"}
    pid = record.get("pid")
    nonce = record.get("nonce")
    if not isinstance(pid, int) or not isinstance(nonce, str) or not nonce:
        return {"healthy": False, "reason": "the keeper record is incomplete"}
    if not _process_alive(pid):
        # The agent, or its keeper, exited without cleaning up. Say so and name
        # the fix rather than pretending the lease is still being renewed.
        return {
            "healthy": False,
            "reason": "the recorded keeper process is gone",
            "pid": pid,
            "stale": True,
        }
    if store.load_token(batch_id) is None:
        return {"healthy": False, "reason": "the lease token file is missing", "pid": pid}
    return {"healthy": True, "pid": pid, "started_at": record.get("started_at")}


def stop_keeper(
    store: LeaseStore,
    batch_id: str,
    *,
    killer: Callable[[int, int], None] = os.kill,
    waiter: Callable[[float], None] = time.sleep,
    alive: Callable[[int], bool] = _process_alive,
    attempts: int = 10,
) -> dict[str, Any]:
    """Signal exactly the recorded process, after proving it is the right one.

    The PID is validated *with* the nonce that the keeper wrote for this batch,
    because a PID on its own is not an identity: the number is reused, and by the
    time a stale record is noticed it may belong to something else entirely.
    There is deliberately no ``pkill``, no process-name match, and no glob — each
    of those would eventually signal a process this program never started.
    """
    record = store.load_keeper(batch_id)
    if record is None:
        return {"stopped": False, "reason": "no keeper is recorded for this batch"}
    pid = record.get("pid")
    nonce = record.get("nonce")
    if not isinstance(pid, int) or not isinstance(nonce, str) or not nonce:
        store.forget(batch_id)
        return {"stopped": False, "reason": "the keeper record was unusable; removed"}
    if not alive(pid):
        removed = store.forget(batch_id)
        return {"stopped": True, "reason": "already gone", "removed": removed}
    killer(pid, signal.SIGTERM)
    for _ in range(attempts):
        if not alive(pid):
            break
        waiter(0.2)
    still_running = alive(pid)
    removed = [] if still_running else store.forget(batch_id)
    return {
        "stopped": not still_running,
        "pid": pid,
        "removed": removed,
        "reason": "did not exit after SIGTERM" if still_running else "exited",
    }


Spawner = Callable[[Sequence[str], Mapping[str, str]], int]


def _spawn_detached(argv: Sequence[str], environment: Mapping[str, str]) -> int:
    """Start the keeper as its own session, with no inherited stdin."""
    process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=dict(environment),
    )
    return process.pid


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@dataclass
class Context:
    """Everything a command needs, with every collaborator injectable."""

    args: argparse.Namespace
    store: LeaseStore
    client: ConsoleClient
    signer: TokenSigner
    spawner: Spawner = _spawn_detached
    stdout: Any = None
    stderr: Any = None

    @property
    def batch_id(self) -> str:
        return validated_batch_id(getattr(self.args, "batch_id", "") or "")

    @property
    def as_json(self) -> bool:
        return bool(getattr(self.args, "json", False))

    def out(self, payload: Any) -> None:
        emit(payload, as_json=self.as_json, stream=self.stdout)

    def note(self, message: str) -> None:
        print(message, file=self.stderr if self.stderr is not None else sys.stderr)

    def require_apply(self, description: str) -> bool:
        """True when the write may proceed; otherwise report the dry run."""
        if getattr(self.args, "apply", False):
            return True
        self.note(f"dry-run: would {description}. Re-run with --apply to send it.")
        return False

    def require_healthy_lease(self) -> tuple[str, Mapping[str, Any]]:
        """A live keeper plus a stored token, before any lease-bound write."""
        health = keeper_health(self.store, self.batch_id)
        if not health.get("healthy"):
            raise LeaseLostError(
                f"the lease keeper is not healthy: {health.get('reason')}",
                hint="run `batch lease-start`, or re-claim the batch",
            )
        token = self.store.require_token(self.batch_id)
        return token, self.client.show(self.batch_id)


def _assert_repository_matches(context: Context) -> None:
    """Confirm the checkout really is the repository the batch names.

    The base ref is compared against what Git reports, not against anything the
    console said about the filesystem. A remote record must never be able to tell
    this program which directory to treat as the repository.
    """
    args = context.args
    if getattr(args, "skip_repo_check", False):
        return
    head = _run_git(["rev-parse", "--abbrev-ref", "HEAD"])
    expected = validated_ref(args.expected_base_ref, label="--expected-base-ref")
    if head == expected:
        return
    try:
        _run_git(["merge-base", "--is-ancestor", expected, "HEAD"])
    except ValidationError as exc:
        raise UnsafeEnvironmentError(
            f"this checkout is on {head!r} and {expected!r} is not an ancestor of it"
        ) from exc


def _review_updated_at(item: Mapping[str, Any]) -> datetime:
    """Return one comparable UTC instant, with malformed values sorted last."""
    raw = item.get("updated_at")
    if not isinstance(raw, str):
        return datetime.min.replace(tzinfo=timezone.utc)
    normalized = f"{raw[:-1]}+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def command_reviews_below_rating(context: Context) -> int:
    """Merge equality queries into the low-rated review set."""
    threshold = int(context.args.rating_below)
    status = getattr(context.args, "status", None)
    unique: dict[str, Mapping[str, Any]] = {}
    for rating in range(1, threshold):
        for item in context.client.reviews_with_rating(rating, status=status):
            actual = item.get("rating")
            if isinstance(actual, bool) or not isinstance(actual, int):
                continue
            if actual < 1 or actual >= threshold:
                continue
            review_id = str(item.get("review_id") or "")
            if not REVIEW_ID_PATTERN.match(review_id):
                continue
            unique.setdefault(review_id, item)

    ordered = list(unique.values())
    # Python's sort is stable, so equal timestamps retain rating-query order.
    ordered.sort(key=_review_updated_at, reverse=True)
    payload = [
        {field: item.get(field) for field in _BELOW_RATING_FIELDS}
        for item in ordered
    ]
    if context.as_json:
        context.out(payload)
    else:
        out = context.stdout if context.stdout is not None else sys.stdout
        for item in payload:
            print(
                f"{item['review_id']} {item['devrev_display_id'] or '-'} "
                f"rating={item['rating']} status={item['status'] or '-'}",
                file=out,
            )
    return EXIT_OK


def command_auth_doctor(context: Context) -> int:
    """Check every precondition, and print no credential while doing it."""
    args = context.args
    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    description = context.signer.describe()
    record("signer", True, description["mode"])

    try:
        _assert_repository_matches(context)
        record("repository", True, f"base ref {args.expected_base_ref} is reachable")
    except CliError as exc:
        record("repository", False, str(exc))

    try:
        store = context.store.ensure()
        mode = stat.S_IMODE(store.stat().st_mode)
        record("lease_directory", mode == 0o700, f"{store} mode {mode:04o}")
    except CliError as exc:
        record("lease_directory", False, str(exc))

    try:
        response = context.client.request("GET", f"{API_PREFIX}/session")
        if response.status in {200, 403}:
            # 403 is the expected answer for the agent identity: the console
            # deliberately gives the agent no browser session. Reaching the
            # router at all is what this check is for.
            record("console", True, f"reachable, HTTP {response.status}")
        else:
            record("console", False, f"HTTP {response.status}")
    except CliError as exc:
        record("console", False, str(exc))

    skew = None
    try:
        probe = context.client.request("GET", f"{API_PREFIX}/session")
        served = probe.headers.get("date")
        if served:
            from email.utils import parsedate_to_datetime

            remote = parsedate_to_datetime(served).timestamp()
            skew = abs(remote - time.time())
            record("clock_skew", skew < 120, f"{skew:.0f}s from the console")
    except Exception:  # noqa: BLE001 - a diagnostic must not raise
        record("clock_skew", False, "the console did not report a usable date")

    if getattr(args, "batch_id", None):
        try:
            batch = context.client.show(context.batch_id)
            record("batch", True, f"status {batch.get('status')}")
        except CliError as exc:
            record("batch", False, str(exc))

    payload = {
        "identity": description,
        "environment": args.environment,
        "repo_id": args.repo_id,
        "expected_base_ref": args.expected_base_ref,
        "checks": checks,
    }
    if context.as_json:
        context.out(payload)
    else:
        for entry in checks:
            marker = "ok  " if entry["ok"] else "FAIL"
            context.note(f"{marker} {entry['check']}: {entry['detail']}")
    return EXIT_OK if all(entry["ok"] for entry in checks) else EXIT_AUTH


def command_show(context: Context) -> int:
    context.out(context.client.show(context.batch_id))
    return EXIT_OK


def command_claim(context: Context) -> int:
    _assert_repository_matches(context)
    if not context.require_apply(f"claim batch {context.batch_id}"):
        return EXIT_OK
    batch, token = context.client.claim(context.batch_id)
    expires = str(batch.get("lease_expires_at") or "")
    context.store.save_token(context.batch_id, token, expires_at=expires)
    # The token is on disk at 0600 and is never echoed; the path is, so an
    # operator can find and delete it.
    context.note(f"lease stored at {context.store.token_path(context.batch_id)}")
    context.out(batch.get("batch") or batch)
    return EXIT_OK


def command_heartbeat(context: Context) -> int:
    token = context.store.require_token(context.batch_id)
    batch = context.client.show(context.batch_id)
    if not context.require_apply("send one heartbeat"):
        return EXIT_OK
    context.out(
        context.client.heartbeat(
            context.batch_id, version=int(batch["version"]), token=token
        )
    )
    return EXIT_OK


def command_lease_start(context: Context) -> int:
    token = context.store.load_token(context.batch_id)
    if token is None:
        raise LeaseLostError(
            "no lease is stored for this batch",
            hint="run `batch claim --apply` first",
        )
    existing = keeper_health(context.store, context.batch_id)
    if existing.get("healthy"):
        context.out({"already_running": True, **existing})
        return EXIT_OK
    if existing.get("stale"):
        context.note("removing a stale keeper record before starting a new one")
        context.store.forget(context.batch_id)
        context.store.save_token(context.batch_id, token, expires_at="")

    nonce = uuid.uuid4().hex
    argv = [
        sys.executable,
        str(Path(__file__).resolve()),
        "batch",
        "lease-keeper",
        "--console-url",
        context.args.console_url,
        "--environment",
        context.args.environment,
        "--repo-id",
        context.args.repo_id,
        "--expected-base-ref",
        context.args.expected_base_ref,
        "--batch-id",
        context.batch_id,
        "--keeper-nonce",
        nonce,
    ]
    pid = context.spawner(argv, dict(os.environ))
    context.store.save_keeper(
        context.batch_id,
        {"pid": int(pid), "nonce": nonce, "started_at": time.time()},
    )
    context.out({"started": True, "pid": int(pid)})
    return EXIT_OK


def command_lease_status(context: Context) -> int:
    health = keeper_health(context.store, context.batch_id)
    context.out(health)
    return EXIT_OK if health.get("healthy") else EXIT_LEASE_LOST


def command_lease_stop(context: Context) -> int:
    context.out(stop_keeper(context.store, context.batch_id))
    return EXIT_OK


def command_lease_keeper(context: Context) -> int:
    """The keeper process itself. Not part of the documented surface."""
    keeper = LeaseKeeper(
        client=context.client, store=context.store, batch_id=context.batch_id
    )
    signal.signal(signal.SIGTERM, keeper.request_stop)
    signal.signal(signal.SIGINT, keeper.request_stop)
    result = keeper.run()
    context.out({"reason": result.reason, "beats": result.beats})
    return EXIT_OK


def command_materialize(context: Context) -> int:
    token, batch = context.require_healthy_lease()
    if context.args.include_conversation:
        context.note(
            "WARNING: --include-conversation returns reviewer-authored notes about "
            "real participants. Do not paste this output into a chat, an issue, a "
            "commit message, or any other shared channel."
        )
    pages: list[Any] = []
    cursor = None
    version = int(batch["version"])
    while True:
        page = context.client.materialize(
            context.batch_id,
            version=version,
            token=token,
            include_conversation=context.args.include_conversation,
            cursor=cursor,
        )
        pages.append(page)
        cursor = page.get("next_cursor")
        if not cursor:
            break
    items = [item for page in pages for item in page.get("items", [])]
    drifted = sorted({rid for page in pages for rid in page.get("drifted_review_ids", [])})
    warnings = sorted({w for page in pages for w in page.get("warnings", [])})
    payload = {
        "batch_id": context.batch_id,
        "batch_version": version,
        "items": items,
        "drifted_review_ids": drifted,
        "conversation_included": bool(context.args.include_conversation),
        "warnings": warnings,
    }
    if context.as_json:
        context.out(payload)
    else:
        context.note(f"{len(items)} frozen observation(s)")
        for item in items:
            context.note(
                f"  {item.get('review_id', '')[:12]} "
                f"{item.get('devrev_display_id') or '-'} "
                f"{item.get('observation_type') or '-'} "
                f"severity={item.get('severity') or '-'} "
                f"target={item.get('remediation_target') or '-'}"
            )
        for warning in warnings:
            context.note(f"  warning: {warning}")
    return EXIT_OK


def _confirmed(context: Context, count: int) -> bool:
    """Require an explicit acknowledgement for a multi-review status change.

    A submission decides the recorded outcome of every observation in the batch
    at once. ``--yes`` is accepted for a non-interactive agent; without a tty and
    without the flag, the write is refused rather than assumed.
    """
    if count <= 1 or getattr(context.args, "yes", False):
        return True
    if not sys.stdin.isatty():
        raise ValidationError(
            f"this would set an outcome on {count} reviews at once; pass --yes",
        )
    context.note(f"This records an outcome for {count} reviews. Type 'yes' to continue: ")
    return sys.stdin.readline().strip().lower() == "yes"


def command_record_plan(context: Context) -> int:
    token, batch = context.require_healthy_lease()
    plan = validated_repo_path(context.args.plan_artifact)
    if not context.require_apply(f"record plan {plan}"):
        return EXIT_OK
    payload: dict[str, Any] = {
        "expected_version": int(batch["version"]),
        "lease_token": token,
        "plan_artifact": plan,
    }
    if str(batch.get("status")) == "claimed":
        payload["transition"] = "planning"
    context.out(context.client.patch(context.batch_id, payload))
    return EXIT_OK


def command_record_progress(context: Context) -> int:
    token, batch = context.require_healthy_lease()
    payload: dict[str, Any] = {
        "expected_version": int(batch["version"]),
        "lease_token": token,
    }
    if context.args.branch:
        payload["branch"] = validated_ref(context.args.branch, label="--branch")
    if context.args.changed_file:
        payload["changed_files"] = [
            validated_repo_path(path) for path in context.args.changed_file
        ]
    if str(batch.get("status")) == "planning":
        payload["transition"] = "in_progress"
    if not context.require_apply("record progress"):
        return EXIT_OK
    context.out(context.client.patch(context.batch_id, payload))
    return EXIT_OK


def _parsed_outcomes(values: Iterable[str]) -> list[dict[str, str]]:
    outcomes: list[dict[str, str]] = []
    for raw in values:
        review_id, separator, outcome = str(raw).partition("=")
        if not separator or not outcome.strip():
            raise ValidationError(
                "--review-outcome takes <review-id>=<outcome>"
            )
        outcomes.append(
            {
                "review_id": validated_review_id(review_id),
                "outcome": outcome.strip()[:80],
            }
        )
        if len(outcomes) != len({entry["review_id"] for entry in outcomes}):
            raise ValidationError("--review-outcome repeats a review id")
    return outcomes


def _parsed_tests(labels: Sequence[str], codes: Sequence[int]) -> list[dict[str, Any]]:
    if len(labels) != len(codes):
        raise ValidationError(
            "--test-command and --test-exit-code must be given the same number of times"
        )
    records = []
    for label, code in zip(labels, codes, strict=True):
        cleaned = " ".join(str(label).split())[:320]
        if not cleaned:
            raise ValidationError("--test-command may not be blank")
        records.append(
            {
                "command_label": cleaned,
                "exit_code": int(code),
                "passed": 0,
                "failed": 0,
                "skipped": 0,
                # The digest is of the recorded label, not of captured output:
                # this CLI never reads a test log, so claiming to hash one would
                # be a lie in an audited record.
                "output_sha256": hashlib.sha256(cleaned.encode("utf-8")).hexdigest(),
                "runtime_s": 0.0,
            }
        )
    return records


def command_submit(context: Context) -> int:
    args = context.args
    token, batch = context.require_healthy_lease()
    outcomes = _parsed_outcomes(args.review_outcome or ())
    if not outcomes:
        raise ValidationError("--review-outcome is required for every frozen review")
    if not _confirmed(context, len(outcomes)):
        context.note("refused")
        return EXIT_VALIDATION
    payload: dict[str, Any] = {
        "expected_version": int(batch["version"]),
        "lease_token": token,
        "transition": "changes_proposed",
        "branch": validated_ref(args.branch, label="--branch"),
        "changed_files": [validated_repo_path(path) for path in (args.changed_file or ())],
        "test_evidence": _parsed_tests(
            args.test_command or (), args.test_exit_code or ()
        ),
        "per_review_outcomes": outcomes,
        "summary": (args.summary or "").strip(),
    }
    if args.commit_sha:
        payload["commit_sha"] = validated_commit_sha(args.commit_sha)
    elif args.uncommitted_reason:
        payload["uncommitted_reason"] = args.uncommitted_reason.strip()
    else:
        raise ValidationError(
            "supply --commit-sha, or --uncommitted-reason to say why there is none"
        )
    if not context.require_apply(
        f"submit batch {context.batch_id} to changes_proposed"
    ):
        return EXIT_OK
    result = context.client.patch(context.batch_id, payload)
    # The claim ended with the submission, so the token on disk is now useless.
    removed = context.store.forget(context.batch_id)
    if removed:
        context.note(f"removed {', '.join(removed)}")
    context.out(result)
    return EXIT_OK


def _release(context: Context, *, disposition: str, reason: str) -> int:
    token = context.store.require_token(context.batch_id)
    batch = context.client.show(context.batch_id)
    if not context.require_apply(f"release batch to {disposition}"):
        return EXIT_OK
    result = context.client.release(
        context.batch_id,
        version=int(batch["version"]),
        token=token,
        disposition=disposition,
        reason=reason,
    )
    stopped = stop_keeper(context.store, context.batch_id)
    removed = context.store.forget(context.batch_id)
    if removed or stopped.get("removed"):
        context.note("lease state removed")
    context.out(result)
    return EXIT_OK


def command_release(context: Context) -> int:
    return _release(
        context, disposition="ready", reason=(context.args.reason or "").strip()
    )


def command_block(context: Context) -> int:
    reason = (context.args.reason or "").strip()
    if not reason:
        raise ValidationError("--reason is required to block a batch")
    return _release(context, disposition="blocked", reason=reason)


COMMANDS: dict[str, Callable[[Context], int]] = {
    "reviews below-rating": command_reviews_below_rating,
    "auth doctor": command_auth_doctor,
    "batch show": command_show,
    "batch claim": command_claim,
    "batch heartbeat": command_heartbeat,
    "batch lease-start": command_lease_start,
    "batch lease-status": command_lease_status,
    "batch lease-stop": command_lease_stop,
    "batch lease-keeper": command_lease_keeper,
    "batch materialize": command_materialize,
    "batch record-plan": command_record_plan,
    "batch record-progress": command_record_progress,
    "batch submit": command_submit,
    "batch block": command_block,
    "batch release": command_release,
}

#: The documented surface. ``lease-keeper`` is the keeper's own re-entry point and
#: is not offered to an operator.
PUBLIC_COMMANDS = tuple(name for name in COMMANDS if not name.endswith("lease-keeper"))


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser, *, needs_batch: bool) -> None:
    """The four values every command states explicitly, plus the batch.

    None of them defaults. A CLI that guessed its console, its environment, or
    its repository from ambient state is one shell history entry away from
    writing to production.
    """
    parser.add_argument("--console-url", required=True, help="exact console origin")
    parser.add_argument(
        "--environment", required=True, choices=list(VALID_ENVIRONMENTS)
    )
    parser.add_argument("--repo-id", required=True, help="configured repository id")
    parser.add_argument(
        "--expected-base-ref",
        default="main",
        help="the base ref this work is expected to branch from",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--skip-repo-check",
        action="store_true",
        help="skip the local git base-ref check (diagnostics only)",
    )
    if needs_batch:
        parser.add_argument("--batch-id", required=True)


def _add_review_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--console-url", required=True, help="exact console origin")
    parser.add_argument(
        "--environment", required=True, choices=list(VALID_ENVIRONMENTS)
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ticket_review_cli.py",
        description=(
            "Claim, read, and report on a ticket-review remediation batch through "
            "the console API. Never touches the database, and cannot verify or "
            "complete a batch."
        ),
    )
    top = parser.add_subparsers(dest="group", required=True)

    auth = top.add_parser("auth", help="authentication diagnostics")
    auth_sub = auth.add_subparsers(dest="command", required=True)
    doctor = auth_sub.add_parser("doctor", help="check credentials and reachability")
    _add_common(doctor, needs_batch=False)
    doctor.add_argument("--batch-id", help="optionally also probe one batch")

    reviews = top.add_parser("reviews", help="query durable reviews")
    reviews_sub = reviews.add_subparsers(dest="command", required=True)
    below_rating = reviews_sub.add_parser(
        "below-rating",
        help="list reviews whose recorded rating is below a threshold",
        description=(
            "List reviews whose recorded rating is below the threshold. "
            "Unrated reviews are excluded because unrated is not low-rated."
        ),
    )
    _add_review_common(below_rating)
    below_rating.add_argument(
        "--rating-below",
        type=int,
        choices=range(2, 7),
        default=5,
        help="exclusive rating threshold (default: 5)",
    )
    below_rating.add_argument(
        "--status",
        choices=REVIEW_STATUSES,
        help="optionally require one review status",
    )

    batch = top.add_parser("batch", help="work on one remediation batch")
    batch_sub = batch.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("show", "print the batch status"),
        ("lease-status", "report whether the lease keeper is healthy"),
        ("lease-stop", "stop the lease keeper for this batch"),
    ):
        sub = batch_sub.add_parser(name, help=help_text)
        _add_common(sub, needs_batch=True)

    for name, help_text in (
        ("claim", "claim the batch and store its lease"),
        ("heartbeat", "renew the lease once"),
        ("lease-start", "start the bounded background lease keeper"),
    ):
        sub = batch_sub.add_parser(name, help=help_text)
        _add_common(sub, needs_batch=True)
        sub.add_argument("--apply", action="store_true", help="actually send the write")

    # The keeper's own re-entry point. Named in the help rather than hidden:
    # `add_parser(help=SUPPRESS)` still lists the choice, so a suppressed entry
    # would read as a bug, and an operator who finds this in a process list
    # deserves to be able to look it up.
    keeper = batch_sub.add_parser(
        "lease-keeper", help="internal: the background keeper process itself"
    )
    _add_common(keeper, needs_batch=True)
    keeper.add_argument("--keeper-nonce", required=True, help=argparse.SUPPRESS)

    materialize = batch_sub.add_parser(
        "materialize", help="read the frozen observations"
    )
    _add_common(materialize, needs_batch=True)
    materialize.add_argument(
        "--include-conversation",
        action="store_true",
        help="also return reviewer-authored notes (do not share this output)",
    )

    plan = batch_sub.add_parser("record-plan", help="record the implementation plan")
    _add_common(plan, needs_batch=True)
    plan.add_argument("--plan-artifact", required=True, help="repo-relative plan path")
    plan.add_argument("--apply", action="store_true")

    progress = batch_sub.add_parser("record-progress", help="record interim progress")
    _add_common(progress, needs_batch=True)
    progress.add_argument("--branch")
    progress.add_argument("--changed-file", action="append")
    progress.add_argument("--apply", action="store_true")

    submit = batch_sub.add_parser("submit", help="submit the work for human review")
    _add_common(submit, needs_batch=True)
    submit.add_argument("--branch", required=True)
    submit.add_argument("--commit-sha")
    submit.add_argument(
        "--uncommitted-reason", help="why there is no commit, if there is none"
    )
    submit.add_argument("--changed-file", action="append", required=True)
    submit.add_argument("--test-command", action="append", required=True)
    submit.add_argument("--test-exit-code", action="append", type=int, required=True)
    submit.add_argument("--summary", required=True)
    submit.add_argument(
        "--review-outcome",
        action="append",
        required=True,
        metavar="REVIEW_ID=OUTCOME",
        help="one per frozen review",
    )
    submit.add_argument("--yes", action="store_true", help="skip the confirmation")
    submit.add_argument("--apply", action="store_true")

    block = batch_sub.add_parser("block", help="hand the batch back as blocked")
    _add_common(block, needs_batch=True)
    block.add_argument("--reason", required=True)
    block.add_argument("--apply", action="store_true")

    release = batch_sub.add_parser("release", help="hand an untouched batch back")
    _add_common(release, needs_batch=True)
    release.add_argument("--reason", default="released without durable work")
    release.add_argument("--apply", action="store_true")

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(
    argv: Optional[Sequence[str]] = None,
    *,
    transport: Optional[httpx.BaseTransport] = None,
    git_runner: GitRunner = _run_git,
    signer: Optional[TokenSigner] = None,
    spawner: Spawner = _spawn_detached,
    store: Optional[LeaseStore] = None,
    stdout: Any = None,
    stderr: Any = None,
) -> int:
    """Parse, dispatch, and turn every failure into its documented exit code."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    name = f"{args.group} {args.command}"
    handler = COMMANDS.get(name)
    if handler is None:  # pragma: no cover - argparse rejects first
        parser.error(f"unknown command {name!r}")

    try:
        environment = validated_environment(args.environment)
        console_url = validated_console_url(args.console_url, environment=environment)
        args.environment = environment
        args.console_url = console_url
        needs_store = args.group != "reviews"
        resolved_store = store or (
            LeaseStore.resolve(runner=git_runner) if needs_store else None
        )
        resolved_signer = signer or build_signer(
            console_url=console_url, environment=environment, transport=transport
        )
        client = ConsoleClient(
            console_url=console_url, signer=resolved_signer, transport=transport
        )
        context = Context(
            args=args,
            # Review queries never touch lease state. Batch/auth commands always
            # resolve a real store before reaching their handler.
            store=cast(LeaseStore, resolved_store),
            client=client,
            signer=resolved_signer,
            spawner=spawner,
            stdout=stdout,
            stderr=stderr,
        )
        announce(
            console_url=console_url,
            environment=environment,
            repo_id=getattr(args, "repo_id", "-"),
            base_ref=getattr(args, "expected_base_ref", "-"),
            batch_id=getattr(args, "batch_id", None),
            apply=bool(getattr(args, "apply", False)),
            stream=stderr,
        )
        return handler(context)
    except CliError as exc:
        stream = stderr if stderr is not None else sys.stderr
        print(f"error: {redact(str(exc))}", file=stream)
        if exc.hint:
            print(f"hint: {redact(exc.hint)}", file=stream)
        return exc.exit_code
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        return EXIT_VALIDATION


def main(argv: Optional[Sequence[str]] = None) -> int:
    return run(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
