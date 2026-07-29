"""Tests for the read-only staged-scope guard.

The unit tests patch the module's own Git seams (``_repo_root`` / ``_staged_paths``)
so no Git process ever runs against the working repository. The single
integration-style test builds a throwaway repository under ``tmp_path`` with an
isolated Git configuration and never touches the real checkout.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import verify_staged_scope as module
from scripts.verify_staged_scope import parse_nul_separated, unexpected_staged_paths

SPACED = "docs/plan notes.md"
QUOTED = 'docs/say "hi".md'
NEWLINED = "docs/line\nbreak.md"
NON_ASCII = "docs/informe-ción-ñ-测试.md"


def _nul(*paths: str) -> bytes:
    """Encode paths the way `git ... -z` does: raw, unquoted, NUL-terminated."""
    return b"".join(path.encode("utf-8") + b"\x00" for path in paths)


def _stub_git(monkeypatch: pytest.MonkeyPatch, staged: list[str]) -> None:
    """Replace both Git seams so main() runs fully offline."""
    monkeypatch.setattr(module, "_repo_root", lambda start=None: "/nonexistent/fake-root")
    monkeypatch.setattr(module, "_staged_paths", lambda root: list(staged))


class TestParseNulSeparated:
    def test_empty_input_returns_empty_list(self):
        assert parse_nul_separated(b"") == []

    def test_trailing_empty_element_is_discarded(self):
        assert parse_nul_separated(_nul("a.py", "b.py")) == ["a.py", "b.py"]

    def test_single_path_without_trailing_nul(self):
        assert parse_nul_separated(b"a.py") == ["a.py"]

    def test_space_quote_and_newline_paths_survive_intact(self):
        raw = _nul(SPACED, QUOTED, NEWLINED)
        # Proves NUL parsing rather than shell/line splitting: the newline path
        # stays a single element instead of becoming two.
        assert parse_nul_separated(raw) == [SPACED, QUOTED, NEWLINED]

    def test_non_ascii_path_round_trips(self):
        assert parse_nul_separated(_nul(NON_ASCII)) == [NON_ASCII]

    def test_order_is_preserved(self):
        assert parse_nul_separated(_nul("z.py", "a.py")) == ["z.py", "a.py"]


class TestUnexpectedStagedPaths:
    def test_exact_match_reports_nothing(self):
        allowed = ["api/main.py", "tests/test_api.py"]
        assert unexpected_staged_paths(list(allowed), allowed) == []

    def test_single_extra_file_is_reported(self):
        staged = ["api/main.py", "api/secret_scratch.py"]
        allowed = ["api/main.py"]
        assert unexpected_staged_paths(staged, allowed) == ["api/secret_scratch.py"]

    def test_allowed_but_unstaged_path_is_not_an_error(self):
        allowed = ["api/main.py", "docs/never_staged.md"]
        assert unexpected_staged_paths(["api/main.py"], allowed) == []

    def test_empty_index_is_never_unexpected(self):
        assert unexpected_staged_paths([], ["api/main.py"]) == []

    def test_spaced_path_matches_the_allowlist(self):
        assert unexpected_staged_paths([SPACED], [SPACED]) == []

    def test_spaced_path_outside_allowlist_is_reported_whole(self):
        assert unexpected_staged_paths([SPACED], ["docs/plan"]) == [SPACED]

    def test_quoted_newline_and_non_ascii_paths_match_exactly(self):
        odd = [SPACED, QUOTED, NEWLINED, NON_ASCII]
        assert unexpected_staged_paths(odd, odd) == []

    def test_matching_is_exact_not_prefix_or_glob(self):
        staged = ["api/main.py", "api/sub/main.py"]
        assert unexpected_staged_paths(staged, ["api"]) == staged
        assert unexpected_staged_paths(staged, ["api/*.py"]) == staged

    def test_output_is_sorted_and_deduplicated(self):
        staged = ["z.py", "a.py", "m.py", "a.py"]
        assert unexpected_staged_paths(staged, []) == ["a.py", "m.py", "z.py"]

    def test_ordering_is_independent_of_input_order(self):
        forward = unexpected_staged_paths(["b.py", "a.py", "c.py"], [])
        backward = unexpected_staged_paths(["c.py", "a.py", "b.py"], [])
        assert forward == backward == ["a.py", "b.py", "c.py"]

    def test_accepts_arbitrary_iterables(self):
        assert unexpected_staged_paths(iter(["a.py"]), iter([])) == ["a.py"]


class TestMain:
    def test_subset_of_allowlist_exits_zero_and_is_quiet(self, monkeypatch, capsys):
        _stub_git(monkeypatch, ["api/main.py"])
        code = module.main(["--allow", "api/main.py", "--allow", "docs/unstaged.md"])
        captured = capsys.readouterr()
        assert code == 0
        assert captured.err == ""
        assert captured.out == ""

    def test_one_extra_file_exits_two_and_names_it_on_stderr(self, monkeypatch, capsys):
        _stub_git(monkeypatch, ["api/main.py", "api/oops.py"])
        code = module.main(["--allow", "api/main.py"])
        captured = capsys.readouterr()
        assert code == 2
        assert "api/oops.py" in captured.err
        assert "api/main.py" not in captured.err
        assert captured.out == ""

    def test_spaced_path_is_reported_on_stderr(self, monkeypatch, capsys):
        _stub_git(monkeypatch, ["api/main.py", SPACED])
        code = module.main(["--allow", "api/main.py"])
        assert code == 2
        assert SPACED in capsys.readouterr().err

    def test_quoted_newline_and_non_ascii_paths_are_reported(self, monkeypatch, capsys):
        _stub_git(monkeypatch, [QUOTED, NEWLINED, NON_ASCII])
        code = module.main(["--allow", "api/main.py"])
        err = capsys.readouterr().err
        assert code == 2
        for path in (QUOTED, NEWLINED, NON_ASCII):
            assert path in err

    def test_stderr_lists_unexpected_paths_in_sorted_order(self, monkeypatch, capsys):
        _stub_git(monkeypatch, ["z.py", "a.py", "m.py"])
        assert module.main(["--allow", "kept.py"]) == 2
        reported = [
            line.split(": ", 1)[1]
            for line in capsys.readouterr().err.splitlines()
            if line.startswith("unexpected staged path: ")
        ]
        assert reported == ["a.py", "m.py", "z.py"]

    def test_git_failure_exits_one_not_two(self, monkeypatch, capsys):
        def _boom(start=None):
            raise subprocess.CalledProcessError(128, ["git", "rev-parse"])

        monkeypatch.setattr(module, "_repo_root", _boom)
        code = module.main(["--allow", "api/main.py"])
        captured = capsys.readouterr()
        assert code == 1
        assert "git" in captured.err

    def test_missing_git_binary_exits_one(self, monkeypatch, capsys):
        def _boom(start=None):
            raise FileNotFoundError("git")

        monkeypatch.setattr(module, "_repo_root", _boom)
        assert module.main(["--allow", "api/main.py"]) == 1
        assert "git" in capsys.readouterr().err

    def test_allow_is_required(self, monkeypatch):
        _stub_git(monkeypatch, [])
        with pytest.raises(SystemExit):
            module.main([])


class TestReadOnlyContract:
    def test_only_read_only_git_subcommands_appear_in_the_source(self):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for mutating in ('"add"', '"commit"', '"reset"', '"checkout"', '"stash"'):
            assert mutating not in source

    def test_git_seams_request_nul_delimited_output(self, monkeypatch):
        recorded: list[list[str]] = []

        def _capture(argv):
            recorded.append(argv)
            return b""

        monkeypatch.setattr(module, "_run_git", _capture)
        assert module._staged_paths("/repo") == []
        assert recorded == [
            ["git", "-C", "/repo", "diff", "--cached", "--name-only", "-z"]
        ]


def _git(repo: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - fixed argv list, shell=False, tmp_path repo
        ["git", "-C", str(repo), *args],  # noqa: S607 - resolve git from PATH in tests
        check=True,
        capture_output=True,
        env=env,
    )


def _isolated_git_env(home: Path) -> dict[str, str]:
    """Git environment that ignores global/system config and any outer repo."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    env.update(
        {
            "HOME": str(home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CEILING_DIRECTORIES": str(home),
        }
    )
    return env


