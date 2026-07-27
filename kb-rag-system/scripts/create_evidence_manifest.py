"""Construcción canónica del evidence manifest de una release."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from create_plan_manifest import generation_from_uri

ARTIFACT_NAMES = (
    "ci_provenance",
    "sbom",
    "scan",
    "staging_revisions",
    "e2e",
    "differential",
    "semantic_review",
    "rollback",
)

BASE_REQUIRED_FIELDS = (
    "evidence_sha",
    "main_sha",
    "image_digest",
    "controller_builder_digest",
    *(field for name in ARTIFACT_NAMES for field in (
        f"{name}_uri", f"{name}_hash",
    )),
)
REQUIRED_FIELDS = (*BASE_REQUIRED_FIELDS, "artifact_claims")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_IMAGE_DIGEST_RE = re.compile(r"^\S+@sha256:[0-9a-f]{64}$")
_CVE_RE = re.compile(r"^CVE-[0-9]{4}-[0-9]{4,}$")
_SEVERITIES = {
    "CRITICAL", "HIGH", "MEDIUM", "LOW", "MINIMAL", "NEGLIGIBLE",
    "UNKNOWN",
}

SemanticAttestationVerifier = Callable[
    [Mapping[str, Any], Mapping[str, Any]], None,
]
VulnerabilityAttestationVerifier = Callable[[Mapping[str, Any]], None]


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} debe ser objeto JSON")
    return value


def _require_nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} debe ser texto no vacío")
    return value


def _require_nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} debe ser entero no negativo")
    return value


def _validate_ci_provenance(result: Mapping[str, Any], **lineage: str) -> dict:
    build_id = _require_nonempty_string(
        result.get("build_id"), "ci_provenance.result.build_id",
    )
    if result.get("provenance_verified") is not True:
        raise ValueError("ci_provenance provenance_verified no es true")
    if result.get("source_commit") != lineage["main_sha"]:
        raise ValueError("ci_provenance source_commit distinto")
    if result.get("subject_digest") != lineage["image_digest"]:
        raise ValueError("ci_provenance subject_digest distinto")
    return {
        "build_id": build_id,
        "provenance_verified": True,
        "source_commit": lineage["main_sha"],
        "subject_digest": lineage["image_digest"],
    }


def _validate_sbom(result: Mapping[str, Any], **lineage: str) -> dict:
    format_name = result.get("format")
    if format_name not in {"spdx-json", "cyclonedx-json"}:
        raise ValueError("sbom.result.format no soportado")
    namespace = _require_nonempty_string(
        result.get("document_namespace"), "sbom.result.document_namespace",
    )
    if result.get("subject_digest") != lineage["image_digest"]:
        raise ValueError("sbom subject_digest distinto")
    package_count = _require_nonnegative_int(
        result.get("package_count"), "sbom.result.package_count",
    )
    if package_count == 0:
        raise ValueError("sbom sin paquetes")
    return {
        "format": format_name,
        "document_namespace": namespace,
        "subject_digest": lineage["image_digest"],
        "package_count": package_count,
    }


def _validate_scan(result: Mapping[str, Any], **lineage: str) -> dict:
    if result.get("policy_passed") is not True:
        raise ValueError("scan policy_passed no es true")
    if result.get("subject_digest") != lineage["image_digest"]:
        raise ValueError("scan subject_digest distinto")
    raw_counts = _require_object(
        result.get("severity_counts"), "scan.result.severity_counts",
    )
    if not raw_counts or any(key not in _SEVERITIES for key in raw_counts):
        raise ValueError("scan severity_counts inválido")
    counts = {
        key: _require_nonnegative_int(value, f"scan severity {key}")
        for key, value in raw_counts.items()
    }
    if counts.get("CRITICAL", 0):
        raise ValueError("scan contiene CRITICAL")

    approvals = result.get("high_approvals")
    if not isinstance(approvals, list):
        raise ValueError("scan high_approvals debe ser lista")
    approved_ids: list[str] = []
    normalized_approvals: list[dict[str, str]] = []
    for approval in approvals:
        item = _require_object(approval, "scan high approval")
        if set(item) != {"vulnerability_id", "approval_hash"}:
            raise ValueError("scan high approval con campos inválidos")
        vulnerability_id = str(item.get("vulnerability_id", "")).upper()
        if _CVE_RE.fullmatch(vulnerability_id) is None:
            raise ValueError("scan high approval sin CVE válido")
        approval_hash = str(item.get("approval_hash", ""))
        if _SHA256_RE.fullmatch(approval_hash) is None:
            raise ValueError("scan high approval sin hash válido")
        approved_ids.append(vulnerability_id)
        normalized_approvals.append({
            "vulnerability_id": vulnerability_id,
            "approval_hash": approval_hash,
        })
    if len(approved_ids) != len(set(approved_ids)):
        raise ValueError("scan high approvals duplicadas")
    if counts.get("HIGH", 0) != len(approved_ids):
        raise ValueError("scan contiene HIGH no aprobadas")
    return {
        "policy_passed": True,
        "subject_digest": lineage["image_digest"],
        "severity_counts": dict(sorted(counts.items())),
        "high_approvals": sorted(
            normalized_approvals, key=lambda item: item["vulnerability_id"],
        ),
        "approved_high_count": len(approved_ids),
    }


def _validate_staging_revisions(
    result: Mapping[str, Any], **lineage: str,
) -> dict:
    phase = result.get("release_phase")
    if phase not in {"disabled", "dark_no_traffic", "shadow"}:
        raise ValueError("staging_revisions release_phase no segura")
    raw_services = _require_object(
        result.get("services"), "staging_revisions.result.services",
    )
    if not raw_services:
        raise ValueError("staging_revisions sin servicios")
    services: dict[str, dict[str, Any]] = {}
    for name, raw_service in raw_services.items():
        service_name = _require_nonempty_string(
            name, "staging_revisions service name",
        )
        service = _require_object(
            raw_service, f"staging_revisions service {service_name}",
        )
        revision = _require_nonempty_string(
            service.get("revision"),
            f"staging_revisions {service_name}.revision",
        )
        if service.get("image_digest") != lineage["image_digest"]:
            raise ValueError(
                f"staging_revisions {service_name} image_digest distinto"
            )
        if service.get("ready") is not True:
            raise ValueError(f"staging_revisions {service_name} no ready")
        services[service_name] = {
            "revision": revision,
            "image_digest": lineage["image_digest"],
            "ready": True,
        }
    return {"release_phase": phase, "services": dict(sorted(services.items()))}


def _validate_e2e(result: Mapping[str, Any], **_lineage: str) -> dict:
    collected = _require_nonnegative_int(
        result.get("tests_collected"), "e2e tests_collected",
    )
    passed = _require_nonnegative_int(
        result.get("tests_passed"), "e2e tests_passed",
    )
    failed = _require_nonnegative_int(
        result.get("tests_failed"), "e2e tests_failed",
    )
    skipped = _require_nonnegative_int(
        result.get("tests_skipped"), "e2e tests_skipped",
    )
    if collected == 0 or passed != collected or failed or skipped:
        raise ValueError("e2e no pasó íntegramente")
    return {
        "tests_collected": collected,
        "tests_passed": passed,
        "tests_failed": failed,
        "tests_skipped": skipped,
    }


def _validate_differential(result: Mapping[str, Any], **_lineage: str) -> dict:
    cases = _require_nonnegative_int(
        result.get("cases"), "differential cases",
    )
    if cases == 0 or result.get("passed") is not True:
        raise ValueError("differential status no-pass")
    if result.get("failures") != []:
        raise ValueError("differential contiene failures")
    if result.get("semantic_quality_verified") is not False:
        raise ValueError(
            "differential no puede autoafirmar semantic_quality_verified"
        )
    metrics = _require_object(
        result.get("metrics"), "differential.result.metrics",
    )
    safe_metrics = (
        "unsafe_publish_rate",
        "missing_inquiry_rate",
        "idempotency_replay_failure_rate",
        "unexplained_poll_404_rate",
    )
    for metric in safe_metrics:
        value = metrics.get(metric)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"differential metric {metric} inválida")
        if value != 0:
            raise ValueError(f"differential metric {metric} no segura")
    replay_observation_rate = metrics.get(
        "idempotency_replay_observation_rate"
    )
    if isinstance(replay_observation_rate, bool) \
            or not isinstance(replay_observation_rate, (int, float)) \
            or replay_observation_rate != 1.0:
        raise ValueError(
            "differential metric idempotency_replay_observation_rate debe ser 1.0"
        )
    exact_match_rate = metrics.get("deterministic_exact_match_rate")
    if isinstance(exact_match_rate, bool) \
            or not isinstance(exact_match_rate, (int, float)) \
            or exact_match_rate != 1.0:
        raise ValueError(
            "differential metric deterministic_exact_match_rate debe ser 1.0"
        )
    lexical_min = metrics.get("reviewed_lexical_coverage_min")
    if isinstance(lexical_min, bool) \
            or not isinstance(lexical_min, (int, float)) \
            or not 0.95 <= lexical_min <= 1.0:
        raise ValueError(
            "differential metric reviewed_lexical_coverage_min debe ser >= 0.95"
        )
    semantic_evaluator = _require_object(
        result.get("semantic_evaluator"), "differential semantic_evaluator",
    )
    if set(semantic_evaluator) != {"method", "rubric_set_sha256"} \
            or semantic_evaluator.get("method") != "reviewed-lexical-rubric-v1" \
            or not isinstance(semantic_evaluator.get("rubric_set_sha256"), str) \
            or _SHA256_RE.fullmatch(semantic_evaluator["rubric_set_sha256"]) is None:
        raise ValueError("differential semantic_evaluator no es smoke lexical revisado")
    reply_set_sha256 = result.get("reply_set_sha256")
    if not isinstance(reply_set_sha256, str) \
            or _SHA256_RE.fullmatch(reply_set_sha256) is None:
        raise ValueError("differential reply_set_sha256 inválido")
    raw_per_case = result.get("per_case")
    if not isinstance(raw_per_case, list) or len(raw_per_case) != cases:
        raise ValueError("differential per_case no coincide con cases")
    reply_rows: list[dict[str, str]] = []
    case_ids: set[str] = set()
    for raw in raw_per_case:
        item = _require_object(raw, "differential per_case item")
        case_id = _require_nonempty_string(
            item.get("case_id"), "differential per_case.case_id",
        )
        if case_id in case_ids:
            raise ValueError("differential per_case contiene case_id duplicado")
        case_ids.add(case_id)
        row = {"case_id": case_id}
        for key in ("legacy_reply_sha256", "v2_reply_sha256"):
            value = item.get(key)
            if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
                raise ValueError(f"differential per_case.{key} inválido")
            row[key] = value
        reply_rows.append(row)
    recomputed_reply_set = hashlib.sha256(json.dumps(
        reply_rows, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    if recomputed_reply_set != reply_set_sha256:
        raise ValueError("differential reply_set_sha256 no coincide con per_case")
    return {
        "cases": cases,
        "passed": True,
        "failures": [],
        "semantic_quality_verified": False,
        "semantic_evaluator": dict(semantic_evaluator),
        "reply_set_sha256": reply_set_sha256,
        "per_case": reply_rows,
        "metrics": {
            **{metric: metrics[metric] for metric in safe_metrics},
            "idempotency_replay_observation_rate": replay_observation_rate,
            "deterministic_exact_match_rate": exact_match_rate,
            "reviewed_lexical_coverage_min": lexical_min,
        },
    }


def _validate_semantic_review(
    result: Mapping[str, Any], **_lineage: str,
) -> dict:
    required = {
        "semantic_quality_verified", "verdict", "review_type",
        "reviewer_identity_sha256", "reviewed_at", "reviewed_case_count",
        "rubric_set_sha256", "reply_set_sha256", "differential_uri",
    }
    if set(result) != required:
        raise ValueError("semantic_review contiene campos inválidos")
    if result.get("semantic_quality_verified") is not True \
            or result.get("verdict") != "pass":
        raise ValueError("semantic_review no contiene un veredicto pass")
    review_type = result.get("review_type")
    if review_type not in {"human", "independent"}:
        raise ValueError("semantic_review review_type inválido")
    reviewer_hash = result.get("reviewer_identity_sha256")
    rubric_hash = result.get("rubric_set_sha256")
    reply_hash = result.get("reply_set_sha256")
    if any(
        not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None
        for value in (reviewer_hash, rubric_hash, reply_hash)
    ):
        raise ValueError("semantic_review contiene hashes inválidos")
    reviewed_at = result.get("reviewed_at")
    if not isinstance(reviewed_at, str) or re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
        reviewed_at,
    ) is None:
        raise ValueError("semantic_review reviewed_at inválido")
    reviewed_cases = _require_nonnegative_int(
        result.get("reviewed_case_count"),
        "semantic_review reviewed_case_count",
    )
    if reviewed_cases == 0:
        raise ValueError("semantic_review reviewed_case_count debe ser positivo")
    differential_uri = _require_nonempty_string(
        result.get("differential_uri"), "semantic_review differential_uri",
    )
    generation_from_uri(differential_uri)
    return {
        "semantic_quality_verified": True,
        "verdict": "pass",
        "review_type": review_type,
        "reviewer_identity_sha256": reviewer_hash,
        "reviewed_at": reviewed_at,
        "reviewed_case_count": reviewed_cases,
        "rubric_set_sha256": rubric_hash,
        "reply_set_sha256": reply_hash,
        "differential_uri": differential_uri,
    }


def _validate_rollback(result: Mapping[str, Any], **lineage: str) -> dict:
    exercise = _require_nonempty_string(
        result.get("exercise"), "rollback.result.exercise",
    )
    if result.get("rollback_succeeded") is not True:
        raise ValueError("rollback no fue exitoso")
    if result.get("candidate_image_digest") != lineage["image_digest"]:
        raise ValueError("rollback candidate_image_digest distinto")
    if result.get("candidate_traffic_percent") != 0:
        raise ValueError("rollback dejó tráfico candidate")
    revision = _require_nonempty_string(
        result.get("restored_revision"), "rollback restored_revision",
    )
    restored_digest = result.get("restored_image_digest")
    if not isinstance(restored_digest, str) \
            or _IMAGE_DIGEST_RE.fullmatch(restored_digest) is None \
            or restored_digest == lineage["image_digest"]:
        raise ValueError(
            "rollback restored_image_digest debe ligar la baseline, no candidate"
        )
    if result.get("poll_preserved") is not True:
        raise ValueError("rollback no preservó polling")
    return {
        "exercise": exercise,
        "rollback_succeeded": True,
        "candidate_image_digest": lineage["image_digest"],
        "candidate_traffic_percent": 0,
        "restored_revision": revision,
        "restored_image_digest": restored_digest,
        "poll_preserved": True,
    }


_RESULT_VALIDATORS = {
    "ci_provenance": _validate_ci_provenance,
    "sbom": _validate_sbom,
    "scan": _validate_scan,
    "staging_revisions": _validate_staging_revisions,
    "e2e": _validate_e2e,
    "differential": _validate_differential,
    "semantic_review": _validate_semantic_review,
    "rollback": _validate_rollback,
}


def validate_artifact(
    name: str,
    document: Any,
    *,
    main_sha: str,
    image_digest: str,
    vulnerability_attestation_verifier: (
        VulnerabilityAttestationVerifier | None
    ) = None,
) -> dict[str, Any]:
    """Valida un resultado versionado y devuelve sólo claims sanitizados."""
    if name not in ARTIFACT_NAMES:
        raise ValueError(f"artefacto desconocido: {name}")
    body = _require_object(document, name)
    required = {
        "schema_version", "artifact_type", "status", "main_sha",
        "image_digest", "result",
    }
    if set(body) != required:
        raise ValueError(f"{name} con campos inválidos")
    if body.get("schema_version") != "1.0":
        raise ValueError(f"{name} schema_version no soportada")
    if body.get("artifact_type") != name:
        raise ValueError(f"{name} artifact_type distinto")
    if body.get("status") != "pass":
        raise ValueError(f"{name} status no-pass")
    if body.get("main_sha") != main_sha:
        raise ValueError(f"{name} main_sha distinto")
    if body.get("image_digest") != image_digest:
        raise ValueError(f"{name} image_digest distinto")
    result = _require_object(body.get("result"), f"{name}.result")
    normalized_result = _RESULT_VALIDATORS[name](
        result, main_sha=main_sha, image_digest=image_digest,
    )
    if name == "scan" and normalized_result["high_approvals"]:
        if vulnerability_attestation_verifier is None:
            raise ValueError(
                "scan HIGH requiere quorum externo autenticado; "
                "approvals.md y el JSON candidato no son autoridad"
            )
        vulnerability_attestation_verifier(normalized_result)
    return {
        "schema_version": "1.0",
        "artifact_type": name,
        "status": "pass",
        "main_sha": main_sha,
        "image_digest": image_digest,
        "result": normalized_result,
    }


def load_artifact_claims(
    artifact_files: Mapping[str, str],
    *,
    main_sha: str,
    image_digest: str,
    vulnerability_attestation_verifier: (
        VulnerabilityAttestationVerifier | None
    ) = None,
) -> dict[str, Any]:
    if set(artifact_files) != set(ARTIFACT_NAMES):
        raise ValueError("faltan archivos de evidencia")
    claims = {}
    for name in ARTIFACT_NAMES:
        try:
            with open(artifact_files[name], encoding="utf-8") as fh:
                document = json.load(fh)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{name} no es JSON legible") from exc
        claims[name] = validate_artifact(
            name, document, main_sha=main_sha, image_digest=image_digest,
            vulnerability_attestation_verifier=(
                vulnerability_attestation_verifier if name == "scan" else None
            ),
        )
    return claims


def validate_artifact_claims(
    claims: Any, *, main_sha: str, image_digest: str,
    vulnerability_attestation_verifier: (
        VulnerabilityAttestationVerifier | None
    ) = None,
) -> None:
    body = _require_object(claims, "artifact_claims")
    if set(body) != set(ARTIFACT_NAMES):
        raise ValueError("artifact_claims incompletos")
    for name in ARTIFACT_NAMES:
        normalized = validate_artifact(
            name, body[name], main_sha=main_sha, image_digest=image_digest,
            vulnerability_attestation_verifier=(
                vulnerability_attestation_verifier if name == "scan" else None
            ),
        )
        if normalized != body[name]:
            raise ValueError(f"artifact_claims {name} no canónico")


def validate_semantic_review_bindings(
    fields: Mapping[str, Any], claims: Mapping[str, Any],
) -> None:
    """Bind an external semantic receipt to the exact lexical artifact.

    The automatic differential remains explicitly non-semantic. Only a
    separate immutable receipt can elevate semantic quality for promotion.
    """
    differential = _require_object(
        _require_object(claims.get("differential"), "differential claim").get(
            "result"
        ),
        "differential claim result",
    )
    review = _require_object(
        _require_object(claims.get("semantic_review"), "semantic_review claim").get(
            "result"
        ),
        "semantic_review claim result",
    )
    expected = {
        "reviewed_case_count": differential.get("cases"),
        "rubric_set_sha256": _require_object(
            differential.get("semantic_evaluator"),
            "differential semantic_evaluator",
        ).get("rubric_set_sha256"),
        "reply_set_sha256": differential.get("reply_set_sha256"),
        "differential_uri": fields.get("differential_uri"),
    }
    for field, value in expected.items():
        if review.get(field) != value:
            raise ValueError(
                f"semantic_review {field} no coincide con differential"
            )
    if differential.get("semantic_quality_verified") is not False \
            or review.get("semantic_quality_verified") is not True:
        raise ValueError("semantic_review no eleva un differential lexical exacto")


def _validate_base_fields(fields: Mapping[str, Any]) -> None:
    missing = [name for name in BASE_REQUIRED_FIELDS if not fields.get(name)]
    if missing:
        raise ValueError(f"faltan campos del evidence manifest: {missing}")
    if _COMMIT_RE.fullmatch(str(fields["evidence_sha"])) is None:
        raise ValueError("evidence_sha no es commit válido")
    if _COMMIT_RE.fullmatch(str(fields["main_sha"])) is None:
        raise ValueError("main_sha no es commit válido")
    if _IMAGE_DIGEST_RE.fullmatch(str(fields["image_digest"])) is None:
        raise ValueError("image_digest no es digest inmutable")
    if _IMAGE_DIGEST_RE.fullmatch(
        str(fields["controller_builder_digest"]),
    ) is None:
        raise ValueError("controller_builder_digest no es digest inmutable")
    for name in ARTIFACT_NAMES:
        generation_from_uri(str(fields[f"{name}_uri"]))
        if _SHA256_RE.fullmatch(str(fields[f"{name}_hash"])) is None:
            raise ValueError(f"{name}_hash no es SHA-256")
def validate_fields(
    fields: dict[str, Any], *,
    vulnerability_attestation_verifier: (
        VulnerabilityAttestationVerifier | None
    ) = None,
) -> None:
    _validate_base_fields(fields)
    if "artifact_claims" not in fields:
        raise ValueError("faltan campos del evidence manifest: ['artifact_claims']")
    validate_artifact_claims(
        fields["artifact_claims"],
        main_sha=str(fields["main_sha"]),
        image_digest=str(fields["image_digest"]),
        vulnerability_attestation_verifier=vulnerability_attestation_verifier,
    )
    validate_semantic_review_bindings(
        fields,
        _require_object(fields["artifact_claims"], "artifact_claims"),
    )


def build_manifest(
    fields: dict[str, Any], *, artifact_files: Mapping[str, str],
    semantic_attestation_verifier: SemanticAttestationVerifier | None = None,
    vulnerability_attestation_verifier: (
        VulnerabilityAttestationVerifier | None
    ) = None,
) -> dict[str, Any]:
    _validate_base_fields(fields)
    candidate = dict(fields)
    candidate["artifact_claims"] = load_artifact_claims(
        artifact_files,
        main_sha=str(fields["main_sha"]),
        image_digest=str(fields["image_digest"]),
        vulnerability_attestation_verifier=vulnerability_attestation_verifier,
    )
    validate_fields(
        candidate,
        vulnerability_attestation_verifier=vulnerability_attestation_verifier,
    )
    if semantic_attestation_verifier is None:
        raise ValueError(
            "semantic_review requiere attestation externa autenticada; "
            "el JSON/GCS del candidato no es autoridad"
        )
    semantic_attestation_verifier(
        candidate,
        _require_object(candidate["artifact_claims"], "artifact_claims"),
    )

    manifest = {key: candidate[key] for key in REQUIRED_FIELDS}
    manifest["manifest_hash"] = hashlib.sha256(json.dumps(
        manifest, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Crea evidence manifest")
    parser.add_argument("--evidence-sha", required=True)
    parser.add_argument("--main-sha", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--controller-builder-digest", required=True)
    parser.add_argument("--out", required=True)
    for name in ARTIFACT_NAMES:
        option = name.replace("_", "-")
        parser.add_argument(f"--{option}-uri", required=True)
        parser.add_argument(f"--{option}-file", required=True)
    args = parser.parse_args(argv)

    fields: dict[str, Any] = {
        "evidence_sha": args.evidence_sha,
        "main_sha": args.main_sha,
        "image_digest": args.image_digest,
        "controller_builder_digest": args.controller_builder_digest,
    }
    artifact_files = {}
    for name in ARTIFACT_NAMES:
        fields[f"{name}_uri"] = getattr(args, f"{name}_uri")
        artifact_file = getattr(args, f"{name}_file")
        artifact_files[name] = artifact_file
        fields[f"{name}_hash"] = _sha256_file(artifact_file)
    manifest = build_manifest(
        fields, artifact_files=artifact_files,
    )
    with open(args.out, "w") as fh:
        json.dump(manifest, fh, sort_keys=True, indent=2)
    print(manifest["manifest_hash"])
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
