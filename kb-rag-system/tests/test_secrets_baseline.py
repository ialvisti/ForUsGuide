from __future__ import annotations

from pathlib import Path

import pytest

from scripts.verify_secrets_baseline import verify_baseline, verify_no_findings


def _baseline(results=None):
    return {
        "version": "1.5.0",
        "plugins_used": [{"name": "KeywordDetector"}],
        "filters_used": [],
        "results": results or {},
    }


def test_unchanged_reviewed_secret_baseline_passes():
    approved = _baseline({
        "fixture.txt": [{"type": "Secret Keyword", "hashed_secret": "h1"}]
    })
    candidate = _baseline({
        "fixture.txt": [{
            "type": "Secret Keyword", "hashed_secret": "h1", "line_number": 99
        }]
    })
    verify_baseline(approved, candidate)


def test_new_secret_finding_fails_closed_without_printing_hash():
    candidate = _baseline({
        "app.py": [{"type": "Secret Keyword", "hashed_secret": "sensitive-hash"}]
    })
    with pytest.raises(ValueError) as exc:
        verify_baseline(_baseline(), candidate)
    assert "app.py" in str(exc.value)
    assert "sensitive-hash" not in str(exc.value)


@pytest.mark.parametrize("bad_results", [None, [], "truncated"])
def test_invalid_candidate_results_shape_fails_closed(bad_results):
    candidate = _baseline()
    candidate["results"] = bad_results
    with pytest.raises(ValueError, match="results"):
        verify_baseline(_baseline(), candidate)


def test_missing_candidate_results_fails_closed():
    candidate = _baseline()
    candidate.pop("results")
    with pytest.raises(ValueError, match="results"):
        verify_baseline(_baseline(), candidate)


@pytest.mark.parametrize("field", ["version", "plugins_used", "filters_used"])
def test_missing_configuration_fails_even_when_both_documents_match(field):
    approved = _baseline()
    candidate = _baseline()
    approved.pop(field)
    candidate.pop(field)
    with pytest.raises(ValueError, match=field):
        verify_baseline(approved, candidate)


def test_duplicate_of_reviewed_value_in_same_file_is_a_new_finding():
    finding = {"type": "Secret Keyword", "hashed_secret": "h1"}
    approved = _baseline({"fixture.txt": [finding]})
    candidate = _baseline({"fixture.txt": [
        {**finding, "line_number": 1},
        {**finding, "line_number": 2},
    ]})
    with pytest.raises(ValueError, match="new secret findings"):
        verify_baseline(approved, candidate)


def test_scoped_scan_rejects_disappeared_finding_for_present_file(tmp_path):
    (tmp_path / "fixture.txt").write_text("fixture", encoding="utf-8")
    approved = _baseline({
        "fixture.txt": [{"type": "Secret Keyword", "hashed_secret": "h1"}],
    })
    with pytest.raises(ValueError, match="disappeared"):
        verify_baseline(approved, _baseline(), scan_root=tmp_path)


def test_scoped_scan_allows_reviewed_path_absent_from_subset(tmp_path):
    approved = _baseline({
        "not-in-image.txt": [{"type": "Secret Keyword", "hashed_secret": "h1"}],
    })
    verify_baseline(approved, _baseline(), scan_root=tmp_path)


def test_external_input_scan_requires_zero_findings():
    verify_no_findings(_baseline())
    candidate = _baseline({
        "External agents/prompt.md": [{
            "type": "Secret Keyword",
            "hashed_secret": "external-fixture",
        }],
    })
    with pytest.raises(ValueError, match="external input") as exc:
        verify_no_findings(candidate)
    assert "external-fixture" not in str(exc.value)


def test_cloud_build_compares_fresh_scan_to_reviewed_baseline():
    """El gate no puede comparar dos copias del baseline ya aprobado."""
    root = Path(__file__).resolve().parent.parent
    config = (root / "cloudbuild.yaml").read_text(encoding="utf-8")
    assert "--no-verify" in config
    assert "--baseline" not in config
    for pattern in (
        r"\.venv/.*",
        r"\.pytest_cache/.*",
        r"\.mypy_cache/.*",
        r"\.ruff_cache/.*",
        r"^\.secrets\.baseline$",
        "__pycache__/.*",
        "rag-testing/stress_test_results.*",
    ):
        assert f"--exclude-files '{pattern}'" in config
    assert "--approved .secrets.baseline" in config
    assert "--candidate /workspace/candidate-secrets.baseline" in config
    assert "--scan-root ." in config
    assert "external-input-secrets.json" in config
    assert "../PA" in config
    assert '"../External agents"' in config
    assert "../docs/verification/handle-ticket/11-incident-drill-template.md" in config
    assert "--require-empty" in config
