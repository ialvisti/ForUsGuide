#!/usr/bin/env python3
"""Read-only guard that fails when the Git index holds paths outside an allowlist.

Usage:
    python scripts/verify_staged_scope.py --allow PATH [--allow PATH ...]

Every ``--allow`` value is an EXACT repository-root-relative path: not a glob and
not a directory prefix. Exit codes are distinct so callers can branch on them:

    0  the staged set is a subset of the allowlist (staging fewer files than
       allowed is legitimate, so an allowed-but-unstaged path is not an error)
    1  Git itself failed (could not be executed, or exited non-zero)
    2  at least one staged path is outside the allowlist

This script NEVER mutates repository state. The only commands it may run are
``git rev-parse --show-toplevel`` and ``git diff --cached --name-only -z``; it
does not stage, unstage, add, reset, checkout, commit, stash, or write anything.
It reports staged PATHS only and never prints staged file content.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

EXIT_OK = 0
EXIT_GIT_FAILED = 1
EXIT_UNEXPECTED_PATHS = 2

_GIT = "git"
_ERROR_PREFIX = "verify-staged-scope:"
_UNEXPECTED_PREFIX = "unexpected staged path:"


def parse_nul_separated(raw: bytes) -> list[str]:
    """Split NUL-delimited Git output into UTF-8 decoded paths.

    ``git ... -z`` terminates every record with a NUL byte, so splitting always
    produces a trailing empty element; it is discarded here (as is any other
    empty element, which is never a valid path).
    """
    if not raw:
        return []
    return [part.decode("utf-8") for part in raw.split(b"\x00") if part]


def unexpected_staged_paths(staged: Iterable[str], allowed: Iterable[str]) -> list[str]:
    """Return staged paths absent from ``allowed``, sorted and de-duplicated.

    Sorting keeps the report deterministic; membership is exact string equality
    against the allowlist, never a prefix or glob match.
    """
    allowlist = set(allowed)
    return sorted({path for path in staged if path not in allowlist})


def _run_git(argv: list[str]) -> bytes:
    """Run a read-only Git command and return its raw stdout bytes."""
    # S603: the argv is a fixed, read-only list built in this module (no shell,
    # no user-controlled executable); only the repository root is interpolated,
    # and it comes from Git itself.
    completed = subprocess.run(  # noqa: S603 - fixed read-only argv list, shell=False
        argv,
        check=True,
        capture_output=True,
        shell=False,
    )
    return completed.stdout


def _repo_root(start: str | None = None) -> str:
    """Return the absolute repository root via read-only ``git rev-parse``.

    The default anchor is the directory containing THIS SCRIPT, not the process
    working directory. Every stage invokes this guard by absolute path, often
    from a different worktree; anchoring on the cwd would silently inspect the
    wrong repository's index and exit 0 while the repository actually being
    committed held unreviewed paths. A guard that can pass vacuously is worse
    than no guard, so the anchor is explicit and overridable via ``--repo``.
    """
    anchor = start if start is not None else str(Path(__file__).resolve().parent)
    return _run_git([_GIT, "-C", anchor, "rev-parse", "--show-toplevel"]).decode("utf-8").strip()


def _staged_paths(root: str) -> list[str]:
    """Return the staged paths for ``root`` using NUL-delimited Git output."""
    # `-z` is mandatory: without it Git QUOTES and escapes paths containing
    # spaces, double quotes, newlines or non-ASCII bytes, and embedded newlines
    # would additionally break any line-based splitting. With `-z` Git emits
    # raw, unquoted, NUL-terminated paths, so the parsing below stays exact.
    raw = _run_git([_GIT, "-C", root, "diff", "--cached", "--name-only", "-z"])
    return parse_nul_separated(raw)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fail when the Git index contains paths outside the reviewed "
            "allowlist. Read-only: never modifies repository state."
        ),
    )
    parser.add_argument(
        "--allow",
        action="append",
        required=True,
        metavar="PATH",
        help=(
            "Exact repository-root-relative path allowed in the index. "
            "Repeat once per allowed path; globs and prefixes are not expanded."
        ),
    )
    parser.add_argument(
        "--repo",
        metavar="DIR",
        default=None,
        help=(
            "Directory used to locate the repository root. Defaults to this "
            "script's own directory, so the guard always inspects the worktree "
            "it ships in rather than whatever directory the caller happens to "
            "be in."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; see the module docstring for the exit-code contract."""
    args = _build_parser().parse_args(argv)
    try:
        root = _repo_root(args.repo)
        staged = _staged_paths(root)
    except subprocess.CalledProcessError as error:
        print(
            f"{_ERROR_PREFIX} git exited with status {error.returncode}",
            file=sys.stderr,
        )
        return EXIT_GIT_FAILED
    except OSError:
        print(f"{_ERROR_PREFIX} unable to execute git", file=sys.stderr)
        return EXIT_GIT_FAILED

    unexpected = unexpected_staged_paths(staged, args.allow)
    if unexpected:
        for path in unexpected:
            print(f"{_UNEXPECTED_PREFIX} {path}", file=sys.stderr)
        print(
            f"{_ERROR_PREFIX} {len(unexpected)} staged path(s) outside the allowlist",
            file=sys.stderr,
        )
        return EXIT_UNEXPECTED_PATHS
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
