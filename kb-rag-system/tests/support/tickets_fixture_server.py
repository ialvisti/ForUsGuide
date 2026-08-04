"""Start, inspect, and stop one loopback fixture console. Test-only.

Why this is a module rather than three shell lines
--------------------------------------------------
A browser check needs a server that outlives the command that started it, and
every convenient way to later stop such a thing is dangerous. Killing by
command-line pattern or process name hits a colleague's unrelated work. A bare
pidfile kills whatever inherited that number after the original exited. A port
check proves something is listening, not that it is *ours*.

So the identity of the process is established three ways and all three must agree
before a signal is sent: the recorded pid must still exist, the recorded URL must
answer, and the fixture endpoint at that URL must return the exact random nonce
this runner generated and stored with mode ``0600``. A recycled pid fails the
nonce check; an unrelated listener fails it too. When they disagree the runner
refuses to signal anything and says so.

The other rules, each with its reason
-------------------------------------
*   ``--host`` must be exactly ``127.0.0.1``. Binding anywhere else would put a
    console holding synthetic review data — and an authenticator that trusts
    everyone — on a reachable interface.
*   ``--port`` is explicit and must be unoccupied. Port 0 would mean the caller
    cannot know what it started, and reusing an occupied port would mean talking
    to something else.
*   the state directory must already exist, be owned by this user, and be mode
    ``0700``. The runner does not create it: a caller who chose the location knows
    whether it is a safe one, and a directory the runner invents in a shared
    temporary space is a place another user can watch.
*   ``--max-seconds`` is between one minute and thirty. The child enforces its own
    deadline, so a forgotten fixture stops on its own. That is a backstop, not a
    substitute for ``stop``.
*   ``stop`` sends ``TERM``, waits at most ten seconds, and only then may send
    ``KILL`` — to the same pid, re-verified immediately beforehand. It removes an
    explicit list of file names and never a pattern.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import secrets
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_NOT_VERIFIED = 2
EXIT_STARTUP_FAILED = 3

#: The only interface a fixture may bind.
REQUIRED_HOST = "127.0.0.1"

MIN_PORT = 1024
MAX_PORT = 65535
MIN_MAX_SECONDS = 60
MAX_MAX_SECONDS = 1800

#: Exactly the files this runner writes, and the only ones it ever removes.
STATE_FILES = ("pid", "nonce", "url", "started_at", "deadline", "log")

REQUIRED_DIR_MODE = 0o700
STATE_FILE_MODE = 0o600

#: A state file is a pid, a token, a URL, or a timestamp. Nothing is large.
MAX_STATE_FILE_BYTES = 4096

#: How long to wait for the child to answer its liveness probe.
STARTUP_TIMEOUT_S = 25.0
STARTUP_POLL_S = 0.15

#: How long ``stop`` waits for a graceful exit before it may escalate.
TERM_GRACE_S = 10.0

PROBE_TIMEOUT_S = 2.0
NONCE_BYTES = 32

FIXTURE_STATUS_PATH = "/__fixture/status"
LIVENESS_PATH = "/livez"

#: Duplicated from the fixture application rather than imported, because
#: importing that module poisons this process's credential environment and
#: installs its socket guard — side effects a ``status`` or ``stop`` command has
#: no business causing. A test asserts the two definitions agree.
FIXTURE_MODE_ENV = "TICKETS_FIXTURE_MODE"
FIXTURE_MODE_VALUE = "1"
FIXTURE_NONCE_ENV = "TICKETS_FIXTURE_NONCE"
DEAD_METADATA_ENDPOINT = "127.0.0.1:1"
ABSENT_CREDENTIALS_FILE = "tickets-fixture-absent-credentials.json"


class FixtureServerError(RuntimeError):
    """A refusal. The message is intended for a person reading a terminal."""


# ---------------------------------------------------------------------------
# Validation. Pure, so the refusals are unit-testable without a subprocess.
# ---------------------------------------------------------------------------


def validate_host(host: str) -> str:
    if host != REQUIRED_HOST:
        raise FixtureServerError(
            f"--host must be exactly {REQUIRED_HOST}; a fixture console must not be reachable"
        )
    return host


def validate_port(port: object) -> int:
    try:
        value = int(str(port).strip())
    except (TypeError, ValueError) as exc:
        raise FixtureServerError("--port must be an integer") from exc
    if not MIN_PORT <= value <= MAX_PORT:
        raise FixtureServerError(f"--port must be between {MIN_PORT} and {MAX_PORT}")
    return value


def validate_max_seconds(seconds: object) -> int:
    try:
        value = int(str(seconds).strip())
    except (TypeError, ValueError) as exc:
        raise FixtureServerError("--max-seconds must be an integer") from exc
    if not MIN_MAX_SECONDS <= value <= MAX_MAX_SECONDS:
        raise FixtureServerError(
            f"--max-seconds must be between {MIN_MAX_SECONDS} and {MAX_MAX_SECONDS}"
        )
    return value


def validate_state_dir(path: Path, *, fresh: bool) -> Path:
    """Refuse a directory this runner should not be writing secrets into."""
    resolved = Path(path)
    if resolved.is_symlink():
        raise FixtureServerError("--state-dir must not be a symbolic link")
    if not resolved.is_dir():
        raise FixtureServerError(
            "--state-dir must already exist; create it yourself with mode 0700 so the "
            "location is your choice rather than this runner's"
        )
    info = resolved.stat()
    if info.st_uid != os.geteuid():
        raise FixtureServerError("--state-dir must be owned by the user running this command")
    if stat.S_IMODE(info.st_mode) != REQUIRED_DIR_MODE:
        raise FixtureServerError("--state-dir must be mode 0700")
    if fresh:
        existing = sorted(name for name in STATE_FILES if (resolved / name).exists())
        if existing:
            raise FixtureServerError(
                "--state-dir already holds fixture state "
                f"({', '.join(existing)}); stop the previous fixture first"
            )
    return resolved


def assert_port_available(host: str, port: int) -> None:
    """Refuse a port something is already using.

    Two checks, because they fail for different reasons: a successful connect
    means a live listener, and a failed bind means the address is unusable even
    if nothing accepted a connection.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        if probe.connect_ex((host, port)) == 0:
            raise FixtureServerError(f"port {port} is already serving something")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as binder:
            binder.bind((host, port))
    except OSError as exc:
        raise FixtureServerError(f"port {port} cannot be bound: {exc.strerror}") from exc


