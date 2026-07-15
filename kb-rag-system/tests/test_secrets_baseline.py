from __future__ import annotations

from pathlib import Path

import pytest

from scripts.verify_secrets_baseline import verify_baseline


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


def test_cloud_build_compares_fresh_scan_to_reviewed_baseline():
    """El gate no puede comparar dos copias del baseline ya aprobado."""
    root = Path(__file__).resolve().parent.parent
    config = (root / "cloudbuild.yaml").read_text(encoding="utf-8")
    # With ``--baseline``, detect-secrets updates that file in place and emits
    # no JSON to stdout. The candidate must therefore be serialized from the
    # updated disposable copy, not captured from the command's empty stdout.
    assert (
        "python -m json.tool /workspace/scanned-secrets.baseline \\\n"
        "          > /workspace/candidate-secrets.baseline"
    ) in config
    assert (
        "--baseline /workspace/scanned-secrets.baseline \\\n"
        "          > /workspace/candidate-secrets.baseline"
    ) not in config
    assert "--approved /workspace/approved-secrets.baseline" in config
    assert "--candidate /workspace/candidate-secrets.baseline" in config
