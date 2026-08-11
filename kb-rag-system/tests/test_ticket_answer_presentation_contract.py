"""Behavioral contract for choosing among recorded answer channels."""

from __future__ import annotations

import json
import subprocess

from api.tickets_console_main import UI_ASSETS_DIRECTORY


def _plan(execution: dict) -> dict:
    module = (UI_ASSETS_DIRECTORY / "answer-presentation.js").resolve().as_uri()
    script = """
const execution = JSON.parse(process.argv[1]);
const moduleUrl = process.argv[2];
const { planAnswerPresentation } = await import(moduleUrl);
process.stdout.write(JSON.stringify(planAnswerPresentation(execution)));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script, json.dumps(execution), module],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_generated_answer_stays_canonical_and_equal_guided_data_is_not_duplicated():
    guided = {
        "opening": "OPEN",
        "key_points": ["POINT"],
        "steps": [{"step_number": 1, "action": "ACT"}],
        "warnings": ["WARN"],
    }
    plan = _plan(
        {
            "generatedAnswer": json.dumps(guided),
            "structuredResponse": {**guided, "outcome_reason": "WHY"},
            "outcomeReason": "WHY",
        }
    )

    assert plan["primary"]["source"] == "generated_answer"
    assert plan["secondaries"] == []
    assert plan["residual"] is None
    assert plan["outcome"]["primary"]["value"] == "WHY"
    assert plan["outcome"]["secondary"] is None
    assert any(ref["source"] == "structured_response.guided" for ref in plan["references"])


def test_divergent_guided_and_outcome_channels_all_survive_with_final_first():
    plan = _plan(
        {
            "generatedAnswer": "FINAL",
            "structuredResponse": {
                "opening": "STRUCTURED",
                "outcome_reason": "WHY-B",
            },
            "outcomeReason": "WHY-A",
        }
    )

    assert plan["primary"]["value"] == "FINAL"
    assert [item["value"] for item in plan["secondaries"]] == [
        {"opening": "STRUCTURED"}
    ]
    assert plan["outcome"]["primary"]["value"] == "WHY-A"
    assert plan["outcome"]["secondary"]["value"] == "WHY-B"


def test_every_wrapper_is_either_presented_once_or_referenced_by_exact_path():
    plan = _plan(
        {
            "generatedAnswer": "B",
            "structuredResponse": {
                "response_to_participant": "A",
                "answer": "B",
                "response": "C",
            },
        }
    )

    assert plan["primary"]["value"] == "B"
    assert [item["value"] for item in plan["secondaries"]] == ["A", "C"]
    references = {item["path"]: item["sameAs"] for item in plan["references"]}
    assert references["$/structured_response/answer"] == "$/generated_answer"
    assert "$/structured_response/response_to_participant" in references
    assert "$/structured_response/response" in references


def test_a_non_null_wrapper_is_the_fallback_but_a_recorded_null_still_survives():
    plan = _plan(
        {
            "generatedAnswer": None,
            "structuredResponse": {
                "response_to_participant": None,
                "answer": "B",
            },
        }
    )

    assert plan["primary"]["source"] == "structured_response.answer"
    assert plan["primary"]["value"] == "B"
    assert [item["value"] for item in plan["secondaries"]] == [None]


def test_an_equivalent_whole_structured_response_is_not_rendered_twice():
    plan = _plan(
        {
            "generatedAnswer": '{"foo":"X"}',
            "structuredResponse": {"foo": "X"},
        }
    )

    assert plan["secondaries"] == []
    assert plan["residual"] is None
    assert any(ref["source"] == "structured_response" for ref in plan["references"])


def test_null_and_empty_generated_answers_remain_distinct_recorded_states():
    absent = _plan({"generatedAnswer": None, "structuredResponse": {}})
    empty = _plan({"generatedAnswer": "", "structuredResponse": {}})

    assert absent["primary"]["recorded"] is False
    assert empty["primary"]["recorded"] is True
    assert empty["primary"]["value"] == ""


def test_a_fallback_channel_never_creates_a_self_reference():
    guided = _plan(
        {
            "generatedAnswer": None,
            "structuredResponse": {"opening": "ONLY-GUIDED"},
        }
    )
    wrapped = _plan(
        {
            "generatedAnswer": None,
            "structuredResponse": {"answer": "ONLY-WRAPPER"},
        }
    )

    assert guided["primary"]["path"] == "$/structured_response"
    assert wrapped["primary"]["path"] == "$/structured_response/answer"
    assert all(
        reference["path"] != reference["sameAs"]
        for plan in (guided, wrapped)
        for reference in plan["references"]
    )
