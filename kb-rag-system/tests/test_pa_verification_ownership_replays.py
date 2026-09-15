"""Semantic acceptance of real PA replays, never mocked model responses.

Set PA_OWNERSHIP_REPLAY_DIR to a sanitized replay directory. These checks run
against saved HTTP or local-engine responses; they do not send participant
messages, create reviews or execute a model themselves.
"""
import json
import os
from pathlib import Path
import re

import pytest


pytestmark = pytest.mark.live_dependencies


def replay(case):
    directory = os.environ.get("PA_OWNERSHIP_REPLAY_DIR")
    if not directory:
        pytest.skip("Requires explicitly supplied real replay evidence")
    row = json.loads((Path(directory) / f"{case}.json").read_text())
    assert row.get("execution_error") is None
    assert row.get("fixture_kind") == "simulation"
    assert row.get("http_status", 200) == 200
    assert row.get("provenance"), "Replay provenance must be retained"
    return row["result"]["response"]


@pytest.mark.parametrize("case", ["TKT-909642", "TKT-910571", "TKT-912180"])
def test_missing_record_facts_have_internal_owner(case):
    response = replay(case)
    questions = " ".join(q["question"] for q in response["questions_to_ask"])
    assert not re.search(r"vested|after.tax|source balance|blackout", questions, re.I), (
        "Missing authoritative record facts must not become participant questions"
    )
    participant = response["response_to_participant"]
    for step in participant.get("steps", []):
        text = " ".join(str(step.get(k) or "") for k in ("action", "detail"))
        assert not re.search(
            r"(?:check|verify|confirm)\s+(?:your|the current|the total|the)\s+(?:total\s+)?vested balance",
            text, re.I,
        ), "Do not move an internal verification question into participant steps"
    assert response["outcome"] == "blocked_missing_data"
    assert response["escalation"]["needed"] is True
    assert "vested" in response["escalation"]["reason"].lower()
    text = json.dumps(participant).lower()
    assert not re.search(r"we (?:have )?(?:verified|confirmed) your (?:total )?vested", text)


def test_participant_can_still_clarify_unknown_destination_type():
    response = replay("SIM-BROKERAGE")
    questions = " ".join(q["question"] for q in response["questions_to_ask"]).lower()
    assert response["outcome"] == "blocked_missing_data"
    assert "taxable" in questions and ("retirement" in questions or "ira" in questions)


def test_participant_can_still_report_missing_separation_date():
    response = replay("SIM-SEPARATION")
    questions = " ".join(q["question"] for q in response["questions_to_ask"]).lower()
    assert response["outcome"] == "blocked_missing_data"
    assert re.search(r"last day|(?:termination|separation|employment).*date|when.*(?:leave|stop|end)", questions)
    assert response["escalation"]["needed"] is True
