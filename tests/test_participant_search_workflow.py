import json
import hashlib
from pathlib import Path


WORKFLOW_PATH = (
    Path(__file__).resolve().parents[1] / "flows_n8n" / "participant_search.json"
)

HTTP_NODES = (
    "Search Participant",
    "Get Job",
    "Search Participant1",
    "Get Job1",
    "Scrape Participant",
    "Get Job2",
)

REPLICATE_NODES = ("Replicate Data", "Replicate Data1", "Replicate Data2")

POLL_NODES = ("If Done", "If Done1", "If Done2")

EXPECTED_CONNECTIONS_SHA256 = (
    "6beb0c60214c4780e59a4968e22539e8a5ead83e49643053301cc4da72f93339"
)
EXPECTED_NODE_IDENTITY_SHA256 = (
    "fcb5947b1455f588bdb3e119e1539ccb2d52b768faa47160bee5b1b495385f55"
)


def load_workflow():
    workflow = json.loads(WORKFLOW_PATH.read_text())
    nodes = {node["name"]: node for node in workflow["nodes"]}
    return workflow, nodes


def canonical_sha256(value):
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(serialized).hexdigest()


def test_node_identity_and_connections_remain_unchanged():
    workflow, _ = load_workflow()
    node_identity = sorted(
        (node["id"], node["name"], node["type"]) for node in workflow["nodes"]
    )

    assert len(node_identity) == 45
    assert canonical_sha256(node_identity) == EXPECTED_NODE_IDENTITY_SHA256
    assert canonical_sha256(workflow["connections"]) == EXPECTED_CONNECTIONS_SHA256


def test_forusbots_http_requests_have_finite_timeouts():
    _, nodes = load_workflow()

    for node_name in HTTP_NODES:
        assert nodes[node_name]["parameters"]["options"]["timeout"] == 30_000
        assert nodes[node_name]["retryOnFail"] is True
        assert nodes[node_name]["maxTries"] == 3
        assert nodes[node_name]["waitBetweenTries"] == 2_000


def test_retry_payloads_preserve_the_job_id_contract():
    _, nodes = load_workflow()

    for node_name in REPLICATE_NODES:
        source = nodes[node_name]["parameters"]["jsCode"]
        assert "job_Id" not in source
        assert "jobId" in source


def test_poll_checks_stop_on_terminal_failure_without_extra_nodes():
    _, nodes = load_workflow()

    for node_name in POLL_NODES:
        conditions = nodes[node_name]["parameters"]["conditions"]["conditions"]
        assert len(conditions) == 1
        expression = conditions[0]["leftValue"]
        assert "failed" in expression
        assert "canceled" in expression
        assert "cancelled" in expression
        assert "throw new Error(" in expression


def test_success_checks_are_null_safe_and_match_only_succeeded():
    _, nodes = load_workflow()

    for node_name in POLL_NODES:
        conditions = nodes[node_name]["parameters"]["conditions"]["conditions"]
        assert len(conditions) == 1
        expression = conditions[0]["leftValue"]
        assert "String(" in expression
        assert "??" in expression
        assert "=== 'succeeded'" in expression
        assert ".includes(" not in expression