class TestThrowawayRepository:
    """End-to-end coverage against a scratch repo created under tmp_path only."""

    def _repo(self, tmp_path: Path) -> tuple[Path, dict[str, str], list[str]]:
        if shutil.which("git") is None:
            pytest.skip("git is not installed")
        home = tmp_path / "home"
        home.mkdir()
        repo = tmp_path / "scratch-repo"
        repo.mkdir()
        env = _isolated_git_env(home)
        subprocess.run(  # noqa: S603 - fixed argv list, shell=False, tmp_path repo
            ["git", "init", "-q", str(repo)],  # noqa: S607 - git resolved from PATH
            check=True,
            capture_output=True,
            env=env,
        )
        _git(repo, "config", "user.email", "scratch@example.invalid", env=env)
        _git(repo, "config", "user.name", "Scratch Repo", env=env)
        # macOS stores filenames as NFD; precomposeunicode makes Git record the
        # NFC form so non-ASCII paths compare equal to the allowlist literals.
        _git(repo, "config", "core.precomposeunicode", "true", env=env)
        _git(repo, "commit", "-q", "--allow-empty", "-m", "root", env=env)

        names = ["plain.md", SPACED.split("/")[-1], NON_ASCII.split("/")[-1]]
        for candidate in (QUOTED.split("/")[-1], NEWLINED.split("/")[-1]):
            try:
                (repo / candidate).write_text("x", encoding="utf-8")
            except OSError:  # filesystem refuses the character; skip just that one
                continue
            names.append(candidate)
        for name in names:
            (repo / name).write_text("x", encoding="utf-8")
        _git(repo, "add", "--", *names, env=env)
        return repo, env, names

    def _run_guard(
        self, repo: Path, env: dict[str, str], allow: list[str]
    ) -> subprocess.CompletedProcess:
        argv = [sys.executable, str(Path(module.__file__).resolve())]
        for path in allow:
            argv += ["--allow", path]
        # --repo is required: the guard deliberately anchors on its own script
        # directory, so cwd alone no longer selects the repository under test.
        argv += ["--repo", str(repo)]
        return subprocess.run(  # noqa: S603 - sys.executable + this module's own path
            argv, cwd=str(repo), capture_output=True, text=True, env=env, check=False
        )

    def test_fully_allowlisted_index_exits_zero(self, tmp_path):
        repo, env, names = self._repo(tmp_path)
        result = self._run_guard(repo, env, names)
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""

    def test_one_extra_staged_file_exits_two_with_the_real_path(self, tmp_path):
        repo, env, names = self._repo(tmp_path)
        extra = names[1]  # the filename containing a space
        allow = [name for name in names if name != extra]
        result = self._run_guard(repo, env, allow)
        assert result.returncode == 2, result.stdout + result.stderr
        assert f"unexpected staged path: {extra}" in result.stderr
        for kept in allow:
            assert f"unexpected staged path: {kept}\n" not in result.stderr

    def test_staging_a_subset_of_the_allowlist_exits_zero(self, tmp_path):
        repo, env, names = self._repo(tmp_path)
        result = self._run_guard(repo, env, [*names, "docs/never-created.md"])
        assert result.returncode == 0, result.stderr


