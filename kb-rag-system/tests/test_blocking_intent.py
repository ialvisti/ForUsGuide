"""Unit + regression tests for the `blocking_intent` policy refactor.

These tests cover the new behaviour added to
`RAGEngine._apply_informational_outcome_policy`:

1. Unit — each blocking_intent value (always, execution_only,
   personalization_only, eligibility_confirmation) is honoured.
2. Unit — mixed sets of intents respect the strictest item.
3. Regression — load every real article in PA/Distributions and PA/Loans,
   validate their blocking_intent shape and contradictions, then exercise
   the policy with synthetic informational vs. execution scenarios.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
from unittest.mock import Mock, patch


REPO_ROOT = Path(__file__).resolve().parents[2]
PA_DIR = REPO_ROOT / "PA"
ARTICLE_DIRS = [PA_DIR / "Distributions", PA_DIR / "Loans"]
ALL_ARTICLES: List[Path] = sorted(
    p for d in ARTICLE_DIRS if d.exists() for p in d.glob("*.json")
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_router():
    from unittest.mock import AsyncMock

    router = Mock()
    router.call = AsyncMock()
    return router


@pytest.fixture
def engine(mock_router):
    from data_pipeline.rag_engine import RAGEngine

    with patch("data_pipeline.rag_engine.PineconeUploader"):
        return RAGEngine(llm_router=mock_router)


def _must_have_chunk(intents: Dict[str, str]) -> Dict[str, Any]:
    """Build a minimal `required_data_must_have` chunk with the given
    `data_point -> blocking_intent` mapping serialized into the metadata
    field that the chunker now emits."""
    return {
        "id": "chunk-mh",
        "score": 0.9,
        "metadata": {
            "chunk_type": "required_data_must_have",
            "chunk_tier": "critical",
            "article_id": "test-article",
            "must_have_blocking_intents": [
                f"{dp}|{intent}" for dp, intent in intents.items()
            ],
        },
        "content": "must_have chunk",
    }


def _blocked_parsed(missing_labels: List[str]) -> Dict[str, Any]:
    return {
        "outcome": "blocked_missing_data",
        "outcome_reason": "Some data missing.",
        "questions_to_ask": list(missing_labels),
        "data_gaps": [],
        "guardrails_applied": [],
    }


def _profile(intent: str = "informational_options") -> Dict[str, Any]:
    return {"inquiry_intent": intent, "primary_action": "loan"}


# ---------------------------------------------------------------------------
# Unit tests — single intent values
# ---------------------------------------------------------------------------


class TestBlockingIntentUnit:
    def test_execution_only_informational_rescues_to_can_proceed(self, engine):
        chunks = [_must_have_chunk({"Loan amount needed": "execution_only"})]
        parsed, info = engine._apply_informational_outcome_policy(
            parsed=_blocked_parsed(["What loan amount would you like?"]),
            retrieval_profile=_profile("informational_options"),
            collected_data={},
            selected_chunks=chunks,
        )
        assert parsed["outcome"] == "can_proceed"
        assert info["normalized"] is True
        assert info["rescue_path"] == "blocking_intent"
        assert info["blocking_intent_overrides"]
        assert any(
            "execution_only" in g.lower() or "personalization_only" in g.lower()
            for g in parsed["guardrails_applied"]
        )

    def test_personalization_only_informational_rescues(self, engine):
        chunks = [_must_have_chunk({"Participant date of birth": "personalization_only"})]
        parsed, info = engine._apply_informational_outcome_policy(
            parsed=_blocked_parsed(["What is your date of birth?"]),
            retrieval_profile=_profile("informational_options"),
            collected_data={},
            selected_chunks=chunks,
        )
        assert parsed["outcome"] == "can_proceed"
        assert info["rescue_path"] == "blocking_intent"

    def test_always_informational_keeps_block(self, engine):
        chunks = [_must_have_chunk({
            "Date the rollover funds were received": "always",
        })]
        parsed, info = engine._apply_informational_outcome_policy(
            parsed=_blocked_parsed(["When did you receive the rollover funds?"]),
            retrieval_profile=_profile("informational_options"),
            collected_data={},
            selected_chunks=chunks,
        )
        assert parsed["outcome"] == "blocked_missing_data"
        assert info["normalized"] is False
        assert "always" in (info["reason"] or "")

    def test_eligibility_confirmation_keeps_block(self, engine):
        chunks = [_must_have_chunk({"Employment status": "eligibility_confirmation"})]
        parsed, info = engine._apply_informational_outcome_policy(
            parsed=_blocked_parsed(["What is your employment status?"]),
            retrieval_profile=_profile("informational_options"),
            collected_data={},
            selected_chunks=chunks,
        )
        assert parsed["outcome"] == "blocked_missing_data"
        assert info["normalized"] is False
        assert "eligibility_confirmation" in (info["reason"] or "")

    def test_execution_only_with_execution_intent_keeps_block(self, engine):
        chunks = [_must_have_chunk({"Loan amount needed": "execution_only"})]
        parsed, info = engine._apply_informational_outcome_policy(
            parsed=_blocked_parsed(["What loan amount would you like?"]),
            retrieval_profile=_profile("execution"),
            collected_data={},
            selected_chunks=chunks,
        )
        # Non-informational intents never trigger a rescue.
        assert parsed["outcome"] == "blocked_missing_data"
        assert info["normalized"] is False


# ---------------------------------------------------------------------------
# Unit tests — mixed intents
# ---------------------------------------------------------------------------


class TestBlockingIntentMixed:
    def test_mixed_execution_and_personalization_rescues(self, engine):
        chunks = [_must_have_chunk({
            "Loan amount needed": "execution_only",
            "Participant age": "personalization_only",
        })]
        parsed, info = engine._apply_informational_outcome_policy(
            parsed=_blocked_parsed([
                "What loan amount would you like?",
                "What is your age?",
            ]),
            retrieval_profile=_profile("informational_options"),
            collected_data={},
            selected_chunks=chunks,
        )
        assert parsed["outcome"] == "can_proceed"
        assert info["rescue_path"] == "blocking_intent"

    def test_mixed_execution_and_always_keeps_block(self, engine):
        chunks = [_must_have_chunk({
            "Loan amount needed": "execution_only",
            "Date the rollover funds were received": "always",
        })]
        parsed, info = engine._apply_informational_outcome_policy(
            parsed=_blocked_parsed([
                "What loan amount would you like?",
                "When did you receive the rollover funds?",
            ]),
            retrieval_profile=_profile("informational_options"),
            collected_data={},
            selected_chunks=chunks,
        )
        assert parsed["outcome"] == "blocked_missing_data"
        assert info["normalized"] is False
        assert info["rescue_path"] is None or info["rescue_path"] != "blocking_intent"

    def test_mixed_execution_and_eligibility_confirmation_keeps_block(self, engine):
        chunks = [_must_have_chunk({
            "Loan amount needed": "execution_only",
            "Employment status": "eligibility_confirmation",
        })]
        parsed, info = engine._apply_informational_outcome_policy(
            parsed=_blocked_parsed([
                "What loan amount would you like?",
                "What is your employment status?",
            ]),
            retrieval_profile=_profile("informational_options"),
            collected_data={},
            selected_chunks=chunks,
        )
        assert parsed["outcome"] == "blocked_missing_data"
        assert info["normalized"] is False


# ---------------------------------------------------------------------------
# Unit tests — chunk metadata extraction edge cases
# ---------------------------------------------------------------------------


class TestExtractMustHaveBlockingIntents:
    def test_empty_chunks_returns_empty(self, engine):
        assert engine._extract_must_have_blocking_intents([]) == {}
        assert engine._extract_must_have_blocking_intents(None) == {}

    def test_ignores_non_must_have_chunks(self, engine):
        chunks = [{
            "metadata": {
                "chunk_type": "decision_guide",
                "must_have_blocking_intents": ["x|always"],
            },
        }]
        assert engine._extract_must_have_blocking_intents(chunks) == {}

    def test_handles_invalid_intent_as_always(self, engine):
        chunks = [_must_have_chunk({"Foo": "made_up_intent"})]
        intents = engine._extract_must_have_blocking_intents(chunks)
        assert intents == {"Foo": "always"}

    def test_strictest_intent_wins_across_chunks(self, engine):
        chunks = [
            _must_have_chunk({"Foo": "execution_only"}),
            _must_have_chunk({"Foo": "always"}),
        ]
        intents = engine._extract_must_have_blocking_intents(chunks)
        assert intents["Foo"] == "always"


# ---------------------------------------------------------------------------
# Unit tests — legacy LT-termination rescue path is preserved
# ---------------------------------------------------------------------------


class TestLegacyLtTerminationRescue:
    def test_legacy_path_runs_when_no_blocking_intent_metadata(self, engine):
        """Articles not yet re-ingested should still benefit from the
        original LT-termination rescue."""
        parsed = _blocked_parsed(["Please provide your full name and email address."])
        retrieval = {
            "inquiry_intent": "informational_options",
            "primary_action": "termination_distribution",
        }
        collected_data = {
            "participant_data": {
                "employment_status": "Terminated",
                "termination_date": "2025-01-01",
                "total_vested_balance": 25000,
            },
            "plan_data": {"blackout_period": False},
        }
        out, info = engine._apply_informational_outcome_policy(
            parsed=parsed,
            retrieval_profile=retrieval,
            collected_data=collected_data,
            selected_chunks=[],  # No must_have chunks → legacy path eligible
        )
        assert out["outcome"] == "can_proceed"
        assert info["rescue_path"] == "legacy_lt_termination"


# ---------------------------------------------------------------------------
# Schema regression tests over the 15 real articles
# ---------------------------------------------------------------------------


class TestArticleSchemaRegression:
    """Validate that every PA/Distributions and PA/Loans article matches the
    invariants the engine assumes:

    - every must_have item has a valid blocking_intent
    - no missing_data_condition references a nice_to_have item
    - every missing_data_condition references a known data_point
    """

    @pytest.mark.parametrize("article_path", ALL_ARTICLES, ids=[p.name for p in ALL_ARTICLES])
    def test_article_invariants(self, article_path: Path):
        with article_path.open() as fh:
            data = json.load(fh)
        details = data["details"]
        rd = details["required_data"]
        dg = details["decision_guide"]
        must = rd.get("must_have", [])
        nice = rd.get("nice_to_have", [])
        must_names = {m["data_point"] for m in must}
        nice_names = {n["data_point"] for n in nice}

        # Every must_have item must declare a valid blocking_intent.
        valid = {"always", "execution_only", "personalization_only", "eligibility_confirmation"}
        for item in must:
            assert "blocking_intent" in item, (
                f"must_have item {item.get('data_point')!r} in {article_path.name} "
                "is missing required field 'blocking_intent'"
            )
            assert item["blocking_intent"] in valid, (
                f"must_have item {item.get('data_point')!r} has invalid "
                f"blocking_intent={item['blocking_intent']!r}"
            )

        # nice_to_have items must NOT carry blocking_intent.
        for item in nice:
            assert "blocking_intent" not in item, (
                f"nice_to_have item {item.get('data_point')!r} in {article_path.name} "
                "must not declare blocking_intent"
            )

        # No contradiction: missing_data_conditions cannot reference nice_to_have.
        contradictions = [
            c["missing_data_point"]
            for c in dg.get("missing_data_conditions", [])
            if c["missing_data_point"] in nice_names
        ]
        assert not contradictions, (
            f"{article_path.name} has missing_data_conditions referencing "
            f"nice_to_have items: {contradictions}"
        )

        # Every missing_data_condition data point should map to a known field.
        unmapped = [
            c["missing_data_point"]
            for c in dg.get("missing_data_conditions", [])
            if c["missing_data_point"] not in must_names
            and c["missing_data_point"] not in nice_names
        ]
        assert not unmapped, (
            f"{article_path.name} missing_data_conditions reference unknown fields: {unmapped}"
        )

        # The blocked_missing_data response_frame must reuse exactly the
        # `ask_participant` strings from missing_data_conditions, in order
        # (skipping nulls and dedup'ing). This keeps the frame the agent reads
        # consistent with the formal blocking conditions in the decision_guide.
        bmd_frame = (
            details.get("response_frames", {}).get("blocked_missing_data", {}) or {}
        )
        actual_questions = list(bmd_frame.get("questions_to_ask", []) or [])
        seen: set = set()
        expected_questions: List[str] = []
        for cond in dg.get("missing_data_conditions", []):
            ask = cond.get("ask_participant")
            if ask and ask not in seen:
                seen.add(ask)
                expected_questions.append(ask)
        assert actual_questions == expected_questions, (
            f"{article_path.name}: response_frames.blocked_missing_data.questions_to_ask "
            f"is not aligned with missing_data_conditions[*].ask_participant.\n"
            f"  actual:   {actual_questions}\n"
            f"  expected: {expected_questions}"
        )


# ---------------------------------------------------------------------------
# Regression — informational vs execution simulation per article
# ---------------------------------------------------------------------------


class TestRealArticleInformationalRescue:
    """For each real article that has at least one must_have item, exercise
    the policy as if every must_have were missing. Articles whose must_have
    items are ALL execution_only or personalization_only should rescue under
    informational intent and stay blocked under execution intent. Articles
    with any always / eligibility_confirmation must stay blocked in both
    cases."""

    @pytest.mark.parametrize("article_path", ALL_ARTICLES, ids=[p.name for p in ALL_ARTICLES])
    def test_informational_vs_execution(self, engine, article_path: Path):
        with article_path.open() as fh:
            data = json.load(fh)
        must = data["details"]["required_data"].get("must_have", [])
        if not must:
            pytest.skip("Article has no must_have items")

        intents = {item["data_point"]: item["blocking_intent"] for item in must}
        chunks = [_must_have_chunk(intents)]
        missing_labels = list(intents.keys())
        parsed = _blocked_parsed(missing_labels)

        all_rescuable = all(
            intent in {"execution_only", "personalization_only"}
            for intent in intents.values()
        )

        # Informational intent
        out_info, info_info = engine._apply_informational_outcome_policy(
            parsed=dict(parsed),
            retrieval_profile=_profile("informational_options"),
            collected_data={},
            selected_chunks=chunks,
        )
        if all_rescuable:
            assert out_info["outcome"] == "can_proceed", (
                f"{article_path.name}: all must_have are rescuable but informational"
                f" outcome was {out_info['outcome']}"
            )
            assert info_info["rescue_path"] == "blocking_intent"
        else:
            assert out_info["outcome"] == "blocked_missing_data", (
                f"{article_path.name}: at least one must_have is non-rescuable"
                f" but informational outcome was rescued anyway"
            )

        # Execution intent — must always keep the block when items are missing.
        out_exec, _ = engine._apply_informational_outcome_policy(
            parsed=dict(parsed),
            retrieval_profile=_profile("execution"),
            collected_data={},
            selected_chunks=chunks,
        )
        assert out_exec["outcome"] == "blocked_missing_data", (
            f"{article_path.name}: execution intent should never be rescued by"
            f" the blocking_intent path but got {out_exec['outcome']}"
        )
