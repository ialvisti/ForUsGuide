"""
Prompt parity guard.

The runtime agent prompts are packaged under data_pipeline/agent_prompts/. Their
source of truth is the `External agents/*.md` specs the domain team tunes. This
test fails when the two drift, forcing a re-sync (the field-mapper is allowed to
be a SUPERSET because Stage 3 reconciled in Rule 10 + aliases from the newer
Module Builder V2 spec).

Skipped automatically when the `External agents/` spec directory is not present
(e.g. CI that ships only kb-rag-system/).
"""

from __future__ import annotations

from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_PACKAGED_DIR = _TESTS_DIR.parent / "data_pipeline" / "agent_prompts"
_SPECS_DIR = _TESTS_DIR.parents[1] / "External agents"

# packaged stem -> canonical spec filename
_VERBATIM = {
    "extract_inquiries": "Inquiry Extraction & Required-Data Builder agent .md",
    "kb_question_synthesis": "Knowledge Question Inquiry Generator.md",
    "gr_body_build": "Generate Response Body Builder.md",
}
_FIELD_MAP_STEM = "forusbots_field_map"
_FIELD_MAP_SPEC = "Forusbots field mapper.md"

pytestmark = pytest.mark.skipif(
    not _SPECS_DIR.exists(),
    reason="External agents/ spec directory not present",
)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


@pytest.mark.parametrize("stem,spec", list(_VERBATIM.items()))
def test_verbatim_prompts_match_spec(stem, spec):
    packaged = _read(_PACKAGED_DIR / f"{stem}.md")
    canonical = _read(_SPECS_DIR / spec)
    assert packaged == canonical, (
        f"{stem}.md drifted from 'External agents/{spec}'. "
        f"Re-copy the spec into data_pipeline/agent_prompts/{stem}.md."
    )


def test_field_map_contains_spec_and_reconciliation():
    packaged = _read(_PACKAGED_DIR / f"{_FIELD_MAP_STEM}.md")
    canonical = _read(_SPECS_DIR / _FIELD_MAP_SPEC)
    # the full canonical spec must be present (field-mapper is a superset)
    assert canonical.rstrip() in packaged, (
        "forusbots_field_map.md no longer contains the full canonical spec."
    )
    # the reconciled additions from Module Builder V2 must be present
    for anchor in (
        "Rule 10: Predicate Decomposition",
        "employer_match_vested_balance",
        "participant_s_name",
        "rollover_balance",
        # runtime addendum (Rules 11-14 incl. the plan-module catalog)
        "RUNTIME ADDENDUM",
        "Rule 14",
        "plan_design",
        "CURRENT YEAR",
    ):
        assert anchor in packaged, f"missing reconciled anchor: {anchor!r}"