# ---------------------------------------------------------------------------
# State files
# ---------------------------------------------------------------------------


def write_state_file(directory: Path, name: str, value: str) -> Path:
    """Write one state file with mode 0600, creating it if needed."""
    if name not in STATE_FILES:
        raise FixtureServerError(f"{name!r} is not a declared state file")
    path = directory / name
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, STATE_FILE_MODE)
    try:
        os.write(descriptor, value.encode("utf-8"))
    finally:
        os.close(descriptor)
    os.chmod(path, STATE_FILE_MODE)
    return path


def read_state_file(directory: Path, name: str) -> str:
    if name not in STATE_FILES:
        raise FixtureServerError(f"{name!r} is not a declared state file")
    path = directory / name
    if path.is_symlink():
        raise FixtureServerError(f"the {name} state file must not be a symbolic link")
    if not path.is_file():
        raise FixtureServerError(f"no {name} state file: this directory holds no live fixture")
    if stat.S_IMODE(path.stat().st_mode) != STATE_FILE_MODE:
        raise FixtureServerError(f"the {name} state file must be mode 0600")
    with path.open("rb") as handle:
        raw = handle.read(MAX_STATE_FILE_BYTES + 1)
    if len(raw) > MAX_STATE_FILE_BYTES:
        raise FixtureServerError(f"the {name} state file is implausibly large")
    return raw.decode("utf-8", errors="replace").strip()