class TestRepoRootAnchoring:
    """The guard must inspect the worktree it ships in, not the caller's cwd.

    Regression: `_repo_root()` originally ran `git rev-parse --show-toplevel`
    with no `-C`, so invoking the guard by absolute path from another repository
    (exactly how every stage's commit block calls it) inspected that other
    repository's empty index and exited 0 while the repository being committed
    held unreviewed paths.
    """

    def test_default_anchor_is_the_script_directory_not_the_cwd(self, monkeypatch, tmp_path):
        import scripts.verify_staged_scope as module

        recorded: list[list[str]] = []

        def fake_run_git(argv):
            recorded.append(argv)
            if "rev-parse" in argv:
                return b"/anchored/repo\n"
            return b""

        monkeypatch.setattr(module, "_run_git", fake_run_git)
        monkeypatch.chdir(tmp_path)

        assert module._repo_root() == "/anchored/repo"
        rev_parse = recorded[0]
        assert rev_parse[1] == "-C"
        # Anchored on the script's own directory, never the (changed) cwd.
        assert rev_parse[2] == str(Path(module.__file__).resolve().parent)
        assert rev_parse[2] != str(tmp_path)

    def test_an_explicit_repo_override_is_honoured(self, monkeypatch):
        import scripts.verify_staged_scope as module

        recorded: list[list[str]] = []

        def fake_run_git(argv):
            recorded.append(argv)
            return b"/explicit/repo\n" if "rev-parse" in argv else b""

        monkeypatch.setattr(module, "_run_git", fake_run_git)
        assert module._repo_root("/somewhere/else") == "/explicit/repo"
        assert recorded[0][:3] == ["git", "-C", "/somewhere/else"]

    def test_main_reports_unexpected_paths_from_the_anchored_repo(self, monkeypatch, capsys):
        import scripts.verify_staged_scope as module

        monkeypatch.setattr(module, "_repo_root", lambda start=None: "/anchored/repo")
        monkeypatch.setattr(
            module, "_staged_paths", lambda root: ["allowed.py", "sneaky.py"]
        )
        assert module.main(["--allow", "allowed.py"]) == module.EXIT_UNEXPECTED_PATHS
        assert "sneaky.py" in capsys.readouterr().err

    def test_main_passes_the_override_through_to_repo_root(self, monkeypatch):
        import scripts.verify_staged_scope as module

        seen: list[str | None] = []

        def fake_repo_root(start=None):
            seen.append(start)
            return "/anchored/repo"

        monkeypatch.setattr(module, "_repo_root", fake_repo_root)
        monkeypatch.setattr(module, "_staged_paths", lambda root: [])
        assert module.main(["--allow", "a.py", "--repo", "/chosen"]) == module.EXIT_OK
        assert seen == ["/chosen"]
