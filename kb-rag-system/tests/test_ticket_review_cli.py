"""Stage 8 Step 2 — the repository CLI.

Everything here runs with an **injected** HTTP transport, an injected token
signer, an injected process spawner, and a lease store rooted in a temporary
directory. No test requires ADC, IAP, a network, or a real ``git`` repository,
and no test constructs a Firestore client — the CLI has no code path that could.

The three things worth defending
--------------------------------
*   **The lease token is a credential on disk.** It lives only under the
    directory Git itself names, at mode ``0600``, is never printed, and is
    removed on release or submission. The linked-worktree case (``.git`` is a
    *file*) is the normal case for this project, so it is the one exercised.
*   **The keeper is a process this program must be able to stop safely.** It is
    signalled by a PID that was validated together with the nonce written for
    that batch — never by name, never by glob, never by an unrelated PID — and
    its abrupt death is reported rather than mistaken for health.
*   **A write cannot happen by accident.** Deployed writes need an active claim,
    a matching repository, and ``--apply``; the default is a dry run that sends
    nothing.
"""

from __future__ import annotations

import io
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT))

from scripts import ticket_review_cli as cli  # noqa: E402

# =====================================================================
# Synthetic constants
# =====================================================================

BATCH_ID = "0" * 31 + "1"
OTHER_BATCH_ID = "f" * 32
REVIEW_A = "a" * 64
REVIEW_B = "b" * 64
REVIEW_C = "c" * 64
COMMIT = "c" * 40

DEPLOYED_CONSOLE = "https://tickets-console-abc-uc.a.run.app"
LOOPBACK_CONSOLE = "http://127.0.0.1:8010"
AGENT_SA = "tickets-remediation-agent@rag-kb-system.iam.gserviceaccount.com"
REPO_ID = "synthetic-repo"
BASE_REF = "main"

LEASE_TOKEN = "synthetic-lease-token-value"  # pragma: allowlist secret


def _base_args(*, console: str = DEPLOYED_CONSOLE, environment: str = "staging") -> list[str]:
    return [
        "--console-url",
        console,
        "--environment",
        environment,
        "--repo-id",
        REPO_ID,
        "--expected-base-ref",
        BASE_REF,
        "--skip-repo-check",
    ]


class _StubSigner:
    """A signer that reaches nothing. The real ones are tested separately."""

    def authorization(self) -> str:
        return "Bearer synthetic-assertion"

    def headers(self) -> dict[str, str]:
        return {"Authorization": self.authorization()}

    def describe(self) -> dict[str, Any]:
        return {"mode": "stub", "service_account": AGENT_SA}


class _Recorder:
    """One httpx transport that answers from a script and records every call."""

    def __init__(self, routes: dict[tuple[str, str], Any]) -> None:
        self.routes = routes
        self.calls: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        self.calls.append(
            {
                "method": request.method,
                "path": request.url.path,
                "query": dict(request.url.params),
                "headers": dict(request.headers),
                "body": json.loads(request.content) if request.content else None,
            }
        )
        handler = self.routes.get(key)
        if handler is None:
            return httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "no"}})
        if callable(handler):
            return handler(request)
        return handler

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def _batch_body(**overrides: Any) -> dict[str, Any]:
    body = {
        "batch_id": BATCH_ID,
        "status": "claimed",
        "version": 3,
        "item_count": 2,
        "item_set_digest": "d" * 64,
        "lease": {
            "holder": AGENT_SA,
            "acquired_at": "2026-08-05T12:00:00Z",
            "expires_at": "2026-08-05T12:15:00Z",
            "last_heartbeat_at": "2026-08-05T12:00:00Z",
            "continuous_since": "2026-08-05T12:00:00Z",
        },
        "changed_files": [],
        "prompt_template_version": "1",
    }
    body.update(overrides)
    return body


@pytest.fixture
def store(tmp_path: Path) -> cli.LeaseStore:
    """A lease store shaped exactly like a linked worktree's.

    ``.git`` in a linked worktree is a *file* pointing at
    ``<common>/worktrees/<name>``, so the git directory is nested and the lease
    directory sits inside it. That is the layout this project mandates, and the
    one the resolver has to get right.
    """
    git_dir = tmp_path / "common" / ".git" / "worktrees" / "console"
    git_dir.mkdir(parents=True)
    lease_dir = git_dir / cli.LEASE_DIRECTORY_NAME
    return cli.LeaseStore(git_dir=git_dir, lease_dir=lease_dir)


