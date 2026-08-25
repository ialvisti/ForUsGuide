"""Static contracts for the production n8n ticket workflow export.

The workflow is deliberately tested as data.  These tests do not execute n8n,
resolve credentials, or inspect pinned participant payloads.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / "n8n_workflow"
MAIN_WORKFLOW = WORKFLOW_DIR / "main.json"
PARTICIPANT_WORKFLOW = WORKFLOW_DIR / "paticipant_search.json"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def main_workflow() -> dict[str, Any]:
    return _load(MAIN_WORKFLOW)


def _node(workflow: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [node for node in workflow["nodes"] if node.get("name") == name]
    assert len(matches) == 1, f"expected one n8n node named {name!r}"
    return matches[0]


def _main_targets(workflow: dict[str, Any], source: str) -> list[str]:
    outputs = workflow["connections"][source]["main"]
    return [edge["node"] for branch in outputs for edge in branch]


def test_export_graphs_have_unique_nodes_and_valid_connections() -> None:
    for path in (MAIN_WORKFLOW, PARTICIPANT_WORKFLOW):
        workflow = _load(path)
        names = [node["name"] for node in workflow["nodes"]]
        node_ids = [node["id"] for node in workflow["nodes"]]
        assert len(names) == len(set(names)), f"{path.name} has duplicate node names"
        assert len(node_ids) == len(set(node_ids)), f"{path.name} has duplicate node ids"

        known = set(names)
        assert set(workflow["connections"]).issubset(known)
        for outputs_by_type in workflow["connections"].values():
            for branches in outputs_by_type.values():
                for branch in branches:
                    for edge in branch:
                        assert edge["node"] in known


def test_exports_are_sanitized_before_they_are_versioned() -> None:
    for path in (MAIN_WORKFLOW, PARTICIPANT_WORKFLOW):
        workflow = _load(path)
        assert workflow.get("pinData") == {}, f"{path.name} retains pinned data"
        assert workflow.get("meta") in (None, {}), f"{path.name} exposes instance metadata"

        serialized = json.dumps(workflow)
        assert re.search(r"Bearer\s+[A-Za-z0-9_.-]{100,}", serialized) is None


def test_devrev_updates_use_only_the_managed_credential(
    main_workflow: dict[str, Any],
) -> None:
    for name in ("Update Ticket", "Update Ticket1", "Update Ticket2"):
        node = _node(main_workflow, name)
        assert node["parameters"]["authentication"] == "genericCredentialType"
        assert node["parameters"]["genericAuthType"] == "httpHeaderAuth"
        assert node["credentials"]["httpHeaderAuth"]["name"] == "DevRev Production"

        explicit_headers = node["parameters"].get("headerParameters", {}).get(
            "parameters", []
        )
        assert all(
            header.get("name", "").casefold() != "authorization"
            for header in explicit_headers
        )


def test_ticket_lookup_is_validated_before_external_work(
    main_workflow: dict[str, Any],
) -> None:
    lookup = _node(main_workflow, "Validate Ticket Lookup")
    code = lookup["parameters"]["jsCode"]
    assert "body?.id" in code
    assert "display" in code.casefold() and "don:" in code
    assert "throw new Error" in code

    assert _main_targets(main_workflow, "Webhook1") == ["Validate Ticket Lookup"]
    assert _main_targets(main_workflow, "Validate Ticket Lookup") == [
        "Get tickets from DevRev1"
    ]

    get_ticket = _node(main_workflow, "Get tickets from DevRev1")
    query_value = get_ticket["parameters"]["queryParameters"]["parameters"][0][
        "value"
    ]
    assert "$json.ticketId" in query_value


def test_canonical_ticket_identity_comes_from_the_devrev_response(
    main_workflow: dict[str, Any],
) -> None:
    get_fields = _node(main_workflow, "Get fields1")
    code = get_fields["parameters"]["jsCode"]
    assert "works.display_id" in code
    assert "works.id" in code
    assert "DevRev response ticket identity" in code
    assert "throw new Error" in code

def test_knowledge_question_rebuilds_the_body_with_canonical_identity(
    main_workflow: dict[str, Any],
) -> None:
    node = _node(main_workflow, "Knowledge Question")
    body = node["parameters"]["jsonBody"]

    assert re.search(r"\bquestion\s*:", body)
    assert re.search(r"\bticket_id\s*:", body)
    assert "$('Parse response').first().json.question" in body
    assert "$('Get fields1').first().json.ticketId" in body
    assert "Knowledge Question Inquiry Generator" not in body
    assert set(node["credentials"]) == {"httpHeaderAuth"}
    assert node["credentials"]["httpHeaderAuth"]["name"] == "RAG KB System"


def test_knowledge_question_retries_with_a_stable_idempotency_key(
    main_workflow: dict[str, Any],
) -> None:
    parser = _node(main_workflow, "Parse response")
    parser_code = parser["parameters"]["jsCode"]
    assert "fnv1a64" in parser_code
    assert "ticketId" in parser_code
    assert "question" in parser_code
    assert "idempotencyKey" in parser_code

    node = _node(main_workflow, "Knowledge Question")
    headers = {
        header["name"].casefold(): header["value"]
        for header in node["parameters"]["headerParameters"]["parameters"]
    }
    assert "idempotency-key" in headers
    assert "$('Parse response').first().json.idempotencyKey" in headers[
        "idempotency-key"
    ]
    assert "$execution.id" not in headers["idempotency-key"]
    assert node["retryOnFail"] is True
    assert node["maxTries"] == 3
    assert node["waitBetweenTries"] >= 2000


def test_knowledge_question_llm_never_receives_ticket_authority(
    main_workflow: dict[str, Any],
) -> None:
    node = _node(main_workflow, "Knowledge Question Inquiry Generator")
    prompt_input = node["parameters"]["text"]
    system_prompt = node["parameters"]["options"]["systemMessage"]

    assert "JSON.stringify($('Code').first().json)" not in prompt_input
    assert '"ticketId"' not in system_prompt
    for forbidden in ("ticketId", "userId", "userName", "userEmail"):
        assert forbidden not in prompt_input
    for required in (
        "emailSubject",
        "emailBody",
        "tag",
        "firstContact",
        "ticket_messages",
    ):
        assert required in prompt_input


def test_handle_ticket_overwrites_identity_and_uses_a_stable_input_fingerprint(
    main_workflow: dict[str, Any],
) -> None:
    node = _node(main_workflow, "Handle Ticket")
    body = node["parameters"]["jsonBody"]
    assert "$('Include Ticket data').item.json.ticketData.ticketId" in body

    envelope = _node(main_workflow, "Include Ticket data")
    envelope_code = envelope["parameters"]["jsCode"]
    assert "$('Get fields1').first().json.ticketId" in envelope_code
    assert "const canonicalTicketData = {" in envelope_code
    assert "...ticketData" in envelope_code
    assert "ticket_id: ticketId" in envelope_code
    assert "ticketData: canonicalTicketData" in envelope_code
    assert "fnv1a64" in envelope_code
    assert "JSON.stringify(envelope)" in envelope_code
    assert "idempotencyKey" in envelope_code

    headers = {
        header["name"].casefold(): header["value"]
        for header in node["parameters"]["headerParameters"]["parameters"]
    }
    assert "idempotency-key" in headers
    assert "$('Include Ticket data').first().json.idempotencyKey" in headers[
        "idempotency-key"
    ]
    assert "$execution.id" not in headers["idempotency-key"]


def test_poll_waits_for_queued_and_running_jobs(
    main_workflow: dict[str, Any],
) -> None:
    node = _node(main_workflow, "Still Running?")
    condition = node["parameters"]["conditions"]["conditions"][0]
    assert "queued" in condition["leftValue"]
    assert "running" in condition["leftValue"]
    assert condition["operator"]["type"] == "boolean"
    assert condition["operator"]["operation"] == "true"


def test_first_async_poll_is_bounded_to_fifteen_seconds(
    main_workflow: dict[str, Any],
) -> None:
    wait = _node(main_workflow, "Wait")
    assert wait["parameters"]["amount"] == 15


def test_async_detection_uses_the_job_handle_contract(
    main_workflow: dict[str, Any],
) -> None:
    node = _node(main_workflow, "Generate response question?")
    condition = node["parameters"]["conditions"]["conditions"][0]
    assert "ticket_job_id" in condition["leftValue"]
    assert "route_taken" not in condition["leftValue"]
    assert condition["operator"]["type"] == "boolean"
    assert condition["operator"]["operation"] == "true"


def test_only_safe_terminal_jobs_reach_the_devrev_formatter(
    main_workflow: dict[str, Any],
) -> None:
    guard = _node(main_workflow, "Safe Participant Reply?")
    conditions = guard["parameters"]["conditions"]["conditions"]
    serialized = json.dumps(conditions)
    assert "succeeded" in serialized
    assert "send_participant_reply" in serialized
    assert "fallback" in serialized

    assert _main_targets(main_workflow, "Still Running?") == [
        "Wait1",
        "Safe Participant Reply?",
    ]
    assert _main_targets(main_workflow, "Safe Participant Reply?") == [
        "Format data for DevRev Internal notes",
        "Stop Unsafe Terminal Result",
    ]

    stop = _node(main_workflow, "Stop Unsafe Terminal Result")
    stop_code = stop["parameters"]["jsCode"]
    assert "throw new Error" in stop_code
    assert "ticket_messages" not in stop_code


def test_inline_ticket_results_use_the_route_aware_multi_inquiry_formatter(
    main_workflow: dict[str, Any],
) -> None:
    assert _main_targets(main_workflow, "Generate response question?") == [
        "Wait",
        "Format data for DevRev Internal notes",
    ]

    formatter = _node(main_workflow, "Format data for DevRev Internal notes")
    code = formatter["parameters"]["jsCode"]
    assert "...(data.related || [])" in code
    assert "knowledge_answer" in code
    assert "needs_more_info_message" in code
    assert "getFields.firstContact" in code

    legacy_formatter = _node(main_workflow, "Format KQ for DevRev")
    assert legacy_formatter.get("disabled") is True
