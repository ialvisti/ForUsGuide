"""
KB data-point ↔ ForusBots alignment regression gate.

Every MUST-HAVE data point whose source_type is participant_profile (i.e. the
article claims it lives in the admin portal) must resolve deterministically via
the catalog's map_slug — or be provided by the handle-ticket request — or be an
explicitly acknowledged exception in KNOWN_UNSCRAPEABLE.

This is the permanent form of the alignment audit (scripts/audit_kb_datapoints.py).
Skipped automatically when the PA/ articles directory is not present.
"""

from __future__ import annotations

import glob
import json
import re
from pathlib import Path

import pytest

from data_pipeline.forusbots_catalog import is_request_provided, map_slug

_ARTICLES_DIR = Path(__file__).resolve().parents[2] / "PA"

pytestmark = pytest.mark.skipif(
    not _ARTICLES_DIR.is_dir(),
    reason="PA/ articles directory not present",
)

# Must-have participant_profile data points acknowledged as not scrapeable.
# Adding an entry here is a DECISION — it means the GR will have to ask for it.
KNOWN_UNSCRAPEABLE: frozenset = frozenset()


def _slugify(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", str(s or "").lower())
    return re.sub(r"[\s\-]+", "_", s).strip("_")


def _iter_must_have_profile_points():
    for path in sorted(glob.glob(str(_ARTICLES_DIR / "**" / "*.json"), recursive=True)):
        if "Tags" in path:
            continue
        doc = json.load(open(path))
        rd = (doc.get("details") or {}).get("required_data") or {}
        for dp in rd.get("must_have") or []:
            if dp.get("source_type") == "participant_profile":
                yield Path(path).name, dp


def test_must_have_profile_points_are_scrapeable_or_request_provided():
    gaps = []
    for fname, dp in _iter_must_have_profile_points():
        slug = _slugify(dp.get("data_point"))
        if slug in KNOWN_UNSCRAPEABLE:
            continue
        item = {"field": slug, "description": dp.get("meaning"),
                "why_needed": dp.get("why_needed")}
        if is_request_provided(item):
            continue
        if map_slug(item, current_year=2026) is None:
            gaps.append(f"{slug}  ({fname})")
    assert not gaps, (
        "must_have participant_profile data points with no deterministic "
        "ForusBots mapping (add a SLUG_MAP alias, fix the article's "
        "source_type, or acknowledge in KNOWN_UNSCRAPEABLE):\n  "
        + "\n  ".join(gaps)
    )


def test_articles_parse_and_have_required_data():
    """Smoke: every article parses and exposes the required_data structure."""
    count = 0
    for path in sorted(glob.glob(str(_ARTICLES_DIR / "**" / "*.json"), recursive=True)):
        if "Tags" in path:
            continue
        doc = json.load(open(path))
        assert isinstance((doc.get("details") or {}).get("required_data"), dict), path
        count += 1
    assert count > 0