def _run(
    argv: list[str],
    *,
    recorder: Optional[_Recorder] = None,
    store: Optional[cli.LeaseStore] = None,
    spawner: Optional[Any] = None,
    signer: Optional[Any] = None,
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = cli.run(
        argv,
        transport=recorder.transport if recorder is not None else None,
        signer=signer if signer is not None else _StubSigner(),
        store=store,
        spawner=spawner or (lambda argv, env: 4242),
        stdout=out,
        stderr=err,
    )
    return code, out.getvalue(), err.getvalue()


# =====================================================================
# The command surface
# =====================================================================


class TestCommandSurface:

    def test_every_documented_command_exists(self):
        """The exact surface Stage 8 names, plus nothing an operator can misuse."""
        assert set(cli.PUBLIC_COMMANDS) == {
            "auth doctor",
            "batch show",
            "batch claim",
            "batch heartbeat",
            "batch lease-start",
            "batch lease-status",
            "batch lease-stop",
            "batch materialize",
            "batch record-plan",
            "batch record-progress",
            "batch submit",
            "batch block",
            "batch release",
            "reviews below-rating",
        }

    def test_the_help_renders_for_every_subcommand(self, capsys):
        parser = cli.build_parser()
        for name in cli.PUBLIC_COMMANDS:
            group, command = name.split(" ", 1)
            with pytest.raises(SystemExit) as exit_info:
                parser.parse_args([group, command, "--help"])
            assert exit_info.value.code == 0
            assert capsys.readouterr().out

    def test_no_command_accepts_a_firestore_target(self):
        """The CLI has no database surface, and cannot grow one by accident."""
        forbidden = re.compile(
            r"--(gcp-)?(project|database|collection|document|firestore|path|namespace|index)\b"
        )
        for name in cli.PUBLIC_COMMANDS + ("batch lease-keeper",):
            group, command = name.split(" ", 1)
            parser = cli.build_parser()
            with pytest.raises(SystemExit):
                parser.parse_args([group, command, "--help"])
            # The option strings themselves, read straight off the subparser.
            actions = _subparser(group, command)._actions
            for action in actions:
                for option in action.option_strings:
                    assert not forbidden.match(option), (name, option)

    def test_the_module_never_imports_firestore_or_the_repository(self):
        """Scanned as code, not as prose: the docstring names what it avoids.

        ``_code_only`` drops docstrings, comments, and double-backtick spans, so
        this asserts on what the module actually does rather than on what it
        explains about itself.
        """
        code = _code_only(Path(cli.__file__).read_text(encoding="utf-8"))
        for banned in (
            "firestore",
            "Firestore",
            "TicketReviewRepository",
            "google.cloud",
        ):
            assert banned not in code, banned
        # Nothing from the server's own persistence layer is imported at all.
        for line in code.splitlines():
            if line.startswith(("import ", "from ")):
                assert "data_pipeline" not in line, line
                assert "api." not in line, line

    @pytest.mark.parametrize(
        "flag", ["--console-url", "--environment", "--repo-id"]
    )
    def test_the_explicit_flags_have_no_default(self, flag):
        """Nothing is inferred from ambient state: every target is stated."""
        parser = cli.build_parser()
        argv = ["batch", "show", "--batch-id", BATCH_ID] + [
            token for token in _base_args() if not token.startswith(flag)
        ]
        # Drop the flag *and* its value.
        cleaned: list[str] = []
        skip = False
        for token in argv:
            if skip:
                skip = False
                continue
            if token == flag:
                skip = True
                continue
            cleaned.append(token)
        with pytest.raises(SystemExit):
            parser.parse_args(cleaned)

    def test_the_duplicated_server_constants_still_agree(self):
        from api.ticket_review_models import (
            LEASE_TOKEN_HEADER,
            REMEDIATION_HEARTBEAT_S,
            REMEDIATION_LEASE_S,
            REMEDIATION_MAX_CONTINUOUS_LEASE_S,
        )
        from api.ticket_review_routes import API_PREFIX, BATCHES_PATH
        from api.tickets_csrf import CURSOR_HEADER, IDEMPOTENCY_HEADER

        assert cli.API_PREFIX == API_PREFIX
        assert cli.BATCHES_PATH == BATCHES_PATH
        assert cli.LEASE_TOKEN_HEADER == LEASE_TOKEN_HEADER
        assert cli.IDEMPOTENCY_HEADER == IDEMPOTENCY_HEADER
        assert cli.CURSOR_HEADER == CURSOR_HEADER
        assert cli.REMEDIATION_LEASE_S == REMEDIATION_LEASE_S
        assert cli.REMEDIATION_HEARTBEAT_S == REMEDIATION_HEARTBEAT_S
        assert cli.REMEDIATION_MAX_CONTINUOUS_LEASE_S == REMEDIATION_MAX_CONTINUOUS_LEASE_S

    def test_the_idempotency_keys_it_mints_satisfy_the_server_pattern(self):
        from api.tickets_csrf import validate_idempotency_key

        for prefix in ("claim", "beat", "mat", "patch", "release"):
            key = cli.new_idempotency_key(prefix)
            assert validate_idempotency_key(key) == key


class TestReviewsBelowRating:
    def test_help_says_unrated_reviews_are_not_low_rated(self):
        help_text = " ".join(
            _subparser("reviews", "below-rating").format_help().split()
        )
        assert "Unrated reviews are excluded" in help_text
        assert "--rating-below" in help_text
        assert "--status" in help_text

    def test_four_rating_queries_are_deduplicated_filtered_and_stably_sorted(
        self, store
    ):
        expected_fields = {
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
        }

        def review(review_id: str, rating: int | None, updated_at: str) -> dict[str, Any]:
            return {
                "review_id": review_id,
                "devrev_display_id": f"TICKET-{review_id[0]}",
                "rating": rating,
                "observation_type": "wrong_route",
                "severity": "medium",
                "remediation_target": "code",
                "status": "reviewed",
                "comments": "Synthetic observation",
                "expected_behavior": "Synthetic expected behavior",
                "modified_surfaces": ["rag_code"],
                "remediation_summary": "Synthetic fix",
                "ticket_job_ids": [f"job-{review_id[0]}"],
                "request_id_hashes": [review_id],
                "source_article_ids": [f"article-{review_id[0]}"],
                "updated_at": updated_at,
            }

        def response(request: httpx.Request) -> httpx.Response:
            rating = int(request.url.params["facet_value"])
            pages = {
                1: [review(REVIEW_A, 1, "2026-08-12T12:00:00Z")],
                2: [
                    review(REVIEW_B, 2, "2026-08-12T14:00:00Z"),
                    review(REVIEW_A, 1, "2026-08-12T12:00:00Z"),
                ],
                3: [
                    review(REVIEW_C, 3, "2026-08-12T13:00:00Z"),
                    review("e" * 64, 5, "2026-08-12T15:00:00Z"),
                    review("f" * 64, None, "2026-08-12T16:00:00Z"),
                ],
                4: [review("d" * 64, 4, "2026-08-12T13:00:00Z")],
            }
            return httpx.Response(
                200,
                json={"items": pages[rating], "next_cursor": None, "page_size": 100},
            )

        recorder = _Recorder(
            {("GET", f"{cli.API_PREFIX}/reviews"): response}
        )
        code, out, _ = _run(
            [
                "reviews",
                "below-rating",
                "--console-url",
                DEPLOYED_CONSOLE,
                "--environment",
                "staging",
                "--rating-below",
                "5",
                "--status",
                "reviewed",
                "--json",
            ],
            recorder=recorder,
            store=store,
        )

        assert code == cli.EXIT_OK
        assert len(recorder.calls) == 4
        assert [call["query"]["facet_value"] for call in recorder.calls] == [
            "1",
            "2",
            "3",
            "4",
        ]
        assert all(call["query"]["facet"] == "rating" for call in recorder.calls)
        assert all(call["query"]["statuses"] == "reviewed" for call in recorder.calls)
        payload = json.loads(out)
        assert [item["review_id"] for item in payload] == [
            REVIEW_B,
            REVIEW_C,
            "d" * 64,
            REVIEW_A,
        ]
        assert all(set(item) == expected_fields for item in payload)
        assert all(item["rating"] in {1, 2, 3, 4} for item in payload)


_DOCSTRING = re.compile(r'("""|\'\'\')(?:.|\n)*?\1')
_BACKTICKED = re.compile(r"``[^`]*``")
_COMMENT = re.compile(r"(?m)^\s*#.*$")


def _code_only(source: str) -> str:
    """Strip docstrings, comments, and ``literal`` spans from Python source.

    Several assertions here ban a name from the CLI's implementation while the
    CLI's own documentation deliberately *names* it — "there is no ``pkill``
    here", "this is not a Firestore client". Scanning raw text would make honest
    documentation fail the test, which teaches exactly the wrong lesson.
    """
    stripped = _DOCSTRING.sub('""', source)
    stripped = _BACKTICKED.sub("``", stripped)
    return _COMMENT.sub("", stripped)


def _subparser(group: str, command: str):
    parser = cli.build_parser()
    groups = [
        action
        for action in parser._actions
        if isinstance(action, __import__("argparse")._SubParsersAction)
    ][0]
    inner = groups.choices[group]
    sub = [
        action
        for action in inner._actions
        if isinstance(action, __import__("argparse")._SubParsersAction)
    ][0]
    return sub.choices[command]


# =====================================================================
# Validation and unsafe environments
# =====================================================================


class TestValidation:

    @pytest.mark.parametrize(
        "url",
        [
            f"{DEPLOYED_CONSOLE}/api",
            f"{DEPLOYED_CONSOLE}?a=b",
            f"{DEPLOYED_CONSOLE}#frag",
            "https://user:pw@console.example.invalid",
            "ftp://console.example.invalid",
            "",
        ],
    )
    def test_a_console_url_that_is_not_an_exact_origin_is_refused(self, url):
        with pytest.raises(cli.CliError):
            cli.validated_console_url(url, environment="staging")

    def test_a_loopback_console_is_refused_outside_local(self):
        with pytest.raises(cli.UnsafeEnvironmentError):
            cli.validated_console_url(LOOPBACK_CONSOLE, environment="staging")
        assert (
            cli.validated_console_url(LOOPBACK_CONSOLE, environment="local")
            == LOOPBACK_CONSOLE
        )

    def test_plain_http_is_refused_for_a_remote_host(self):
        with pytest.raises(cli.UnsafeEnvironmentError):
            cli.validated_console_url(
                "http://console.example.invalid", environment="local"
            )

    def test_https_on_loopback_is_refused(self):
        with pytest.raises(cli.ValidationError):
            cli.validated_console_url("https://127.0.0.1:8010", environment="local")

    @pytest.mark.parametrize("value", ["", "prod", "Local ", "development"])
    def test_an_unknown_environment_is_refused(self, value):
        if value.strip().lower() in cli.VALID_ENVIRONMENTS:
            pytest.skip("normalized value is accepted")
        with pytest.raises(cli.ValidationError):
            cli.validated_environment(value)

    @pytest.mark.parametrize(
        "value", ["", "not-a-batch", "a" * 31, "A" * 32, "../../etc/passwd", "0" * 32 + "0"]
    )
    def test_a_batch_id_that_is_not_server_minted_is_refused(self, value):
        with pytest.raises(cli.ValidationError):
            cli.validated_batch_id(value)

    def test_both_batch_id_spellings_are_accepted(self):
        assert cli.validated_batch_id(BATCH_ID) == BATCH_ID
        dashed = "00000000-0000-4000-8000-000000000001"
        assert cli.validated_batch_id(dashed) == dashed

    @pytest.mark.parametrize(
        "value", ["", "a b", "../x", "/x", "x..y", "x~1", "x/", "x."]
    )
    def test_a_malformed_git_ref_is_refused(self, value):
        with pytest.raises(cli.ValidationError):
            cli.validated_ref(value, label="--branch")

    @pytest.mark.parametrize("value", ["/etc/passwd", "../secrets", "a//b", "x/", ""])
    def test_a_changed_file_outside_the_repository_is_refused(self, value):
        with pytest.raises(cli.ValidationError):
            cli.validated_repo_path(value)

    @pytest.mark.parametrize("value", ["", "abc123", "C" * 40 + "x", "c" * 7])
    def test_an_abbreviated_commit_is_refused(self, value):
        with pytest.raises(cli.ValidationError):
            cli.validated_commit_sha(value)

    def test_a_review_outcome_needs_a_review_id_and_an_outcome(self):
        assert cli._parsed_outcomes([f"{REVIEW_A}=fixed"]) == [
            {"review_id": REVIEW_A, "outcome": "fixed"}
        ]
        for bad in ([REVIEW_A], [f"{REVIEW_A}="], ["not-an-id=fixed"]):
            with pytest.raises(cli.ValidationError):
                cli._parsed_outcomes(bad)

    def test_a_repeated_review_outcome_is_refused(self):
        with pytest.raises(cli.ValidationError):
            cli._parsed_outcomes([f"{REVIEW_A}=fixed", f"{REVIEW_A}=blocked"])

    def test_test_records_must_pair_a_label_with_an_exit_code(self):
        with pytest.raises(cli.ValidationError):
            cli._parsed_tests(["pytest -q"], [])
        records = cli._parsed_tests(["pytest -q", "ruff check"], [0, 1])
        assert [record["exit_code"] for record in records] == [0, 1]
        assert all(len(record["output_sha256"]) == 64 for record in records)


# =====================================================================
# Exit codes
# =====================================================================


class TestExitCodes:

    @pytest.mark.parametrize(
        "status,code,expected",
        [
            (403, "FORBIDDEN", cli.EXIT_AUTH),
            (401, "UNAUTHENTICATED", cli.EXIT_AUTH),
            (404, "NOT_FOUND", cli.EXIT_NOT_FOUND),
            (409, "BATCH_CONFLICT", cli.EXIT_CONFLICT),
            (412, "BATCH_VERSION_CONFLICT", cli.EXIT_CONFLICT),
            (409, "BATCH_LEASE_LOST", cli.EXIT_LEASE_LOST),
            (422, "VALIDATION_FAILED", cli.EXIT_VALIDATION),
            (500, "INTERNAL_ERROR", cli.EXIT_UPSTREAM),
            (503, "REMEDIATION_DISABLED", cli.EXIT_UPSTREAM),
        ],
    )
    def test_each_server_failure_maps_to_its_documented_exit_code(
        self, status, code, expected, store
    ):
        recorder = _Recorder(
            {
                ("GET", cli.ConsoleClient.batch_path(
                    cli.ConsoleClient(console_url="", signer=_StubSigner()), BATCH_ID
                )): httpx.Response(status, json={"error": {"code": code, "message": "no"}})
            }
        )
        result, _, err = _run(
            ["batch", "show", *_base_args(), "--batch-id", BATCH_ID],
            recorder=recorder,
            store=store,
        )
        assert result == expected, err

    def test_a_validation_failure_never_reaches_the_network(self, store):
        recorder = _Recorder({})
        code, _, err = _run(
            ["batch", "show", *_base_args(), "--batch-id", "not-a-batch"],
            recorder=recorder,
            store=store,
        )
        assert code == cli.EXIT_VALIDATION
        assert recorder.calls == []
        assert "batch id" in err

    def test_an_unsafe_environment_is_its_own_exit_code(self, store):
        code, _, err = _run(
            [
                "batch",
                "show",
                "--console-url",
                LOOPBACK_CONSOLE,
                "--environment",
                "production",
                "--repo-id",
                REPO_ID,
                "--batch-id",
                BATCH_ID,
                "--skip-repo-check",
            ],
            store=store,
        )
        assert code == cli.EXIT_UNSAFE
        assert "loopback" in err

    def test_a_redirect_is_refused_rather_than_followed(self, store):
        path = f"{cli.API_PREFIX}{cli.BATCHES_PATH}/{BATCH_ID}"
        recorder = _Recorder(
            {("GET", path): httpx.Response(302, headers={"Location": "https://elsewhere.invalid"})}
        )
        code, _, err = _run(
            ["batch", "show", *_base_args(), "--batch-id", BATCH_ID],
            recorder=recorder,
            store=store,
        )
        assert code == cli.EXIT_UNSAFE
        assert "redirect" in err

    def test_a_transport_failure_is_an_upstream_failure(self, store):
        def explode(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        path = f"{cli.API_PREFIX}{cli.BATCHES_PATH}/{BATCH_ID}"
        recorder = _Recorder({("GET", path): explode})
        code, _, err = _run(
            ["batch", "show", *_base_args(), "--batch-id", BATCH_ID],
            recorder=recorder,
            store=store,
        )
        assert code == cli.EXIT_UPSTREAM
        assert "could not be reached" in err


# =====================================================================
# Redaction
# =====================================================================


class TestRedaction:

    @pytest.mark.parametrize(
        "text",
        [
            '{"lease_token": "synthetic-lease-token-value"}',
            "Authorization: Bearer synthetic-assertion",
            "lease_token=synthetic-lease-token-value",
            'assertion: "ey.synthetic.jwt"',
        ],
    )
    def test_a_credential_never_survives_an_error_message(self, text):
        redacted = cli.redact(text)
        for secret in ("synthetic-lease-token-value", "synthetic-assertion", "ey.synthetic.jwt"):
            assert secret not in redacted, redacted

    def test_a_server_error_body_is_redacted_before_printing(self, store):
        path = f"{cli.API_PREFIX}{cli.BATCHES_PATH}/{BATCH_ID}"
        recorder = _Recorder(
            {
                ("GET", path): httpx.Response(
                    409,
                    json={
                        "error": {
                            "code": "BATCH_CONFLICT",
                            "message": f'rejected lease_token="{LEASE_TOKEN}"',
                        }
                    },
                )
            }
        )
        code, _, err = _run(
            ["batch", "show", *_base_args(), "--batch-id", BATCH_ID],
            recorder=recorder,
            store=store,
        )
        assert code == cli.EXIT_CONFLICT
        assert LEASE_TOKEN not in err


# =====================================================================
# The lease store
# =====================================================================


class TestLeaseStore:

    def test_the_lease_path_comes_from_git_and_stays_inside_the_git_directory(
        self, tmp_path
    ):
        git_dir = tmp_path / "common" / ".git" / "worktrees" / "console"
        git_dir.mkdir(parents=True)

        def runner(args):
            if args[-1] == "--git-dir":
                return str(git_dir)
            return str(git_dir / cli.LEASE_DIRECTORY_NAME)

        resolved = cli.LeaseStore.resolve(runner=runner)
        assert resolved.git_dir == git_dir.resolve()
        assert resolved.lease_dir.parent == git_dir.resolve()

    def test_a_lease_path_outside_the_git_directory_is_refused(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        escape = tmp_path / "elsewhere"

        def runner(args):
            return str(git_dir) if args[-1] == "--git-dir" else str(escape)

        with pytest.raises(cli.UnsafeEnvironmentError):
            cli.LeaseStore.resolve(runner=runner)

    def test_the_git_directory_itself_is_not_the_lease_directory(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        def runner(args):
            return str(git_dir)

        with pytest.raises(cli.UnsafeEnvironmentError):
            cli.LeaseStore.resolve(runner=runner)

    def test_the_directory_is_0700_and_the_token_file_is_0600(self, store):
        store.save_token(BATCH_ID, LEASE_TOKEN, expires_at="2026-08-05T12:15:00Z")
        assert stat.S_IMODE(store.lease_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(store.token_path(BATCH_ID).stat().st_mode) == 0o600

    def test_a_pre_existing_loose_directory_is_tightened(self, store):
        store.lease_dir.mkdir(mode=0o755, parents=True)
        store.ensure()
        assert stat.S_IMODE(store.lease_dir.stat().st_mode) == 0o700

    def test_a_world_readable_token_file_is_refused_rather_than_used(self, store):
        store.save_token(BATCH_ID, LEASE_TOKEN, expires_at="")
        os.chmod(store.token_path(BATCH_ID), 0o644)
        with pytest.raises(cli.UnsafeEnvironmentError):
            store.load_token(BATCH_ID)

    def test_only_this_batch_is_forgotten(self, store):
        store.save_token(BATCH_ID, LEASE_TOKEN, expires_at="")
        store.save_token(OTHER_BATCH_ID, "another", expires_at="")
        store.save_keeper(BATCH_ID, {"pid": 1234, "nonce": "n", "started_at": 0})
        removed = store.forget(BATCH_ID)
        assert sorted(removed) == sorted(
            [f"{BATCH_ID}.json", f"{BATCH_ID}.keeper.json"]
        )
        assert store.load_token(OTHER_BATCH_ID) == "another"

    def test_a_caller_supplied_lease_path_has_nowhere_to_enter(self):
        """There is no option, and no keyword, that names a lease path."""
        source = Path(cli.__file__).read_text(encoding="utf-8")
        assert "--lease-dir" not in source
        assert "--lease-path" not in source
        parser = cli.build_parser()
        rendered = parser.format_help()
        assert "lease-dir" not in rendered and "lease-path" not in rendered

    def test_the_write_is_atomic_and_leaves_no_temporary_behind(self, store):
        store.save_token(BATCH_ID, LEASE_TOKEN, expires_at="")
        store.save_token(BATCH_ID, "second-value", expires_at="")
        assert store.load_token(BATCH_ID) == "second-value"
        assert [path.name for path in store.lease_dir.iterdir()] == [f"{BATCH_ID}.json"]


# =====================================================================
# Claim, and the token's lifetime on disk
# =====================================================================


def _claim_recorder(**batch_overrides: Any) -> _Recorder:
    path = f"{cli.API_PREFIX}{cli.BATCHES_PATH}/{BATCH_ID}"
    return _Recorder(
        {
            ("POST", f"{path}/claim"): httpx.Response(
                200,
                json={
                    "batch": _batch_body(**batch_overrides),
                    "lease_token": "**********",
                    "lease_expires_at": "2026-08-05T12:15:00Z",
                    "heartbeat_interval_s": cli.REMEDIATION_HEARTBEAT_S,
                    "max_continuous_lease_s": cli.REMEDIATION_MAX_CONTINUOUS_LEASE_S,
                },
                headers={cli.LEASE_TOKEN_HEADER: LEASE_TOKEN},
            ),
            ("GET", path): httpx.Response(200, json=_batch_body(**batch_overrides)),
        }
    )


class TestClaim:

    def test_a_claim_is_dry_run_until_apply(self, store):
        recorder = _claim_recorder()
        code, _, err = _run(
            ["batch", "claim", *_base_args(), "--batch-id", BATCH_ID],
            recorder=recorder,
            store=store,
        )
        assert code == cli.EXIT_OK
        assert recorder.calls == []
        assert "dry-run" in err
        assert store.load_token(BATCH_ID) is None

    def test_an_applied_claim_stores_the_token_without_printing_it(self, store):
        recorder = _claim_recorder()
        code, out, err = _run(
            ["batch", "claim", *_base_args(), "--batch-id", BATCH_ID, "--apply"],
            recorder=recorder,
            store=store,
        )
        assert code == cli.EXIT_OK
        assert store.load_token(BATCH_ID) == LEASE_TOKEN
        assert LEASE_TOKEN not in out
        assert LEASE_TOKEN not in err
        # The path is printed so an operator can find and delete it.
        assert str(store.token_path(BATCH_ID)) in err

    def test_the_claim_carries_an_idempotency_key_and_json_content_type(self, store):
        recorder = _claim_recorder()
        _run(
            ["batch", "claim", *_base_args(), "--batch-id", BATCH_ID, "--apply"],
            recorder=recorder,
            store=store,
        )
        headers = recorder.calls[0]["headers"]
        assert headers["content-type"] == "application/json"
        from api.tickets_csrf import validate_idempotency_key

        assert validate_idempotency_key(headers[cli.IDEMPOTENCY_HEADER.lower()])

    def test_a_claim_without_a_token_header_is_an_upstream_failure(self, store):
        path = f"{cli.API_PREFIX}{cli.BATCHES_PATH}/{BATCH_ID}"
        recorder = _Recorder(
            {("POST", f"{path}/claim"): httpx.Response(200, json={"batch": _batch_body()})}
        )
        code, _, err = _run(
            ["batch", "claim", *_base_args(), "--batch-id", BATCH_ID, "--apply"],
            recorder=recorder,
            store=store,
        )
        assert code == cli.EXIT_UPSTREAM
        assert "lease token" in err


# =====================================================================
# The lease keeper
# =====================================================================


class _FakeClient:
    """Answers ``show`` and ``heartbeat`` from a script, and counts beats."""

    def __init__(self, statuses: list[str], *, heartbeat_error: Optional[Exception] = None):
        self.statuses = statuses
        self.heartbeat_error = heartbeat_error
        self.beats = 0
        self.shows = 0

    def show(self, batch_id: str) -> dict[str, Any]:
        index = min(self.shows, len(self.statuses) - 1)
        self.shows += 1
        return {"batch_id": batch_id, "status": self.statuses[index], "version": 3}

    def heartbeat(self, batch_id: str, *, version: int, token: str) -> dict[str, Any]:
        if self.heartbeat_error is not None:
            raise self.heartbeat_error
        self.beats += 1
        return {"batch_id": batch_id, "version": version + 1}


class _Ticker:
    """A monotonic clock that advances only when the keeper sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _keeper(store: cli.LeaseStore, client: Any, ticker: _Ticker) -> cli.LeaseKeeper:
    return cli.LeaseKeeper(
        client=client,
        store=store,
        batch_id=BATCH_ID,
        clock=ticker,
        sleeper=ticker.sleep,
    )


class TestLeaseKeeper:

    def test_it_heartbeats_on_the_canonical_interval(self, store):
        store.save_token(BATCH_ID, LEASE_TOKEN, expires_at="")
        client = _FakeClient(["in_progress"])
        ticker = _Ticker()
        result = _keeper(store, client, ticker).run()
        assert result.reason == "continuous_cap_reached"
        assert set(ticker.sleeps) == {float(cli.REMEDIATION_HEARTBEAT_S)}
        # Two hours of five-minute beats.
        assert client.beats == cli.REMEDIATION_MAX_CONTINUOUS_LEASE_S // cli.REMEDIATION_HEARTBEAT_S

    def test_it_stops_at_the_two_hour_continuous_cap(self, store):
        store.save_token(BATCH_ID, LEASE_TOKEN, expires_at="")
        ticker = _Ticker()
        result = _keeper(store, _FakeClient(["in_progress"]), ticker).run()
        assert result.reason == "continuous_cap_reached"
        assert ticker.now == cli.REMEDIATION_MAX_CONTINUOUS_LEASE_S

    def test_it_stops_the_moment_the_lease_is_lost(self, store):
        store.save_token(BATCH_ID, LEASE_TOKEN, expires_at="")
        client = _FakeClient(["in_progress"], heartbeat_error=cli.LeaseLostError("gone"))
        result = _keeper(store, client, _Ticker()).run()
        assert result.reason == "lease_lost"
        assert client.beats == 0

    def test_a_version_race_is_not_a_reason_to_give_up_the_claim(self, store):
        store.save_token(BATCH_ID, LEASE_TOKEN, expires_at="")
        client = _FakeClient(["in_progress"], heartbeat_error=cli.ConflictError("stale"))
        ticker = _Ticker()
        result = _keeper(store, client, ticker).run()
        # It kept trying to the cap rather than abandoning a live lease.
        assert result.reason == "continuous_cap_reached"
        assert result.beats == 0

    def test_a_transient_upstream_failure_is_survived(self, store):
        store.save_token(BATCH_ID, LEASE_TOKEN, expires_at="")
        client = _FakeClient(["in_progress"], heartbeat_error=cli.UpstreamError("502"))
        result = _keeper(store, client, _Ticker()).run()
        assert result.reason == "continuous_cap_reached"

    @pytest.mark.parametrize(
        "status", ["changes_proposed", "verifying", "completed", "blocked", "cancelled"]
    )
    def test_it_exits_once_the_batch_reaches_a_terminal_state(self, store, status):
        store.save_token(BATCH_ID, LEASE_TOKEN, expires_at="")
        result = _keeper(store, _FakeClient([status]), _Ticker()).run()
        assert result.reason == f"batch_{status}"

    def test_it_exits_when_the_lease_file_is_removed(self, store):
        store.save_token(BATCH_ID, LEASE_TOKEN, expires_at="")
        store.forget(BATCH_ID)
        result = _keeper(store, _FakeClient(["in_progress"]), _Ticker()).run()
        assert result.reason == "lease_file_removed"

    def test_it_exits_on_a_stop_signal(self, store):
        store.save_token(BATCH_ID, LEASE_TOKEN, expires_at="")
        keeper = _keeper(store, _FakeClient(["in_progress"]), _Ticker())
        keeper.request_stop()
        assert keeper.run().reason == "stopped"

    def test_it_never_writes_an_assertion_to_disk(self, store):
        store.save_token(BATCH_ID, LEASE_TOKEN, expires_at="")
        store.save_keeper(BATCH_ID, {"pid": os.getpid(), "nonce": "n", "started_at": 0.0})
        _keeper(store, _FakeClient(["completed"]), _Ticker()).run()
        record = store.load_keeper(BATCH_ID)
        assert set(record) == {"pid", "nonce", "started_at"}


# =====================================================================
# Keeper lifecycle: start, status, stop
# =====================================================================


class TestKeeperLifecycle:

    def test_start_records_a_pid_and_a_nonce_and_spawns_once(self, store):
        store.save_token(BATCH_ID, LEASE_TOKEN, expires_at="")
        spawned: list[list[str]] = []

        def spawner(argv, env):
            spawned.append(list(argv))
            return 9999

        code, out, _ = _run(
            ["batch", "lease-start", *_base_args(), "--batch-id", BATCH_ID, "--json"],
            recorder=_Recorder({}),
            store=store,
            spawner=spawner,
        )
        assert code == cli.EXIT_OK
        assert json.loads(out)["pid"] == 9999
        record = store.load_keeper(BATCH_ID)
        assert record["pid"] == 9999 and record["nonce"]
        assert len(spawned) == 1
        # The child re-enters this same file, with no shell in between.
        assert spawned[0][0] == sys.executable
        assert spawned[0][1] == str(Path(cli.__file__).resolve())
        assert "lease-keeper" in spawned[0]

    def test_start_without_a_stored_lease_is_refused(self, store):
        code, _, err = _run(
            ["batch", "lease-start", *_base_args(), "--batch-id", BATCH_ID],
            recorder=_Recorder({}),
            store=store,
        )
        assert code == cli.EXIT_LEASE_LOST
        assert "claim" in err

    def test_start_is_idempotent_while_a_keeper_is_alive(self, store):
        store.save_token(BATCH_ID, LEASE_TOKEN, expires_at="")
        store.save_keeper(BATCH_ID, {"pid": os.getpid(), "nonce": "n", "started_at": 0.0})
        spawned: list[Any] = []
        code, out, _ = _run(
            ["batch", "lease-start", *_base_args(), "--batch-id", BATCH_ID, "--json"],
            recorder=_Recorder({}),
            store=store,
            spawner=lambda argv, env: spawned.append(argv) or 1,
        )
        assert code == cli.EXIT_OK
        assert json.loads(out)["already_running"] is True
        assert spawned == []

    def test_an_abruptly_dead_keeper_is_reported_stale_not_healthy(self, store):
        """The abrupt-agent-exit case: the record survives, the process does not."""
        store.save_token(BATCH_ID, LEASE_TOKEN, expires_at="")
        store.save_keeper(BATCH_ID, {"pid": _dead_pid(), "nonce": "n", "started_at": 0.0})
        health = cli.keeper_health(store, BATCH_ID)
        assert health["healthy"] is False
        assert health["stale"] is True

    def test_start_replaces_a_stale_record(self, store):
        store.save_token(BATCH_ID, LEASE_TOKEN, expires_at="")
        store.save_keeper(BATCH_ID, {"pid": _dead_pid(), "nonce": "old", "started_at": 0.0})
        code, out, _ = _run(
            ["batch", "lease-start", *_base_args(), "--batch-id", BATCH_ID, "--json"],
            recorder=_Recorder({}),
            store=store,
            spawner=lambda argv, env: 7777,
        )
        assert code == cli.EXIT_OK
        assert json.loads(out)["pid"] == 7777
        assert store.load_keeper(BATCH_ID)["nonce"] != "old"

    def test_lease_status_is_the_gate_before_a_lease_bound_write(self, store):
        code, out, _ = _run(
            ["batch", "lease-status", *_base_args(), "--batch-id", BATCH_ID, "--json"],
            recorder=_Recorder({}),
            store=store,
        )
        assert code == cli.EXIT_LEASE_LOST
        assert json.loads(out)["healthy"] is False

        store.save_token(BATCH_ID, LEASE_TOKEN, expires_at="")
        store.save_keeper(BATCH_ID, {"pid": os.getpid(), "nonce": "n", "started_at": 0.0})
        code, out, _ = _run(
            ["batch", "lease-status", *_base_args(), "--batch-id", BATCH_ID, "--json"],
            recorder=_Recorder({}),
            store=store,
        )
        assert code == cli.EXIT_OK
        assert json.loads(out)["healthy"] is True

    def test_a_missing_token_file_makes_a_live_keeper_unhealthy(self, store):
        store.save_keeper(BATCH_ID, {"pid": os.getpid(), "nonce": "n", "started_at": 0.0})
        health = cli.keeper_health(store, BATCH_ID)
        assert health["healthy"] is False
        assert "token file" in health["reason"]

    def test_stop_validates_the_pid_and_the_nonce_before_signalling(self, store):
        store.save_keeper(BATCH_ID, {"pid": 5555, "nonce": "n", "started_at": 0.0})
        signalled: list[tuple[int, int]] = []
        alive = iter([True, False])
        result = cli.stop_keeper(
            store,
            BATCH_ID,
            killer=lambda pid, sig: signalled.append((pid, sig)),
            waiter=lambda _s: None,
            alive=lambda pid: next(alive, False),
        )
        assert signalled == [(5555, __import__("signal").SIGTERM)]
        assert result["stopped"] is True

    def test_stop_never_signals_when_the_record_has_no_nonce(self, store):
        store.save_keeper(BATCH_ID, {"pid": 5555, "started_at": 0.0})
        signalled: list[Any] = []
        result = cli.stop_keeper(
            store,
            BATCH_ID,
            killer=lambda pid, sig: signalled.append(pid),
            alive=lambda pid: True,
        )
        assert signalled == []
        assert result["stopped"] is False
        assert store.load_keeper(BATCH_ID) is None

    def test_stop_on_an_absent_record_signals_nothing(self, store):
        signalled: list[Any] = []
        result = cli.stop_keeper(
            store, BATCH_ID, killer=lambda pid, sig: signalled.append(pid)
        )
        assert signalled == []
        assert result["stopped"] is False

    def test_stop_waits_a_bounded_interval_and_reports_a_survivor(self, store):
        store.save_keeper(BATCH_ID, {"pid": 5555, "nonce": "n", "started_at": 0.0})
        waits: list[float] = []
        result = cli.stop_keeper(
            store,
            BATCH_ID,
            killer=lambda pid, sig: None,
            waiter=waits.append,
            alive=lambda pid: True,
            attempts=4,
        )
        assert len(waits) == 4
        assert result["stopped"] is False
        # The record survives, so a human can see which PID refused to die.
        assert store.load_keeper(BATCH_ID)["pid"] == 5555

    def test_stop_never_uses_a_process_name_or_a_glob(self):
        """Only `os.kill` on a validated PID. Nothing that matches by name."""
        code = _code_only(Path(cli.__file__).read_text(encoding="utf-8"))
        for banned in ("pkill", "killall", "psutil", "process_iter", "glob(", "iterdir("):
            assert banned not in code, banned

    def test_a_dead_pid_is_cleaned_up_without_a_signal(self, store):
        store.save_token(BATCH_ID, LEASE_TOKEN, expires_at="")
        store.save_keeper(BATCH_ID, {"pid": _dead_pid(), "nonce": "n", "started_at": 0.0})
        signalled: list[Any] = []
        result = cli.stop_keeper(
            store, BATCH_ID, killer=lambda pid, sig: signalled.append(pid)
        )
        assert signalled == []
        assert result["stopped"] is True
        assert store.load_token(BATCH_ID) is None


def _dead_pid() -> int:
    """A PID that has certainly exited: spawn one and reap it."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603
    process.wait()
    return process.pid


# =====================================================================
# Materialization
# =====================================================================


class TestMaterialize:

    def _recorder(self, *, pages: list[dict[str, Any]]) -> _Recorder:
        path = f"{cli.API_PREFIX}{cli.BATCHES_PATH}/{BATCH_ID}"
        sequence = iter(pages)

        def materialize(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=next(sequence))

        return _Recorder(
            {
                ("GET", path): httpx.Response(200, json=_batch_body(status="in_progress")),
                ("POST", f"{path}:materialize"): materialize,
            }
        )

    def _ready(self, store: cli.LeaseStore) -> None:
        store.save_token(BATCH_ID, LEASE_TOKEN, expires_at="")
        store.save_keeper(BATCH_ID, {"pid": os.getpid(), "nonce": "n", "started_at": 0.0})

    def test_materialization_is_impossible_without_a_healthy_lease(self, store):
        recorder = self._recorder(pages=[{"items": [], "next_cursor": None}])
        code, _, err = _run(
            ["batch", "materialize", *_base_args(), "--batch-id", BATCH_ID],
            recorder=recorder,
            store=store,
        )
        assert code == cli.EXIT_LEASE_LOST
        assert "keeper" in err
        assert not any(call["path"].endswith(":materialize") for call in recorder.calls)

    def test_it_pages_until_the_cursor_runs_out(self, store):
        self._ready(store)
        recorder = self._recorder(
            pages=[
                {
                    "items": [{"review_id": REVIEW_A, "devrev_display_id": "TICKET-000001"}],
                    "next_cursor": "cursor-2",
                    "drifted_review_ids": [],
                    "warnings": [],
                },
                {
                    "items": [{"review_id": REVIEW_B, "devrev_display_id": "TICKET-000002"}],
                    "next_cursor": None,
                    "drifted_review_ids": [REVIEW_B],
                    "warnings": ["1 frozen review(s) drifted"],
                },
            ]
        )
        code, out, _ = _run(
            ["batch", "materialize", *_base_args(), "--batch-id", BATCH_ID, "--json"],
            recorder=recorder,
            store=store,
        )
        assert code == cli.EXIT_OK
        payload = json.loads(out)
        assert [item["review_id"] for item in payload["items"]] == [REVIEW_A, REVIEW_B]
        assert payload["drifted_review_ids"] == [REVIEW_B]
        # The second request carried the cursor in the header, never in the URL.
        second = [call for call in recorder.calls if ":materialize" in call["path"]][1]
        assert second["headers"][cli.CURSOR_HEADER.lower()] == "cursor-2"

    def test_conversation_is_off_by_default_and_warns_when_asked_for(self, store):
        self._ready(store)
        recorder = self._recorder(
            pages=[{"items": [], "next_cursor": None, "warnings": []}]
        )
        _run(
            ["batch", "materialize", *_base_args(), "--batch-id", BATCH_ID],
            recorder=recorder,
            store=store,
        )
        sent = [call for call in recorder.calls if ":materialize" in call["path"]][0]
        assert sent["body"]["include_conversation"] is False

        self._ready(store)
        recorder = self._recorder(
            pages=[{"items": [], "next_cursor": None, "warnings": []}]
        )
        code, _, err = _run(
            [
                "batch",
                "materialize",
                *_base_args(),
                "--batch-id",
                BATCH_ID,
                "--include-conversation",
            ],
            recorder=recorder,
            store=store,
        )
        assert code == cli.EXIT_OK
        sent = [call for call in recorder.calls if ":materialize" in call["path"]][0]
        assert sent["body"]["include_conversation"] is True
        assert "WARNING" in err
        for phrase in ("chat", "commit message", "shared channel"):
            assert phrase in err

    def test_the_default_human_output_carries_no_ticket_body(self, store):
        self._ready(store)
        secret = "A participant sentence nobody should paste into a terminal."
        recorder = self._recorder(
            pages=[
                {
                    "items": [
                        {
                            "review_id": REVIEW_A,
                            "devrev_display_id": "TICKET-000001",
                            "observation_type": "retrieval_miss",
                            "severity": "high",
                            "remediation_target": "kb",
                            "comments_excerpt": secret,
                        }
                    ],
                    "next_cursor": None,
                    "warnings": [],
                }
            ]
        )
        code, out, err = _run(
            ["batch", "materialize", *_base_args(), "--batch-id", BATCH_ID],
            recorder=recorder,
            store=store,
        )
        assert code == cli.EXIT_OK
        assert secret not in out
        assert secret not in err
        # The identifiers a fixer needs are still there.
        assert "TICKET-000001" in err and "retrieval_miss" in err


# =====================================================================
# Recording and submission
# =====================================================================


class TestRecordAndSubmit:

    def _recorder(self, *, status: str = "in_progress") -> _Recorder:
        path = f"{cli.API_PREFIX}{cli.BATCHES_PATH}/{BATCH_ID}"
        return _Recorder(
            {
                ("GET", path): httpx.Response(200, json=_batch_body(status=status)),
                ("PATCH", path): httpx.Response(
                    200, json=_batch_body(status="changes_proposed", version=4)
                ),
            }
        )

    def _ready(self, store: cli.LeaseStore) -> None:
        store.save_token(BATCH_ID, LEASE_TOKEN, expires_at="")
        store.save_keeper(BATCH_ID, {"pid": os.getpid(), "nonce": "n", "started_at": 0.0})

    def _submit_args(self, *extra: str) -> list[str]:
        return [
            "batch",
            "submit",
            *_base_args(),
            "--batch-id",
            BATCH_ID,
            "--branch",
            "fix/synthetic",
            "--commit-sha",
            COMMIT,
            "--changed-file",
            "kb-rag-system/api/x.py",
            "--test-command",
            "pytest -q",
            "--test-exit-code",
            "0",
            "--summary",
            "widened the filter",
            "--review-outcome",
            f"{REVIEW_A}=fixed",
            *extra,
        ]

    def test_record_plan_transitions_from_claimed_and_needs_apply(self, store):
        self._ready(store)
        recorder = self._recorder(status="claimed")
        code, _, err = _run(
            [
                "batch",
                "record-plan",
                *_base_args(),
                "--batch-id",
                BATCH_ID,
                "--plan-artifact",
                "docs/plans/x.md",
            ],
            recorder=recorder,
            store=store,
        )
        assert code == cli.EXIT_OK
        assert "dry-run" in err
        assert not any(call["method"] == "PATCH" for call in recorder.calls)

        self._ready(store)
        recorder = self._recorder(status="claimed")
        code, _, _ = _run(
            [
                "batch",
                "record-plan",
                *_base_args(),
                "--batch-id",
                BATCH_ID,
                "--plan-artifact",
                "docs/plans/x.md",
                "--apply",
            ],
            recorder=recorder,
            store=store,
        )
        assert code == cli.EXIT_OK
        patch = [call for call in recorder.calls if call["method"] == "PATCH"][0]
        assert patch["body"]["transition"] == "planning"
        assert patch["body"]["plan_artifact"] == "docs/plans/x.md"

    def test_a_plan_path_outside_the_repository_is_refused(self, store):
        self._ready(store)
        recorder = self._recorder(status="claimed")
        code, _, err = _run(
            [
                "batch",
                "record-plan",
                *_base_args(),
                "--batch-id",
                BATCH_ID,
                "--plan-artifact",
                "/etc/passwd",
                "--apply",
            ],
            recorder=recorder,
            store=store,
        )
        assert code == cli.EXIT_VALIDATION
        assert not any(call["method"] == "PATCH" for call in recorder.calls)

    def test_submission_needs_a_commit_or_an_explicit_reason(self, store):
        self._ready(store)
        recorder = self._recorder()
        argv = [token for token in self._submit_args("--apply", "--yes")]
        index = argv.index("--commit-sha")
        del argv[index : index + 2]
        code, _, err = _run(argv, recorder=recorder, store=store)
        assert code == cli.EXIT_VALIDATION
        assert "uncommitted-reason" in err

        self._ready(store)
        recorder = self._recorder()
        argv += ["--uncommitted-reason", "the suite is red on main"]
        code, _, _ = _run(argv, recorder=recorder, store=store)
        assert code == cli.EXIT_OK
        patch = [call for call in recorder.calls if call["method"] == "PATCH"][0]
        assert patch["body"]["uncommitted_reason"] == "the suite is red on main"
        assert "commit_sha" not in patch["body"]

    def test_a_submission_removes_the_lease_state(self, store):
        self._ready(store)
        recorder = self._recorder()
        code, _, _ = _run(
            self._submit_args("--apply", "--yes"), recorder=recorder, store=store
        )
        assert code == cli.EXIT_OK
        assert store.load_token(BATCH_ID) is None
        assert store.load_keeper(BATCH_ID) is None

    def test_a_dry_run_submission_keeps_the_lease_and_sends_nothing(self, store):
        self._ready(store)
        recorder = self._recorder()
        code, _, err = _run(self._submit_args("--yes"), recorder=recorder, store=store)
        assert code == cli.EXIT_OK
        assert "dry-run" in err
        assert not any(call["method"] == "PATCH" for call in recorder.calls)
        assert store.load_token(BATCH_ID) == LEASE_TOKEN

    def test_a_multi_review_submission_requires_confirmation(self, store, monkeypatch):
        self._ready(store)
        recorder = self._recorder()
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
        argv = self._submit_args("--apply", "--review-outcome", f"{REVIEW_B}=no_change")
        code, _, err = _run(argv, recorder=recorder, store=store)
        assert code == cli.EXIT_VALIDATION
        assert "--yes" in err
        assert not any(call["method"] == "PATCH" for call in recorder.calls)

    def test_the_same_submission_proceeds_with_yes(self, store):
        self._ready(store)
        recorder = self._recorder()
        argv = self._submit_args(
            "--apply", "--yes", "--review-outcome", f"{REVIEW_B}=no_change"
        )
        code, _, _ = _run(argv, recorder=recorder, store=store)
        assert code == cli.EXIT_OK
        patch = [call for call in recorder.calls if call["method"] == "PATCH"][0]
        assert len(patch["body"]["per_review_outcomes"]) == 2

    def test_a_single_review_submission_needs_no_confirmation(self, store, monkeypatch):
        self._ready(store)
        recorder = self._recorder()
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
        code, _, _ = _run(self._submit_args("--apply"), recorder=recorder, store=store)
        assert code == cli.EXIT_OK

    def test_the_submission_body_carries_the_whole_handoff(self, store):
        self._ready(store)
        recorder = self._recorder()
        _run(self._submit_args("--apply", "--yes"), recorder=recorder, store=store)
        body = [call for call in recorder.calls if call["method"] == "PATCH"][0]["body"]
        assert body["transition"] == "changes_proposed"
        assert body["branch"] == "fix/synthetic"
        assert body["commit_sha"] == COMMIT
        assert body["changed_files"] == ["kb-rag-system/api/x.py"]
        assert body["test_evidence"][0]["command_label"] == "pytest -q"
        assert body["summary"] == "widened the filter"
        assert body["per_review_outcomes"] == [{"review_id": REVIEW_A, "outcome": "fixed"}]
        assert body["lease_token"] == LEASE_TOKEN

    def test_there_is_no_command_that_verifies_or_completes(self):
        """The agent's surface stops at submission, by construction.

        Checked three ways: no command *name* mentions verification, no
        subcommand is registered for one, and the implementation contains no path
        for either human-only endpoint. The parser's own description is allowed to
        say the CLI cannot do these things — that is the documentation, not a
        capability.
        """
        for name in cli.PUBLIC_COMMANDS:
            assert "verif" not in name and "complete" not in name, name
        parser = cli.build_parser()
        groups = [
            action
            for action in parser._actions
            if isinstance(action, __import__("argparse")._SubParsersAction)
        ][0]
        registered = set()
        for inner in groups.choices.values():
            for action in inner._actions:
                if isinstance(action, __import__("argparse")._SubParsersAction):
                    registered.update(action.choices)
        assert not {
            name for name in registered if "verif" in name or "complete" in name
        }, registered
        code = _code_only(Path(cli.__file__).read_text(encoding="utf-8"))
        assert ":complete" not in code
        assert ":start-verification" not in code


# =====================================================================
# Release and block
# =====================================================================


class TestReleaseAndBlock:

    def _recorder(self) -> _Recorder:
        path = f"{cli.API_PREFIX}{cli.BATCHES_PATH}/{BATCH_ID}"
        return _Recorder(
            {
                ("GET", path): httpx.Response(200, json=_batch_body()),
                ("POST", f"{path}:release"): httpx.Response(
                    200, json=_batch_body(status="blocked", version=4, lease=None)
                ),
            }
        )

    def test_block_requires_a_reason_and_sends_blocked(self, store):
        store.save_token(BATCH_ID, LEASE_TOKEN, expires_at="")
        recorder = self._recorder()
        code, _, _ = _run(
            [
                "batch",
                "block",
                *_base_args(),
                "--batch-id",
                BATCH_ID,
                "--reason",
                "needs a product decision",
                "--apply",
            ],
            recorder=recorder,
            store=store,
        )
        assert code == cli.EXIT_OK
        body = [call for call in recorder.calls if ":release" in call["path"]][0]["body"]
        assert body["disposition"] == "blocked"
        assert body["reason"] == "needs a product decision"

    def test_a_blank_block_reason_is_refused(self, store):
        store.save_token(BATCH_ID, LEASE_TOKEN, expires_at="")
        recorder = self._recorder()
        code, _, err = _run(
            [
                "batch",
                "block",
                *_base_args(),
                "--batch-id",
                BATCH_ID,
                "--reason",
                "   ",
                "--apply",
            ],
            recorder=recorder,
            store=store,
        )
        assert code == cli.EXIT_VALIDATION
        assert recorder.calls == []

    def test_release_sends_ready_and_clears_the_lease_state(self, store):
        store.save_token(BATCH_ID, LEASE_TOKEN, expires_at="")
        store.save_keeper(BATCH_ID, {"pid": _dead_pid(), "nonce": "n", "started_at": 0.0})
        recorder = self._recorder()
        code, _, _ = _run(
            ["batch", "release", *_base_args(), "--batch-id", BATCH_ID, "--apply"],
            recorder=recorder,
            store=store,
        )
        assert code == cli.EXIT_OK
        body = [call for call in recorder.calls if ":release" in call["path"]][0]["body"]
        assert body["disposition"] == "ready"
        assert store.load_token(BATCH_ID) is None
        assert store.load_keeper(BATCH_ID) is None

    def test_release_without_a_stored_lease_is_refused(self, store):
        recorder = self._recorder()
        code, _, err = _run(
            ["batch", "release", *_base_args(), "--batch-id", BATCH_ID, "--apply"],
            recorder=recorder,
            store=store,
        )
        assert code == cli.EXIT_LEASE_LOST
        assert recorder.calls == []


# =====================================================================
# Authentication
# =====================================================================


class TestSigners:

    def test_the_deployed_signer_targets_the_exact_origin_plus_the_wildcard(self):
        signer = cli.IamCredentialsSigner(
            console_url=DEPLOYED_CONSOLE,
            environment="staging",
            service_account=AGENT_SA,
        )
        assert signer.audience == f"{DEPLOYED_CONSOLE}{cli.AGENT_AUDIENCE_SUFFIX}"
        claims = signer.claims(now=1_800_000_000)
        assert claims["aud"] == f"{DEPLOYED_CONSOLE}/*"
        assert claims["iss"] == claims["sub"] == AGENT_SA
        assert claims["exp"] - claims["iat"] == cli.JWT_LIFETIME_S

    def test_the_audience_is_never_a_bare_base_url(self):
        signer = cli.IamCredentialsSigner(
            console_url=DEPLOYED_CONSOLE, environment="staging", service_account=AGENT_SA
        )
        assert signer.audience != DEPLOYED_CONSOLE
        assert signer.audience.endswith("/*")

    def test_the_signer_describes_itself_without_a_token(self):
        signer = cli.IamCredentialsSigner(
            console_url=DEPLOYED_CONSOLE, environment="staging", service_account=AGENT_SA
        )
        rendered = json.dumps(signer.describe())
        # The mode names the mechanism ("signjwt"), which is useful; what must
        # never appear is a credential-shaped value.
        for banned in ("Bearer", "signedJwt", "eyJ"):
            assert banned not in rendered, banned
        assert "assertion" not in rendered

    def test_signjwt_is_called_for_exactly_the_configured_account(self):
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["payload"] = json.loads(json.loads(request.content)["payload"])
            return httpx.Response(200, json={"signedJwt": "synthetic.signed.jwt"})

        signer = cli.IamCredentialsSigner(
            console_url=DEPLOYED_CONSOLE,
            environment="staging",
            service_account=AGENT_SA,
            transport=httpx.MockTransport(handler),
            credentials_factory=lambda: (
                type("Creds", (), {"token": "synthetic-adc"})(),
                "project",
            ),
        )
        assert signer.authorization() == "Bearer synthetic.signed.jwt"
        assert seen["url"] == (
            f"https://{cli.IAM_CREDENTIALS_HOST}/v1/projects/-/serviceAccounts/"
            f"{AGENT_SA}:signJwt"
        )
        assert seen["payload"]["aud"] == f"{DEPLOYED_CONSOLE}/*"

    def test_a_refused_signjwt_is_an_auth_failure_with_the_grant_named(self):
        signer = cli.IamCredentialsSigner(
            console_url=DEPLOYED_CONSOLE,
            environment="staging",
            service_account=AGENT_SA,
            transport=httpx.MockTransport(lambda request: httpx.Response(403, json={})),
            credentials_factory=lambda: (
                type("Creds", (), {"token": "synthetic-adc"})(),
                None,
            ),
        )
        with pytest.raises(cli.AuthError) as info:
            signer.authorization()
        assert "serviceAccountTokenCreator" in (info.value.hint or "")
        assert AGENT_SA in (info.value.hint or "")

    def test_missing_adc_is_an_auth_failure_that_names_the_fix(self):
        signer = cli.IamCredentialsSigner(
            console_url=DEPLOYED_CONSOLE,
            environment="staging",
            service_account=AGENT_SA,
            credentials_factory=lambda: (type("Creds", (), {"token": None})(), None),
        )
        with pytest.raises(cli.AuthError) as info:
            signer.authorization()
        assert "application-default login" in (info.value.hint or "")

    def test_fixture_auth_cannot_be_enabled_for_a_non_loopback_url(self):
        with pytest.raises(cli.UnsafeEnvironmentError):
            cli.LocalFixtureSigner(
                console_url=DEPLOYED_CONSOLE,
                environment="local",
                agent_email="agent@example.invalid",
            )

    def test_fixture_auth_cannot_be_enabled_outside_local(self):
        with pytest.raises(cli.UnsafeEnvironmentError):
            cli.LocalFixtureSigner(
                console_url=LOOPBACK_CONSOLE,
                environment="staging",
                agent_email="agent@example.invalid",
            )

    def test_the_signer_chosen_follows_the_environment_and_the_host(self, monkeypatch):
        monkeypatch.delenv(cli.AGENT_SERVICE_ACCOUNT_ENV, raising=False)
        local = cli.build_signer(console_url=LOOPBACK_CONSOLE, environment="local")
        assert isinstance(local, cli.LocalFixtureSigner)

        with pytest.raises(cli.ValidationError):
            cli.build_signer(console_url=DEPLOYED_CONSOLE, environment="staging")

        monkeypatch.setenv(cli.AGENT_SERVICE_ACCOUNT_ENV, AGENT_SA)
        deployed = cli.build_signer(
            console_url=DEPLOYED_CONSOLE, environment="production"
        )
        assert isinstance(deployed, cli.IamCredentialsSigner)
        assert deployed.service_account == AGENT_SA

    def test_a_service_account_that_is_not_one_is_refused(self, monkeypatch):
        monkeypatch.setenv(cli.AGENT_SERVICE_ACCOUNT_ENV, "person@example.invalid")
        with pytest.raises(cli.ValidationError):
            cli.build_signer(console_url=DEPLOYED_CONSOLE, environment="staging")


class TestAuthDoctor:

    def test_the_doctor_reports_each_check_and_prints_no_token(self, store):
        recorder = _Recorder(
            {
                ("GET", f"{cli.API_PREFIX}/session"): httpx.Response(
                    403,
                    json={"error": {"code": "FORBIDDEN", "message": "no session"}},
                    headers={"Date": "Wed, 05 Aug 2026 12:00:00 GMT"},
                )
            }
        )
        code, out, err = _run(
            ["auth", "doctor", *_base_args(), "--json"],
            recorder=recorder,
            store=store,
        )
        rendered = out + err
        assert "Bearer" not in rendered
        assert "synthetic-assertion" not in rendered
        payload = json.loads(out)
        names = {entry["check"] for entry in payload["checks"]}
        assert {"signer", "lease_directory", "console"} <= names

    def test_the_doctor_accepts_the_agents_own_403_on_session(self, store):
        """The agent has no browser session by design; reachability is the check."""
        recorder = _Recorder(
            {
                ("GET", f"{cli.API_PREFIX}/session"): httpx.Response(
                    403, json={"error": {"code": "FORBIDDEN", "message": "no"}}
                )
            }
        )
        _, out, _ = _run(
            ["auth", "doctor", *_base_args(), "--json"],
            recorder=recorder,
            store=store,
        )
        console = [
            entry for entry in json.loads(out)["checks"] if entry["check"] == "console"
        ][0]
        assert console["ok"] is True

    def test_an_unreachable_console_makes_the_doctor_exit_three(self, store):
        def explode(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        recorder = _Recorder({("GET", f"{cli.API_PREFIX}/session"): explode})
        code, _, _ = _run(
            ["auth", "doctor", *_base_args(), "--json"],
            recorder=recorder,
            store=store,
        )
        assert code == cli.EXIT_AUTH


# =====================================================================
# Announcements
# =====================================================================


class TestAnnouncement:

    def test_every_invocation_states_where_it_is_pointed(self, store):
        path = f"{cli.API_PREFIX}{cli.BATCHES_PATH}/{BATCH_ID}"
        recorder = _Recorder({("GET", path): httpx.Response(200, json=_batch_body())})
        _, _, err = _run(
            ["batch", "show", *_base_args(), "--batch-id", BATCH_ID],
            recorder=recorder,
            store=store,
        )
        assert f"console={DEPLOYED_CONSOLE}" in err
        assert "environment=staging" in err
        assert f"repo={REPO_ID}" in err
        assert f"batch={BATCH_ID}" in err
        assert "mode=dry-run" in err

    def test_an_applied_write_says_apply(self, store):
        recorder = _claim_recorder()
        _, _, err = _run(
            ["batch", "claim", *_base_args(), "--batch-id", BATCH_ID, "--apply"],
            recorder=recorder,
            store=store,
        )
        assert "mode=APPLY" in err
