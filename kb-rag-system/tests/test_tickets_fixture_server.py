"""Stage 6 Step 7 — the fixture runner refuses everything it should.

This file is mostly about what the runner will *not* do, because the failure
modes of "start a server in the background and kill it later" are all
destructive:

*   killing a process that merely inherited the recorded pid;
*   killing, or trusting, an unrelated listener that happens to hold the port;
*   matching processes by name or pattern, and hitting a colleague's work;
*   writing a nonce into a directory other users can read;
*   leaving a forgotten fixture running on a reachable interface.

Each of those has a test. The three-way identity check — recorded pid alive,
recorded URL answering, and the fixture endpoint returning the exact nonce this
runner stored — is what the destructive paths are gated on, so most of these
tests are assertions that a signal was *not* sent.
"""

from __future__ import annotations

import os
import socket
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.support import tickets_console_fixture_app as fixture_app
from tests.support import tickets_fixture_server as runner

MODULE_SOURCE = Path(runner.__file__).read_text(encoding="utf-8")


@pytest.fixture
def state_dir(tmp_path) -> Path:
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)
    return directory


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def dead_pid() -> int:
    """A pid that certainly no longer exists: one we just reaped ourselves."""
    finished = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-c", "pass"], check=True, capture_output=True
    )
    del finished
    child = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603 - fixed argv
    child.wait()
    return child.pid


# =====================================================================
# Argument validation
# =====================================================================


class TestHostValidation:

    @pytest.mark.parametrize(
        "host",
        ["0.0.0.0", "::", "localhost", "127.0.0.2", "10.0.0.5", "example.invalid", ""],
    )
    def test_only_the_loopback_literal_is_accepted(self, host):
        with pytest.raises(runner.FixtureServerError):
            runner.validate_host(host)

    def test_the_loopback_literal_is_accepted(self):
        assert runner.validate_host("127.0.0.1") == "127.0.0.1"


class TestPortValidation:

    @pytest.mark.parametrize("port", [0, 1, 80, 443, 1023, 65536, 70000, -1, "abc", None])
    def test_an_unusable_port_is_refused(self, port):
        with pytest.raises(runner.FixtureServerError):
            runner.validate_port(port)

    @pytest.mark.parametrize("port", [1024, 8010, 65535, " 8010 "])
    def test_an_explicit_unprivileged_port_is_accepted(self, port):
        assert runner.validate_port(port) == int(str(port).strip())


class TestDeadlineValidation:

    @pytest.mark.parametrize("seconds", [0, 1, 59, 1801, 86400, -60, "soon", None])
    def test_a_deadline_outside_the_window_is_refused(self, seconds):
        with pytest.raises(runner.FixtureServerError):
            runner.validate_max_seconds(seconds)

    @pytest.mark.parametrize("seconds", [60, 600, 1800])
    def test_a_deadline_inside_the_window_is_accepted(self, seconds):
        assert runner.validate_max_seconds(seconds) == seconds


class TestStateDirectoryValidation:

    def test_a_fresh_private_directory_is_accepted(self, state_dir):
        assert runner.validate_state_dir(state_dir, fresh=True) == state_dir

    def test_a_missing_directory_is_refused_rather_than_created(self, tmp_path):
        absent = tmp_path / "not-there"
        with pytest.raises(runner.FixtureServerError):
            runner.validate_state_dir(absent, fresh=True)
        assert not absent.exists(), "the runner must not create the directory"

    def test_a_file_is_not_a_state_directory(self, tmp_path):
        plain = tmp_path / "plain"
        plain.write_text("", encoding="utf-8")
        with pytest.raises(runner.FixtureServerError):
            runner.validate_state_dir(plain, fresh=True)

    @pytest.mark.parametrize("mode", [0o755, 0o750, 0o700 | stat.S_IRGRP, 0o777])
    def test_a_directory_others_can_read_is_refused(self, state_dir, mode):
        os.chmod(state_dir, mode)
        with pytest.raises(runner.FixtureServerError):
            runner.validate_state_dir(state_dir, fresh=True)

    def test_a_symlinked_directory_is_refused(self, tmp_path, state_dir):
        link = tmp_path / "link"
        link.symlink_to(state_dir, target_is_directory=True)
        with pytest.raises(runner.FixtureServerError):
            runner.validate_state_dir(link, fresh=True)

    @pytest.mark.parametrize("name", runner.STATE_FILES)
    def test_a_directory_holding_previous_state_is_not_fresh(self, state_dir, name):
        runner.write_state_file(state_dir, name, "leftover")
        with pytest.raises(runner.FixtureServerError):
            runner.validate_state_dir(state_dir, fresh=True)
        # The same directory is still acceptable for status and stop.
        assert runner.validate_state_dir(state_dir, fresh=False) == state_dir