def remove_state_files(directory: Path) -> list[str]:
    """Remove exactly the declared files. Never a pattern, never a directory walk."""
    removed: list[str] = []
    for name in STATE_FILES:
        path = directory / name
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            continue
        removed.append(name)
    return removed


def claim_state_dir(directory: Path) -> None:
    """Atomically take ownership of the directory for one fixture.

    ``O_EXCL`` is the whole mechanism: two concurrent starts race on this one
    call, and exactly one of them wins.
    """
    try:
        descriptor = os.open(directory / "pid", os.O_CREAT | os.O_EXCL | os.O_WRONLY, STATE_FILE_MODE)
    except FileExistsError as exc:
        raise FixtureServerError(
            "another start already claimed this state directory"
        ) from exc
    os.close(descriptor)


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------


def _opener() -> urllib.request.OpenerDirector:
    # An ambient proxy setting would send a loopback probe somewhere else.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def probe(url: str, *, timeout: float = PROBE_TIMEOUT_S) -> tuple[int, str]:
    """GET a loopback URL, returning the status and a bounded body."""
    request = urllib.request.Request(url, method="GET")  # noqa: S310 - loopback, validated
    try:
        with _opener().open(request, timeout=timeout) as response:
            return response.status, response.read(MAX_STATE_FILE_BYTES).decode(
                "utf-8", errors="replace"
            )
    except urllib.error.HTTPError as error:
        return error.code, ""
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0, ""


def fixture_nonce_at(url: str) -> Optional[str]:
    """The nonce a fixture reports, or ``None`` if it is not a fixture at all."""
    status, body = probe(f"{url}{FIXTURE_STATUS_PATH}")
    if status != 200:
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    nonce = payload.get("nonce")
    return nonce if isinstance(nonce, str) and nonce != "" else None


#: A pid that no longer names a process, or names one already reaped.
PROCESS_GONE = "gone"
#: A pid this very process spawned and has not yet reaped. While that is true the
#: number cannot be reused, which makes it as strong an identity as the nonce.
PROCESS_OWN_CHILD = "own-child"
#: A live process this runner did not spawn — including one whose number may have
#: been recycled since it was recorded.
PROCESS_FOREIGN = "foreign"


def process_state(pid: int) -> str:
    """Classify a pid, distinguishing a *zombie* from a running process.

    ``os.kill(pid, 0)`` succeeds against a terminated-but-unreaped child, so a
    liveness check built only on it never observes a graceful exit when the
    parent is a long-lived process such as a test session. Reaping first is what
    makes "did it stop?" answerable.
    """
    own = False
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        own = True
        if reaped == pid:
            return PROCESS_GONE
    except ChildProcessError:
        own = False
    except OSError:  # pragma: no cover - platform dependent
        own = False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return PROCESS_GONE
    except PermissionError:
        # It exists and belongs to somebody else, which is decisive on its own:
        # this runner must not signal it.
        return PROCESS_FOREIGN
    except OSError as exc:  # pragma: no cover - defensive
        if exc.errno == errno.ESRCH:
            return PROCESS_GONE
    return PROCESS_OWN_CHILD if own else PROCESS_FOREIGN


def process_alive(pid: int) -> bool:
    return process_state(pid) != PROCESS_GONE


def expected_url(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def verify(directory: Path, *, expect_url: Optional[str] = None) -> dict[str, object]:
    """Prove the recorded fixture is running, and is the one we recorded.

    Raises rather than returning a verdict, because every caller either wants a
    verified fixture or wants to stop.
    """
    validate_state_dir(directory, fresh=False)
    raw_pid = read_state_file(directory, "pid")
    if not raw_pid.isdecimal():
        raise FixtureServerError("the recorded pid is not a number; refusing to signal anything")
    pid = int(raw_pid)
    if pid <= 1:
        raise FixtureServerError("the recorded pid is not a plausible child process")
    nonce = read_state_file(directory, "nonce")
    if len(nonce) != NONCE_BYTES * 2 or not all(c in "0123456789abcdef" for c in nonce):
        raise FixtureServerError("the recorded nonce is malformed")
    url = read_state_file(directory, "url")
    if not url.startswith(f"http://{REQUIRED_HOST}:"):
        raise FixtureServerError("the recorded URL is not a loopback fixture URL")
    if expect_url is not None and url != expect_url.rstrip("/"):
        raise FixtureServerError(f"the recorded URL is {url}, not {expect_url}")
    if not process_alive(pid):
        raise FixtureServerError(
            f"pid {pid} is gone; the state directory is stale. Remove it rather than signalling"
        )
    reported = fixture_nonce_at(url)
    if reported is None:
        raise FixtureServerError(
            f"nothing at {url} identifies itself as this fixture; refusing to signal pid {pid}"
        )
    if not secrets.compare_digest(reported, nonce):
        raise FixtureServerError(
            f"the process at {url} is not the one recorded here; refusing to signal pid {pid}"
        )
    return {
        "pid": pid,
        "url": url,
        "nonce_verified": True,
        "started_at": read_state_file(directory, "started_at"),
        "deadline": read_state_file(directory, "deadline"),
    }


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


def _child_environment(nonce: str, url: str) -> dict[str, str]:
    """The child's environment: fixture mode, local auth, and no cloud identity."""
    environment = dict(os.environ)
    environment.update(
        {
            FIXTURE_MODE_ENV: FIXTURE_MODE_VALUE,
            FIXTURE_NONCE_ENV: nonce,
            "TICKETS_ENVIRONMENT": "local",
            "TICKETS_AUTH_MODE": "local",
            "TICKETS_ALLOW_LOCAL_AUTH": "true",
            "TICKETS_CONSOLE_ORIGIN": url,
            # Ambient credentials are neutralized here as well as inside the
            # child, so a failure to import the fixture module cannot leave a
            # process holding a real identity.
            "GOOGLE_APPLICATION_CREDENTIALS": str(
                Path(os.devnull).parent / ABSENT_CREDENTIALS_FILE
            ),
            "GCE_METADATA_HOST": DEAD_METADATA_ENDPOINT,
            "GCE_METADATA_IP": DEAD_METADATA_ENDPOINT,
            "GCE_METADATA_ROOT": DEAD_METADATA_ENDPOINT,
            "NO_GCE_CHECK": "true",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return environment


def repository_root() -> Path:
    """``kb-rag-system``, derived from this file rather than the caller's cwd."""
    return Path(__file__).resolve().parent.parent.parent


def child_command(host: str, port: int, max_seconds: int) -> list[str]:
    """The exact argv of the child. No shell, no console script, no wrapper."""
    return [
        sys.executable,
        "-m",
        "tests.support.tickets_fixture_server",
        "serve",
        "--host",
        host,
        "--port",
        str(port),
        "--max-seconds",
        str(max_seconds),
    ]


def start(
    directory: Path, *, host: str, port: int, max_seconds: int
) -> dict[str, object]:
    """Validate everything, spawn one child, prove it is ours, record it."""
    validate_host(host)
    validate_port(port)
    validate_max_seconds(max_seconds)
    validate_state_dir(directory, fresh=True)
    assert_port_available(host, port)

    claim_state_dir(directory)
    nonce = secrets.token_hex(NONCE_BYTES)
    url = expected_url(host, port)
    started_at = datetime.now(timezone.utc)
    deadline = started_at + timedelta(seconds=max_seconds)

    log_path = write_state_file(directory, "log", "")
    child: Optional[subprocess.Popen] = None
    try:
        with log_path.open("ab", buffering=0) as log:
            child = subprocess.Popen(  # noqa: S603 - fixed argv, shell=False
                child_command(host, port, max_seconds),
                cwd=str(repository_root()),
                env=_child_environment(nonce, url),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                # Its own session, so it survives this command and so a signal
                # sent to this command's group never reaches it by accident.
                start_new_session=True,
                close_fds=True,
                shell=False,
            )
        write_state_file(directory, "pid", str(child.pid))
        write_state_file(directory, "nonce", nonce)
        write_state_file(directory, "url", url)
        write_state_file(directory, "started_at", started_at.isoformat())
        write_state_file(directory, "deadline", deadline.isoformat())

        _await_liveness(child, url, log_path)
        reported = fixture_nonce_at(url)
        if reported is None or not secrets.compare_digest(reported, nonce):
            raise FixtureServerError(
                f"something is listening on {url} but it is not this fixture"
            )
    except BaseException:
        _abandon(child, directory)
        raise

    return {
        "pid": child.pid,
        "url": url,
        "state_dir": str(directory),
        "started_at": started_at.isoformat(),
        "deadline": deadline.isoformat(),
    }


def _await_liveness(child: subprocess.Popen, url: str, log_path: Path) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise FixtureServerError(
                f"the fixture exited during startup with code {child.returncode}\n"
                f"{_log_tail(log_path)}"
            )
        if probe(f"{url}{LIVENESS_PATH}")[0] == 200:
            return
        time.sleep(STARTUP_POLL_S)
    raise FixtureServerError(
        f"the fixture did not answer {LIVENESS_PATH} within "
        f"{STARTUP_TIMEOUT_S:.0f}s\n{_log_tail(log_path)}"
    )


def _log_tail(log_path: Path, limit: int = 4000) -> str:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(no log available)"
    return text[-limit:]


def _abandon(child: Optional[subprocess.Popen], directory: Path) -> None:
    """Give up on a start: stop the child we spawned, then clear our own files."""
    if child is not None and child.poll() is None:
        try:
            child.terminate()
            child.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                child.kill()
            except OSError:  # pragma: no cover - the process is already gone
                pass
    remove_state_files(directory)


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


def stop(directory: Path, *, expect_url: Optional[str] = None) -> dict[str, object]:
    """Verify, terminate, escalate only against the same verified pid, clean up."""
    verified = verify(directory, expect_url=expect_url)
    pid = int(verified["pid"])
    os.kill(pid, signal.SIGTERM)

    escalated = False
    deadline = time.monotonic() + TERM_GRACE_S
    while time.monotonic() < deadline:
        if not process_alive(pid):
            break
        time.sleep(0.1)
    else:
        # Still alive after the grace period. Re-establish identity before
        # escalating, because during those ten seconds the original could have
        # exited and its number been reused — and killing a recycled pid is
        # exactly the accident this module exists to prevent.
        #
        # Either proof is sufficient, and they cover different situations:
        #
        #   * the fixture endpoint still returns our nonce, so the listener is
        #     demonstrably ours. This is the only proof available when `stop` runs
        #     in a different process from `start`;
        #   * the pid is still an unreaped child of *this* process, so the number
        #     cannot have been recycled at all. This one still holds when the
        #     server has already closed its listener and is hanging in shutdown,
        #     which is the case the endpoint check cannot speak to.
        recheck = fixture_nonce_at(str(verified["url"]))
        stored = read_state_file(directory, "nonce")
        answers_as_ours = recheck is not None and secrets.compare_digest(recheck, stored)
        if not answers_as_ours and process_state(pid) != PROCESS_OWN_CHILD:
            raise FixtureServerError(
                f"pid {pid} did not exit and can no longer be proven to be this "
                "fixture; refusing to escalate. Investigate that process by hand"
            )
        os.kill(pid, signal.SIGKILL)
        escalated = True
        grace = time.monotonic() + 5
        while time.monotonic() < grace and process_alive(pid):
            time.sleep(0.1)

    removed = remove_state_files(directory)
    return {"pid": pid, "escalated": escalated, "removed": removed}


# ---------------------------------------------------------------------------
# serve (the child)
# ---------------------------------------------------------------------------


def serve(host: str, port: int, max_seconds: int) -> int:
    """Run the fixture console until its deadline. Only ever a child process."""
    validate_host(host)
    validate_port(port)
    validate_max_seconds(max_seconds)

    from tests.support.tickets_console_fixture_app import (
        FIXTURE_MODE_ENV,
        FIXTURE_MODE_VALUE,
        build_fixture_app,
    )

    if os.environ.get(FIXTURE_MODE_ENV) != FIXTURE_MODE_VALUE:  # pragma: no cover
        raise FixtureServerError("the parent did not set fixture mode")

    import threading

    import uvicorn

    application = build_fixture_app()
    server = uvicorn.Server(
        uvicorn.Config(
            application,
            host=host,
            port=port,
            log_level="info",
            access_log=True,
            lifespan="on",
            # No reloader and no worker fan-out: exactly one process, which is
            # the only thing ``stop`` knows how to account for.
            workers=1,
        )
    )

    threading.Thread(
        target=expire_server,
        args=(server, max_seconds),
        name="fixture-deadline",
        daemon=True,
    ).start()
    server.run()
    return EXIT_OK


#: How long the deadline watchdog waits for a graceful shutdown before it stops
#: asking. A hung shutdown must not let a forgotten fixture outlive its deadline.
SHUTDOWN_GRACE_S = 10


def expire_server(
    server: object,
    max_seconds: int,
    *,
    grace_s: int = SHUTDOWN_GRACE_S,
    sleep=time.sleep,
    hard_exit=os._exit,  # noqa: SLF001 - the documented immediate-exit primitive
) -> None:
    """Ask the server to stop at its deadline, then insist.

    Separated from :func:`serve` and given injectable ``sleep``/``hard_exit`` so
    the auto-timeout is testable without waiting a minute or ending the test
    process.
    """
    sleep(max_seconds)
    server.should_exit = True
    sleep(grace_s)
    hard_exit(0)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tests.support.tickets_fixture_server",
        description="Run one loopback-only fixture ticket console. Test use only.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    starter = commands.add_parser("start", help="spawn a fixture and record its identity")
    starter.add_argument("--state-dir", required=True)
    starter.add_argument("--host", required=True)
    starter.add_argument("--port", required=True)
    starter.add_argument("--max-seconds", required=True)

    checker = commands.add_parser("status", help="verify the recorded fixture is live")
    checker.add_argument("--state-dir", required=True)
    checker.add_argument("--expect-url", default=None)

    stopper = commands.add_parser("stop", help="terminate the recorded fixture and clean up")
    stopper.add_argument("--state-dir", required=True)
    stopper.add_argument("--expect-url", default=None)

    server = commands.add_parser("serve", help="internal: the child process entry point")
    server.add_argument("--host", required=True)
    server.add_argument("--port", required=True)
    server.add_argument("--max-seconds", required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "start":
            summary = start(
                Path(args.state_dir),
                host=args.host,
                port=validate_port(args.port),
                max_seconds=validate_max_seconds(args.max_seconds),
            )
            print(json.dumps(summary, indent=2))
            print(f"FIXTURE_STATE_DIR={args.state_dir}")
            return EXIT_OK
        if args.command == "status":
            print(json.dumps(verify(Path(args.state_dir), expect_url=args.expect_url), indent=2))
            return EXIT_OK
        if args.command == "stop":
            print(json.dumps(stop(Path(args.state_dir), expect_url=args.expect_url), indent=2))
            return EXIT_OK
        if args.command == "serve":  # pragma: no cover - the child path
            return serve(
                args.host, validate_port(args.port), validate_max_seconds(args.max_seconds)
            )
    except FixtureServerError as error:
        print(f"tickets-fixture-server: {error}", file=sys.stderr)
        if args.command == "start":
            return EXIT_STARTUP_FAILED
        return EXIT_NOT_VERIFIED
    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