class TestPortAvailability:

    def test_an_occupied_port_is_refused(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            with pytest.raises(runner.FixtureServerError):
                runner.assert_port_available("127.0.0.1", port)

    def test_a_free_port_is_accepted(self):
        runner.assert_port_available("127.0.0.1", free_port())


class TestConcurrentStart:

    def test_the_second_claim_on_a_directory_loses(self, state_dir):
        runner.claim_state_dir(state_dir)
        with pytest.raises(runner.FixtureServerError):
            runner.claim_state_dir(state_dir)

    def test_the_claim_is_the_pid_file_itself(self, state_dir):
        runner.claim_state_dir(state_dir)
        claimed = state_dir / "pid"
        assert claimed.is_file()
        assert stat.S_IMODE(claimed.stat().st_mode) == 0o600


# =====================================================================
# State files
# =====================================================================


class TestStateFiles:

    def test_every_written_file_is_private(self, state_dir):
        for name in runner.STATE_FILES:
            path = runner.write_state_file(state_dir, name, "value")
            assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_an_undeclared_name_cannot_be_written(self, state_dir):
        with pytest.raises(runner.FixtureServerError):
            runner.write_state_file(state_dir, "secret", "value")

    def test_an_undeclared_name_cannot_be_read(self, state_dir):
        with pytest.raises(runner.FixtureServerError):
            runner.read_state_file(state_dir, "../../etc/passwd")

    def test_a_group_readable_state_file_is_refused(self, state_dir):
        path = runner.write_state_file(state_dir, "nonce", "a" * 64)
        os.chmod(path, 0o644)
        with pytest.raises(runner.FixtureServerError):
            runner.read_state_file(state_dir, "nonce")

    def test_a_symlinked_state_file_is_refused(self, state_dir, tmp_path):
        target = tmp_path / "elsewhere"
        target.write_text("1234", encoding="utf-8")
        (state_dir / "pid").symlink_to(target)
        with pytest.raises(runner.FixtureServerError):
            runner.read_state_file(state_dir, "pid")

    def test_an_implausibly_large_state_file_is_refused(self, state_dir):
        runner.write_state_file(state_dir, "url", "x" * (runner.MAX_STATE_FILE_BYTES + 10))
        with pytest.raises(runner.FixtureServerError):
            runner.read_state_file(state_dir, "url")

    def test_a_missing_state_file_says_the_directory_is_not_live(self, state_dir):
        with pytest.raises(runner.FixtureServerError, match="no pid state file"):
            runner.read_state_file(state_dir, "pid")


class TestCleanup:

    def test_only_the_declared_files_are_removed(self, state_dir):
        for name in runner.STATE_FILES:
            runner.write_state_file(state_dir, name, "value")
        bystander = state_dir / "someone-elses-notes.txt"
        bystander.write_text("keep me", encoding="utf-8")

        removed = runner.remove_state_files(state_dir)

        assert sorted(removed) == sorted(runner.STATE_FILES)
        assert bystander.is_file(), "cleanup must not touch a file it did not write"
        assert state_dir.is_dir(), "cleanup must not remove the caller's directory"

    def test_cleanup_is_idempotent(self, state_dir):
        assert runner.remove_state_files(state_dir) == []

    def test_no_pattern_or_process_name_is_ever_used(self):
        """A glob or a name match is how this kind of tool kills the wrong thing."""
        for forbidden in ("pkill", "killall", "glob(", ".glob", "iterdir", "rglob", "psutil"):
            assert forbidden not in MODULE_SOURCE, forbidden

    def test_no_shell_is_ever_invoked(self):
        assert "shell=True" not in MODULE_SOURCE
        assert "os.system" not in MODULE_SOURCE


# =====================================================================
# Identity verification
# =====================================================================


class TestVerification:

    def _record(self, directory: Path, *, pid: int, nonce: str, port: int) -> None:
        runner.write_state_file(directory, "pid", str(pid))
        runner.write_state_file(directory, "nonce", nonce)
        runner.write_state_file(directory, "url", f"http://127.0.0.1:{port}")
        runner.write_state_file(directory, "started_at", "2026-08-04T12:00:00+00:00")
        runner.write_state_file(directory, "deadline", "2026-08-04T12:30:00+00:00")

    def test_an_empty_directory_is_not_a_live_fixture(self, state_dir):
        with pytest.raises(runner.FixtureServerError):
            runner.verify(state_dir)

    @pytest.mark.parametrize("raw", ["", "not-a-number", "-5", "0", "1", "12.5"])
    def test_an_implausible_pid_is_refused_before_anything_is_signalled(self, state_dir, raw):
        self._record(state_dir, pid=os.getpid(), nonce="a" * 64, port=free_port())
        runner.write_state_file(state_dir, "pid", raw)
        with pytest.raises(runner.FixtureServerError):
            runner.verify(state_dir)

    @pytest.mark.parametrize("nonce", ["", "short", "z" * 64, "A" * 64, "a" * 63])
    def test_a_malformed_nonce_is_refused(self, state_dir, nonce):
        self._record(state_dir, pid=os.getpid(), nonce="a" * 64, port=free_port())
        runner.write_state_file(state_dir, "nonce", nonce)
        with pytest.raises(runner.FixtureServerError, match="nonce is malformed"):
            runner.verify(state_dir)

    @pytest.mark.parametrize(
        "url",
        [
            "http://0.0.0.0:8010",
            "https://tickets.example",
            "http://192.168.1.9:8010",
            "127.0.0.1:8010",
        ],
    )
    def test_a_non_loopback_recorded_url_is_refused(self, state_dir, url):
        self._record(state_dir, pid=os.getpid(), nonce="a" * 64, port=free_port())
        runner.write_state_file(state_dir, "url", url)
        with pytest.raises(runner.FixtureServerError, match="loopback"):
            runner.verify(state_dir)

    def test_a_url_the_caller_did_not_expect_is_refused(self, state_dir):
        port = free_port()
        self._record(state_dir, pid=os.getpid(), nonce="a" * 64, port=port)
        with pytest.raises(runner.FixtureServerError):
            runner.verify(state_dir, expect_url=f"http://127.0.0.1:{port + 1}")

    def test_a_stale_pid_is_reported_rather_than_signalled(self, state_dir):
        self._record(state_dir, pid=dead_pid(), nonce="a" * 64, port=free_port())
        with pytest.raises(runner.FixtureServerError, match="stale"):
            runner.verify(state_dir)

    def test_a_live_but_unrelated_pid_is_refused(self, state_dir):
        """This is the pid-reuse case, and the nonce check is what catches it.

        The recorded pid exists — it is this test process — but nothing at the
        recorded URL claims to be the fixture, so no signal may be sent.
        """
        self._record(state_dir, pid=os.getpid(), nonce="a" * 64, port=free_port())
        with pytest.raises(runner.FixtureServerError, match="refusing to signal"):
            runner.verify(state_dir)

    def test_stop_refuses_when_verification_fails(self, state_dir):
        self._record(state_dir, pid=os.getpid(), nonce="a" * 64, port=free_port())
        with pytest.raises(runner.FixtureServerError):
            runner.stop(state_dir)
        assert (state_dir / "pid").is_file(), "a refused stop must not clean up"

    def test_a_listener_that_is_not_a_fixture_is_not_trusted(self, state_dir):
        """Something answering the port is not proof of identity."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            self._record(state_dir, pid=os.getpid(), nonce="a" * 64, port=port)
            with pytest.raises(runner.FixtureServerError, match="refusing to signal"):
                runner.verify(state_dir)


class TestProbing:

    def test_an_unreachable_url_is_a_zero_status_not_an_exception(self):
        assert runner.probe(f"http://127.0.0.1:{free_port()}/livez")[0] == 0

    def test_a_non_fixture_endpoint_reports_no_nonce(self):
        assert runner.fixture_nonce_at(f"http://127.0.0.1:{free_port()}") is None

    def test_the_probe_ignores_an_ambient_proxy(self, monkeypatch):
        """A proxy variable would send a loopback probe somewhere else entirely.

        The property is that no handler in the runner's opener proxies anything.
        The contrast with the stdlib default is what makes that meaningful: the
        default opener *does* pick the variable up.
        """
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")

        default = urllib.request.build_opener()
        assert any(getattr(handler, "proxies", {}) for handler in default.handlers)

        opener = runner._opener()  # noqa: SLF001 - the seam under test
        assert all(getattr(handler, "proxies", {}) == {} for handler in opener.handlers)

    def test_process_alive_is_true_for_this_process(self):
        assert runner.process_alive(os.getpid()) is True
        assert runner.process_state(os.getpid()) == runner.PROCESS_FOREIGN

    def test_process_alive_is_false_for_a_reaped_child(self):
        assert runner.process_alive(dead_pid()) is False

    def test_a_terminated_but_unreaped_child_counts_as_gone(self):
        """A zombie answers ``kill(pid, 0)``, so it must be reaped to be seen.

        Without this, ``stop`` waits the full grace period against a process that
        already exited and then considers escalating — which is how a correct
        shutdown looks like a hung one.
        """
        child = subprocess.Popen(  # noqa: S603 - fixed argv
            [sys.executable, "-c", "pass"], stdout=subprocess.PIPE
        )
        # End-of-file on the child's pipe means it closed its descriptors, which
        # happens as it exits. Reading to EOF therefore proves the process is
        # gone without calling `poll` or `wait`, either of which would reap it and
        # destroy the very state under test.
        child.stdout.read()
        child.stdout.close()
        os.kill(child.pid, 0)
        assert runner.process_state(child.pid) == runner.PROCESS_GONE
        assert runner.process_alive(child.pid) is False

    def test_a_running_child_of_this_process_is_recognized_as_ours(self):
        """The identity that lets ``stop`` escalate against a hung shutdown."""
        child = subprocess.Popen(  # noqa: S603 - fixed argv
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )
        try:
            assert runner.process_state(child.pid) == runner.PROCESS_OWN_CHILD
        finally:
            child.kill()
            child.wait()


# =====================================================================
# The child command and its environment
# =====================================================================


class TestChildCommand:

    def test_the_child_is_this_interpreter_and_this_module(self):
        argv = runner.child_command("127.0.0.1", 8010, 600)
        assert argv[0] == sys.executable
        assert argv[1:3] == ["-m", "tests.support.tickets_fixture_server"]
        assert argv[3] == "serve"
        assert "127.0.0.1" in argv and "8010" in argv and "600" in argv

    def test_the_child_runs_from_the_package_root(self):
        root = runner.repository_root()
        assert root.name == "kb-rag-system"
        assert (root / "tests" / "support" / "tickets_fixture_server.py").is_file()

    def test_the_child_environment_sets_fixture_and_local_auth_mode(self):
        environment = runner._child_environment("a" * 64, "http://127.0.0.1:8010")  # noqa: SLF001
        assert environment[runner.FIXTURE_MODE_ENV] == runner.FIXTURE_MODE_VALUE
        assert environment[runner.FIXTURE_NONCE_ENV] == "a" * 64
        assert environment["TICKETS_ENVIRONMENT"] == "local"
        assert environment["TICKETS_AUTH_MODE"] == "local"
        assert environment["TICKETS_ALLOW_LOCAL_AUTH"] == "true"

    def test_the_child_environment_has_no_usable_cloud_identity(self):
        environment = runner._child_environment("a" * 64, "http://127.0.0.1:8010")  # noqa: SLF001
        assert not Path(environment["GOOGLE_APPLICATION_CREDENTIALS"]).exists()
        for name in ("GCE_METADATA_HOST", "GCE_METADATA_IP", "GCE_METADATA_ROOT"):
            assert environment[name] == runner.DEAD_METADATA_ENDPOINT

    def test_the_duplicated_constants_agree_with_the_fixture_application(self):
        """They are duplicated on purpose; this is what keeps them honest."""
        assert runner.FIXTURE_MODE_ENV == fixture_app.FIXTURE_MODE_ENV
        assert runner.FIXTURE_MODE_VALUE == fixture_app.FIXTURE_MODE_VALUE
        assert runner.FIXTURE_NONCE_ENV == fixture_app.FIXTURE_NONCE_ENV
        assert runner.DEAD_METADATA_ENDPOINT == fixture_app.DEAD_METADATA_ENDPOINT
        assert runner.FIXTURE_STATUS_PATH == fixture_app.FIXTURE_STATUS_PATH


class TestAutoTimeout:

    def test_the_watchdog_asks_then_insists(self):
        class _Server:
            should_exit = False

        server = _Server()
        slept: list[int] = []
        exited: list[int] = []
        runner.expire_server(
            server,
            600,
            grace_s=7,
            sleep=slept.append,
            hard_exit=exited.append,
        )
        assert slept == [600, 7], "the deadline is waited on, then the shutdown grace"
        assert server.should_exit is True
        assert exited == [0], "a hung shutdown must not outlive the deadline"

    def test_serve_refuses_a_deadline_outside_the_window(self):
        with pytest.raises(runner.FixtureServerError):
            runner.serve("127.0.0.1", 8010, 5)

    def test_serve_refuses_a_non_loopback_host(self):
        with pytest.raises(runner.FixtureServerError):
            runner.serve("0.0.0.0", 8010, 600)


class TestStartupFailure:

    def test_a_child_that_exits_immediately_is_reported_and_cleaned_up(
        self, state_dir, monkeypatch
    ):
        monkeypatch.setattr(
            runner,
            "child_command",
            lambda host, port, max_seconds: [
                sys.executable,
                "-c",
                "raise SystemExit('fixture refused to start')",
            ],
        )
        with pytest.raises(runner.FixtureServerError, match="exited during startup"):
            runner.start(state_dir, host="127.0.0.1", port=free_port(), max_seconds=60)
        assert sorted(path.name for path in state_dir.iterdir()) == []

    def test_a_child_that_never_answers_is_reported_and_cleaned_up(
        self, state_dir, monkeypatch
    ):
        monkeypatch.setattr(runner, "STARTUP_TIMEOUT_S", 1.0)
        monkeypatch.setattr(
            runner,
            "child_command",
            lambda host, port, max_seconds: [sys.executable, "-c", "import time; time.sleep(30)"],
        )
        with pytest.raises(runner.FixtureServerError, match="did not answer"):
            runner.start(state_dir, host="127.0.0.1", port=free_port(), max_seconds=60)
        assert list(state_dir.iterdir()) == []

    def test_start_refuses_an_occupied_port_without_claiming_the_directory(self, state_dir):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            with pytest.raises(runner.FixtureServerError, match="already serving"):
                runner.start(state_dir, host="127.0.0.1", port=port, max_seconds=60)
        assert list(state_dir.iterdir()) == []


# =====================================================================
# The whole lifecycle, against a real child process
# =====================================================================


class TestLifecycle:

    def test_start_status_serve_stop(self, state_dir):
        port = free_port()
        summary = runner.start(state_dir, host="127.0.0.1", port=port, max_seconds=60)
        try:
            assert summary["url"] == f"http://127.0.0.1:{port}"
            assert runner.process_alive(int(summary["pid"]))

            # Every recorded file is present and private.
            for name in runner.STATE_FILES:
                path = state_dir / name
                assert path.is_file(), name
                assert stat.S_IMODE(path.stat().st_mode) == 0o600, name

            verified = runner.verify(state_dir, expect_url=f"http://127.0.0.1:{port}")
            assert verified["nonce_verified"] is True
            assert verified["pid"] == summary["pid"]

            # The console really is serving the RAG-only interface.
            status, body = runner.probe(f"http://127.0.0.1:{port}/tickets")
            assert status == 200
            assert "RAG ticket evaluations" in body

            status, body = runner.probe(
                f"http://127.0.0.1:{port}/api/admin/v1/reviews?page_size=2"
            )
            assert status == 200
            assert "devrev_display_id" in body

            # A mutated nonce is a refusal, not a kill.
            original = runner.read_state_file(state_dir, "nonce")
            runner.write_state_file(state_dir, "nonce", "b" * 64)
            with pytest.raises(runner.FixtureServerError, match="not the one recorded"):
                runner.stop(state_dir)
            assert runner.process_alive(int(summary["pid"])), "a refused stop must not kill"
            runner.write_state_file(state_dir, "nonce", original)
        finally:
            if (state_dir / "pid").exists():
                runner.stop(state_dir)

        assert not runner.process_alive(int(summary["pid"]))
        assert list(state_dir.iterdir()) == [], "stop leaves the directory empty"
        with pytest.raises(runner.FixtureServerError):
            runner.verify(state_dir, expect_url=f"http://127.0.0.1:{port}")

    def test_the_command_line_reports_the_state_directory_and_exits_zero(
        self, state_dir, capsys
    ):
        port = free_port()
        code = runner.main(
            [
                "start",
                "--state-dir",
                str(state_dir),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--max-seconds",
                "60",
            ]
        )
        try:
            assert code == runner.EXIT_OK
            printed = capsys.readouterr().out
            assert f"FIXTURE_STATE_DIR={state_dir}" in printed
            assert (
                runner.main(
                    [
                        "status",
                        "--state-dir",
                        str(state_dir),
                        "--expect-url",
                        f"http://127.0.0.1:{port}",
                    ]
                )
                == runner.EXIT_OK
            )
        finally:
            assert runner.main(["stop", "--state-dir", str(state_dir)]) == runner.EXIT_OK

        assert (
            runner.main(
                [
                    "status",
                    "--state-dir",
                    str(state_dir),
                    "--expect-url",
                    f"http://127.0.0.1:{port}",
                ]
            )
            == runner.EXIT_NOT_VERIFIED
        )

    def test_a_second_start_in_the_same_directory_is_refused(self, state_dir):
        port = free_port()
        runner.start(state_dir, host="127.0.0.1", port=port, max_seconds=60)
        try:
            with pytest.raises(runner.FixtureServerError):
                runner.start(state_dir, host="127.0.0.1", port=free_port(), max_seconds=60)
            # The refusal must not have disturbed the running fixture.
            assert runner.verify(state_dir)["nonce_verified"] is True
        finally:
            runner.stop(state_dir)

    def test_a_start_failure_reports_the_startup_exit_code(self, state_dir, monkeypatch, capsys):
        monkeypatch.setattr(runner, "STARTUP_TIMEOUT_S", 1.0)
        monkeypatch.setattr(
            runner,
            "child_command",
            lambda host, port, max_seconds: [sys.executable, "-c", "raise SystemExit(9)"],
        )
        code = runner.main(
            [
                "start",
                "--state-dir",
                str(state_dir),
                "--host",
                "127.0.0.1",
                "--port",
                str(free_port()),
                "--max-seconds",
                "60",
            ]
        )
        assert code == runner.EXIT_STARTUP_FAILED
        assert "tickets-fixture-server:" in capsys.readouterr().err
        assert list(state_dir.iterdir()) == []


class TestDocumentedRefusals:

    def test_a_non_loopback_host_is_rejected_by_the_command_line(self, state_dir, capsys):
        code = runner.main(
            [
                "start",
                "--state-dir",
                str(state_dir),
                "--host",
                "0.0.0.0",
                "--port",
                str(free_port()),
                "--max-seconds",
                "60",
            ]
        )
        assert code == runner.EXIT_STARTUP_FAILED
        assert "127.0.0.1" in capsys.readouterr().err
        assert list(state_dir.iterdir()) == []

    def test_status_on_an_unknown_directory_exits_not_verified(self, tmp_path, capsys):
        code = runner.main(["status", "--state-dir", str(tmp_path / "nowhere")])
        assert code == runner.EXIT_NOT_VERIFIED
        assert capsys.readouterr().err

    def test_stop_on_an_unknown_directory_exits_not_verified(self, tmp_path):
        assert (
            runner.main(["stop", "--state-dir", str(tmp_path / "nowhere")])
            == runner.EXIT_NOT_VERIFIED
        )

    def test_the_urllib_import_is_present_for_the_loopback_probe(self):
        """Guards against a refactor that reaches for a networking dependency."""
        assert urllib.request is not None
        assert urllib.error is not None
        assert "httpx" not in MODULE_SOURCE
        assert "requests" not in MODULE_SOURCE
