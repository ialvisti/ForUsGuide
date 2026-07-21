"""
Arnés diferencial REAL legacy vs v2 (plan de finalización, Tarea 9 Paso 3).

Cierra el bloqueo 11: el ``run_ticket_differential`` anterior sólo llamaba al
endpoint consolidado e imprimía campos para revisión manual — nunca invocaba
el sistema legacy ni calculaba una diferencia. Este runner:

1. para cada caso sanitizado ejecuta AMBOS sistemas (legacy y v2);
2. normaliza los resultados a una forma comparable;
3. compara IDs, cobertura de inquiries, módulos, hechos determinísticos,
   next_action, publicabilidad y aceptabilidad semántica;
4. emite JSON sanitizado + un resumen legible;
5. termina con código != 0 si no se alcanza cualquier umbral aprobado.

Los "sistemas" se inyectan como callables (``legacy_runner``/``v2_runner``)
para que el arnés sea testeable sin red; en staging se cablean a los clientes
HTTP reales. El comparador semántico también es inyectable (juez LLM en
staging; heurística determinística por defecto).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional
from urllib.parse import urlsplit

import httpx

THRESHOLDS_PATH = Path(__file__).with_name("ticket_differential_thresholds.json")
_MAIN_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_DIGEST_RE = re.compile(r"^\S+@sha256:[0-9a-f]{64}$")
_EXECUTION_SCOPE_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")

# firma de un runner: (caso) -> resultado normalizado del sistema
SystemRunner = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]
# firma del comparador semántico: (caso, legacy_text, v2_text) -> [0,1]
SemanticJudge = Callable[
    [Dict[str, Any], Optional[str], Optional[str]], float,
]


def load_thresholds(path: Path = THRESHOLDS_PATH) -> Dict[str, Any]:
    data = json.loads(path.read_text())
    return {k: v for k, v in data.items() if not k.startswith("__")}


def build_artifact(
    report: Mapping[str, Any], *, main_sha: str, image_digest: str,
) -> Dict[str, Any]:
    """Bind a sanitized report to the exact promoted runtime lineage."""
    if _MAIN_SHA_RE.fullmatch(main_sha) is None:
        raise ValueError("main_sha debe ser un commit SHA completo")
    if _IMAGE_DIGEST_RE.fullmatch(image_digest) is None:
        raise ValueError("image_digest debe ser una referencia @sha256 inmutable")
    return {
        "schema_version": "1.0",
        "artifact_type": "differential",
        "status": "pass" if report.get("passed") is True else "fail",
        "main_sha": main_sha,
        "image_digest": image_digest,
        "result": dict(report),
    }


def _parse_gcs_destination(destination_uri: str) -> tuple[str, str]:
    parsed = urlsplit(destination_uri)
    object_name = parsed.path.lstrip("/")
    if (
        parsed.scheme != "gs"
        or not parsed.netloc
        or not object_name
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "GCS destination debe ser gs://bucket/object sin generation"
        )
    return parsed.netloc, object_name


def upload_artifact_write_once(
    artifact: Mapping[str, Any],
    destination_uri: str,
    *,
    storage_client: Any = None,
) -> str:
    """Create one immutable evidence object and return its generation URI."""
    bucket_name, object_name = _parse_gcs_destination(destination_uri)
    if storage_client is None:
        from google.cloud import storage

        storage_client = storage.Client()
    blob = storage_client.bucket(bucket_name).blob(object_name)
    payload = json.dumps(
        dict(artifact), sort_keys=True, separators=(",", ":"),
    ) + "\n"
    blob.upload_from_string(
        payload,
        content_type="application/json",
        if_generation_match=0,
    )
    generation = getattr(blob, "generation", None)
    if generation is None:
        raise RuntimeError("GCS no devolvió la generation del objeto creado")
    generation_text = str(generation)
    if not generation_text.isdigit() or int(generation_text) <= 0:
        raise RuntimeError("GCS devolvió una generation inválida")
    return f"gs://{bucket_name}/{object_name}#{generation_text}"


def _require_https_audience(value: str, label: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(f"{label} debe ser una audiencia HTTPS base exacta")
    return value.rstrip("/")


def _https_origin(value: str, label: str) -> str:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} contiene un puerto inválido") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(f"{label} debe usar HTTPS sin credenciales embebidas")
    host = parsed.hostname.casefold()
    return f"https://{host}" if port in {None, 443} else f"https://{host}:{port}"


def _require_endpoint_for_audience(
    endpoint: str,
    audience: str,
    label: str,
) -> str:
    parsed = urlsplit(endpoint)
    if (
        parsed.query
        or parsed.fragment
        or (parsed.path and not parsed.path.startswith("/"))
    ):
        raise ValueError(
            f"{label} debe ser un endpoint HTTPS exacto sin query/fragment"
        )
    endpoint_origin = _https_origin(endpoint, label)
    audience_origin = _https_origin(
        _require_https_audience(audience, f"{label} audience"),
        f"{label} audience",
    )
    if endpoint_origin != audience_origin:
        raise ValueError(
            f"{label} no pertenece al origen exacto de su audience"
        )
    return endpoint


def _same_origin_poll_url(response_url: httpx.URL, location: str) -> str:
    """Resolve a poll target without forwarding credentials cross-origin."""
    resolved = response_url.join(location)
    parsed = urlsplit(str(resolved))
    if parsed.query or parsed.fragment:
        raise ValueError("poll URL debe ser exacta y sin query/fragment")
    if _https_origin(str(resolved), "poll URL") != _https_origin(
        str(response_url), "response URL"
    ):
        raise ValueError("poll URL cambió de origen; credenciales no reenviadas")
    return str(resolved)


def _fetch_identity_token(audience: str) -> str:
    """Mint a short-lived ID token from the Run Job service account."""
    from google.auth.transport.requests import Request
    from google.oauth2.id_token import fetch_id_token

    token = fetch_id_token(Request(), audience)
    if not isinstance(token, str) or not token:
        raise RuntimeError("no se pudo obtener un ID token efímero")
    return token


def _naive_semantic_judge(
    _case: Dict[str, Any],
    legacy_text: Optional[str],
    v2_text: Optional[str],
) -> float:
    """Conservative lexical fallback used only by explicit offline runs."""
    if not legacy_text and not v2_text:
        # Dos respuestas ausentes no constituyen equivalencia semántica. El
        # harness anterior las puntuaba 1.0 y podía certificar un contrato
        # que no había extraído ningún texto del response v2 real.
        return 0.0
    if not legacy_text or not v2_text:
        return 0.0
    a = set(legacy_text.lower().split())
    b = set(v2_text.lower().split())
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _validated_semantic_rubric(case: Mapping[str, Any]) -> Mapping[str, Any]:
    rubric = case.get("semantic_rubric")
    if not isinstance(rubric, Mapping) or set(rubric) != {
        "version", "required_concepts", "forbidden_phrases",
    }:
        raise ValueError(
            "cada caso live requiere semantic_rubric v1 con campos exactos"
        )
    if rubric.get("version") != "1.0":
        raise ValueError("semantic_rubric.version debe ser 1.0")

    concepts = rubric.get("required_concepts")
    if not isinstance(concepts, list) or not concepts or len(concepts) > 100:
        raise ValueError("semantic_rubric.required_concepts debe ser no vacío")
    concept_ids: List[str] = []
    for concept in concepts:
        if not isinstance(concept, Mapping) or set(concept) != {"id", "phrases"}:
            raise ValueError("required_concepts usa sólo id + phrases")
        concept_id = concept.get("id")
        phrases = concept.get("phrases")
        if (
            not isinstance(concept_id, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", concept_id) is None
            or not isinstance(phrases, list)
            or not phrases
            or len(phrases) > 20
            or any(
                not isinstance(phrase, str)
                or not phrase.strip()
                or len(phrase) > 300
                for phrase in phrases
            )
        ):
            raise ValueError("required_concepts contiene id/phrases inválidos")
        concept_ids.append(concept_id)
    if len(concept_ids) != len(set(concept_ids)):
        raise ValueError("required_concepts contiene IDs duplicados")

    forbidden = rubric.get("forbidden_phrases")
    if (
        not isinstance(forbidden, list)
        or len(forbidden) > 100
        or any(
            not isinstance(phrase, str)
            or not phrase.strip()
            or len(phrase) > 300
            for phrase in forbidden
        )
    ):
        raise ValueError("semantic_rubric.forbidden_phrases inválido")
    return rubric


def rubric_set_sha256(cases: List[Dict[str, Any]]) -> str:
    reviewed = [
        {
            "case_id": str(case.get("case_id", "")),
            "semantic_rubric": dict(_validated_semantic_rubric(case)),
        }
        for case in cases
    ]
    canonical = json.dumps(
        reviewed, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _semantic_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


def _rubric_text_score(text: Optional[str], rubric: Mapping[str, Any]) -> float:
    if not text:
        return 0.0
    normalized = _semantic_text(text)
    forbidden = rubric["forbidden_phrases"]
    if any(_semantic_text(phrase) in normalized for phrase in forbidden):
        return 0.0
    concepts = rubric["required_concepts"]
    matched = sum(
        any(
            _semantic_text(phrase) in normalized
            for phrase in concept["phrases"]
        )
        for concept in concepts
    )
    return matched / len(concepts)


def _reviewed_lexical_rubric_judge(
    case: Dict[str, Any],
    legacy_text: Optional[str],
    v2_text: Optional[str],
) -> float:
    """Deterministic lexical smoke used by evidence-producing runs.

    Each synthetic case owns a bounded semantic rubric: required concepts can
    list reviewed paraphrases and forbidden phrases encode known strings.
    This deliberately does not claim semantic correctness: unseen paraphrases
    and contradictions can satisfy substring coverage.
    """
    rubric = _validated_semantic_rubric(case)
    return min(
        _rubric_text_score(legacy_text, rubric),
        _rubric_text_score(v2_text, rubric),
    )


@dataclass
class CaseComparison:
    case_id: str
    deterministic_exact: bool
    inquiry_coverage_match: bool
    modules_match: bool
    next_action_match: bool
    v2_publishable: bool
    v2_unsafe_publish: bool
    v2_missing_inquiries: bool
    v2_poll_404_unexplained: bool
    idempotency_replay_failed: bool
    idempotency_replay_observed: bool
    semantic_score: float
    legacy_reply_sha256: Optional[str]
    v2_reply_sha256: Optional[str]
    notes: List[str] = field(default_factory=list)


def _det_facts(system_result: Dict[str, Any]) -> Dict[str, Any]:
    """Extrae los hechos DETERMINÍSTICOS comparables (IDs, módulos, conteos,
    límites de tokens) — nunca texto libre."""
    return {
        # Dos ejecuciones independientes generan IDs upstream distintos. El
        # contrato comparable es su cardinalidad, no igualdad de valores.
        "forusbots_job_count": len(
            set(system_result.get("forusbots_job_ids", []) or [])
        ),
        "modules": sorted(system_result.get("modules", []) or []),
        "total_inquiries": system_result.get("total_inquiries"),
        "token_limit": system_result.get("token_limit"),
        "next_action": system_result.get("next_action"),
        "facts": system_result.get("deterministic_facts", {}),
    }


def compare_case(case: Dict[str, Any], legacy: Dict[str, Any],
                 v2: Dict[str, Any], *, judge: SemanticJudge) -> CaseComparison:
    det_match = _det_facts(legacy) == _det_facts(v2)
    legacy_inq = legacy.get("total_inquiries")
    v2_inq = v2.get("total_inquiries")
    coverage_match = legacy_inq == v2_inq
    modules_match = sorted(legacy.get("modules", []) or []) == \
        sorted(v2.get("modules", []) or [])
    next_action_match = legacy.get("next_action") == v2.get("next_action")

    v2_state = v2.get("state")
    v2_next = v2.get("next_action")
    v2_safe = bool(v2.get("all_inquiries_safe"))
    publishable = (v2_state == "succeeded"
                   and v2_next == "send_participant_reply"
                   and v2_safe
                   and not v2.get("fallback"))
    # La API no publica por sí misma: autoriza al caller mediante next_action.
    # Por tanto el gate observable es si AUTORIZÓ publicar sin cumplir todos
    # los invariantes, no un campo `published` que el HTTP real nunca emite.
    unsafe_publish = (
        v2_next == "send_participant_reply" and not publishable
    )
    missing = (v2_inq is not None and legacy_inq is not None
               and v2_inq < legacy_inq)
    poll_404 = bool(v2.get("poll_404_unexplained"))
    replay_failed = bool(v2.get("idempotency_replay_failed"))
    replay_observed = v2.get("idempotency_replay_observed") is True
    semantic = judge(case, legacy.get("reply_text"), v2.get("reply_text"))

    def _reply_hash(value: Any) -> Optional[str]:
        return (
            hashlib.sha256(value.encode("utf-8")).hexdigest()
            if isinstance(value, str) and value
            else None
        )

    notes: List[str] = []
    if not det_match:
        notes.append("deterministic facts differ")
    if unsafe_publish:
        notes.append("v2 published an unsafe result")
    return CaseComparison(
        case_id=str(case.get("case_id", "unknown")),
        deterministic_exact=det_match,
        inquiry_coverage_match=coverage_match,
        modules_match=modules_match,
        next_action_match=next_action_match,
        v2_publishable=publishable,
        v2_unsafe_publish=unsafe_publish,
        v2_missing_inquiries=missing,
        v2_poll_404_unexplained=poll_404,
        idempotency_replay_failed=replay_failed,
        idempotency_replay_observed=replay_observed,
        semantic_score=semantic,
        legacy_reply_sha256=_reply_hash(legacy.get("reply_text")),
        v2_reply_sha256=_reply_hash(v2.get("reply_text")),
        notes=notes,
    )


async def run_differential(
    cases: List[Dict[str, Any]],
    legacy_runner: SystemRunner,
    v2_runner: SystemRunner,
    *,
    judge: SemanticJudge = _naive_semantic_judge,
    thresholds: Optional[Dict[str, Any]] = None,
    semantic_evaluator: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Ejecuta AMBOS sistemas por caso y computa el veredicto agregado.

    Levanta ``ValueError`` si algún runner no se invoca para un caso (guard
    contra el bug del arnés viejo que nunca llamaba a legacy)."""
    thresholds = thresholds or load_thresholds()
    if not cases:
        raise ValueError("el differential requiere al menos un caso")

    comparisons: List[CaseComparison] = []
    legacy_calls = v2_calls = 0
    for case in cases:
        legacy_result = await legacy_runner(case)
        legacy_calls += 1
        v2_result = await v2_runner(case)
        v2_calls += 1
        comparisons.append(compare_case(case, legacy_result, v2_result,
                                        judge=judge))

    if legacy_calls == 0 or v2_calls == 0:
        raise ValueError(
            "el differential debe invocar AMBOS sistemas (legacy y v2)")

    n = len(comparisons)
    det_rate = sum(c.deterministic_exact for c in comparisons) / n
    unsafe_rate = sum(c.v2_unsafe_publish for c in comparisons) / n
    missing_rate = sum(c.v2_missing_inquiries for c in comparisons) / n
    replay_failure_rate = sum(c.idempotency_replay_failed for c in comparisons) / n
    replay_observation_rate = sum(
        c.idempotency_replay_observed for c in comparisons
    ) / n
    poll404_rate = sum(c.v2_poll_404_unexplained for c in comparisons) / n
    semantic_min = min(c.semantic_score for c in comparisons)

    checks = {
        "deterministic_exact_match_rate": (
            det_rate, ">=", thresholds["deterministic_exact_match_rate"]),
        "unsafe_publish_rate": (
            unsafe_rate, "<=", thresholds["unsafe_publish_rate_max"]),
        "missing_inquiry_rate": (
            missing_rate, "<=", thresholds["missing_inquiry_rate_max"]),
        "idempotency_replay_failure_rate": (
            replay_failure_rate,
            "<=",
            thresholds["idempotency_replay_failure_rate_max"],
        ),
        "idempotency_replay_observation_rate": (
            replay_observation_rate,
            ">=",
            thresholds["idempotency_replay_observation_rate_min"],
        ),
        "unexplained_poll_404_rate": (
            poll404_rate, "<=", thresholds["unexplained_poll_404_rate_max"]),
        "reviewed_lexical_coverage_min": (
            semantic_min, ">=", thresholds["reviewed_lexical_coverage_min"]),
    }
    failures = []
    for name, (value, op, bound) in checks.items():
        ok = value >= bound if op == ">=" else value <= bound
        if not ok:
            failures.append({"metric": name, "value": round(value, 4),
                             "op": op, "threshold": bound})

    per_case = [
        {"case_id": c.case_id, "deterministic_exact": c.deterministic_exact,
         "next_action_match": c.next_action_match,
         "v2_unsafe_publish": c.v2_unsafe_publish,
         "reviewed_lexical_coverage": round(c.semantic_score, 3),
         "legacy_reply_sha256": c.legacy_reply_sha256,
         "v2_reply_sha256": c.v2_reply_sha256,
         "notes": c.notes}
        for c in comparisons
    ]
    reply_set_sha256 = hashlib.sha256(json.dumps(
        [
            {
                "case_id": item["case_id"],
                "legacy_reply_sha256": item["legacy_reply_sha256"],
                "v2_reply_sha256": item["v2_reply_sha256"],
            }
            for item in per_case
        ],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()

    return {
        "cases": n,
        "passed": not failures,
        # A substring smoke cannot establish semantic correctness. Promotion
        # requires a separate immutable semantic-review receipt.
        "semantic_quality_verified": False,
        "semantic_evaluator": dict(semantic_evaluator or {
            "method": "offline-token-jaccard-v1",
        }),
        "reply_set_sha256": reply_set_sha256,
        "metrics": {name: round(v, 4) for name, (v, _o, _b) in checks.items()},
        "failures": failures,
        # Sólo hashes/flags; nunca texto de participante.
        "per_case": per_case,
    }


def _human_summary(report: Dict[str, Any]) -> str:
    lines = [f"differential: {report['cases']} casos — "
             f"{'PASÓ' if report['passed'] else 'FALLÓ'}"]
    for name, value in report["metrics"].items():
        lines.append(f"  {name}: {value}")
    for f in report["failures"]:
        lines.append(f"  ✗ {f['metric']}={f['value']} viola {f['op']} {f['threshold']}")
    return "\n".join(lines)


def _load_cases(path: Path) -> List[Dict[str, Any]]:
    document = json.loads(path.read_text())
    if isinstance(document, list):
        cases = document
    elif isinstance(document, dict) and isinstance(document.get("cases"), list):
        cases = document["cases"]
    elif isinstance(document, dict) and isinstance(document.get("request"), dict):
        cases = [{"case_id": document.get("case_id", path.stem),
                  "request": document["request"]}]
    else:
        raise ValueError("--cases debe contener una lista/cases[]/request")
    if not all(isinstance(case, dict) and case.get("case_id") for case in cases):
        raise ValueError("cada caso requiere case_id")
    return cases


def _inquiry_results(document: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the public InquiryResult objects from either v1 or v2."""
    raw_inquiries = document.get("inquiries")
    if isinstance(raw_inquiries, list):
        results = []
        for item in raw_inquiries:
            if not isinstance(item, dict):
                continue
            result = item.get("result")
            if isinstance(result, dict):
                results.append(result)
        return results

    primary = document.get("primary")
    related = document.get("related")
    candidates = [primary, *(related if isinstance(related, list) else [])]
    return [item for item in candidates if isinstance(item, dict)]


def _render_participant_reply(value: Any) -> Optional[str]:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if not isinstance(value, dict):
        return None

    parts: List[str] = []
    opening = value.get("opening")
    if isinstance(opening, str) and opening.strip():
        parts.append(opening.strip())
    for key in ("key_points",):
        values = value.get(key)
        if isinstance(values, list):
            parts.extend(
                item.strip() for item in values
                if isinstance(item, str) and item.strip()
            )
    steps = value.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            for key in ("action", "detail"):
                item = step.get(key)
                if isinstance(item, str) and item.strip():
                    parts.append(item.strip())
    warnings = value.get("warnings")
    if isinstance(warnings, list):
        parts.extend(
            item.strip() for item in warnings
            if isinstance(item, str) and item.strip()
        )
    return "\n".join(parts) or None


def _reply_text(document: Dict[str, Any]) -> Optional[str]:
    replies: List[str] = []
    for result in _inquiry_results(document):
        candidate: Any = result.get("needs_more_info_message")
        if not candidate:
            candidate = (result.get("knowledge_answer") or {}).get("answer")
        if not candidate:
            candidate = (
                ((result.get("generate_response") or {}).get("response") or {})
                .get("response_to_participant")
            )
        rendered = _render_participant_reply(candidate)
        if rendered:
            replies.append(rendered)
    return "\n".join(replies) or None


def _nested_modules(results: List[Dict[str, Any]]) -> List[str]:
    modules: set[str] = set()
    for result in results:
        mapped = (result.get("diagnostics") or {}).get("mapped_modules")
        if not isinstance(mapped, list):
            continue
        for item in mapped:
            value = item.get("key") if isinstance(item, dict) else item
            if isinstance(value, str) and value:
                modules.add(value)
    return sorted(modules)


def _nested_deterministic_facts(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    facts: Dict[str, Any] = {}
    for index, result in enumerate(results):
        candidate = (result.get("diagnostics") or {}).get("deterministic_facts")
        if not isinstance(candidate, dict):
            continue
        for key, value in candidate.items():
            output_key = key if key not in facts else f"{index}:{key}"
            facts[output_key] = value
    return facts


def _nested_token_limit(results: List[Dict[str, Any]]) -> Any:
    values = [
        (result.get("diagnostics") or {}).get("token_limit")
        for result in results
        if (result.get("diagnostics") or {}).get("token_limit") is not None
    ]
    if not values:
        return None
    if all(value == values[0] for value in values):
        return values[0]
    return values


def normalize_http_result(document: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize public v1/v2 shapes without retaining raw request content."""
    inquiries = document.get("inquiries") or []
    if not inquiries and document.get("primary"):
        inquiries = [document["primary"], *(document.get("related") or [])]
    results = _inquiry_results(document)
    all_safe = bool(inquiries) and all(
        item.get("participant_reply_safe") is True for item in inquiries
    )
    metadata = document.get("metadata") or {}
    ids = list(document.get("forusbots_job_ids") or [])
    for item in inquiries:
        result = item.get("result") or item
        diagnostics = result.get("diagnostics") or {}
        for key in ("forusbots_job_id", "forusbots_participant_job_id",
                    "forusbots_plan_job_id"):
            if diagnostics.get(key) and diagnostics[key] not in ids:
                ids.append(diagnostics[key])
    total = document.get("total_inquiries")
    if total is None:
        total = document.get("total_inquiries_in_ticket")
    state = document.get("state") or (
        "succeeded" if document.get("route_taken") else None
    )
    next_action = document.get("next_action") or (
        "send_participant_reply" if state == "succeeded" else None
    )
    modules = document.get("modules")
    if not isinstance(modules, list):
        modules = _nested_modules(results)
    deterministic_facts = document.get("deterministic_facts")
    if not isinstance(deterministic_facts, dict):
        deterministic_facts = _nested_deterministic_facts(results)
    token_limit = document.get("token_limit")
    if token_limit is None:
        token_limit = _nested_token_limit(results)

    return {
        "state": state,
        "next_action": next_action,
        "all_inquiries_safe": all_safe,
        "published": bool(document.get("published", False)),
        "fallback": metadata.get("fallback") is True,
        "total_inquiries": total,
        "modules": sorted(modules),
        "forusbots_job_ids": sorted(ids),
        "token_limit": token_limit,
        "deterministic_facts": deterministic_facts,
        "reply_text": _reply_text(document),
        "poll_404_unexplained": bool(document.get("poll_404_unexplained")),
        "idempotency_replay_failed": bool(
            document.get("idempotency_replay_failed")
        ),
        "idempotency_replay_observed": (
            document.get("idempotency_replay_observed") is True
        ),
    }


def _build_http_runners(
    *, legacy_url: str, v2_url: str,
    legacy_api_key: str, v2_api_key: str,
    legacy_audience: str, v2_audience: str,
    legacy_authorization_token: str, v2_authorization_token: str,
    poll_timeout_s: float, poll_interval_s: float,
    execution_scope: str,
) -> tuple[SystemRunner, SystemRunner, httpx.AsyncClient]:
    _require_endpoint_for_audience(legacy_url, legacy_audience, "legacy URL")
    _require_endpoint_for_audience(v2_url, v2_audience, "v2 URL")
    if _EXECUTION_SCOPE_RE.fullmatch(execution_scope) is None:
        raise ValueError(
            "execution_scope debe ser un nombre inmutable lowercase de 1-63 chars"
        )
    client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))

    def _headers(case: Dict[str, Any], *, v2: bool) -> Dict[str, str]:
        authorization_token = (
            v2_authorization_token if v2 else legacy_authorization_token
        )
        api_key = v2_api_key if v2 else legacy_api_key
        headers = {
            "Authorization": f"Bearer {authorization_token}",
            "X-API-Key": api_key,
        }
        logical_id = str(case.get("idempotency_key") or case["case_id"])
        # v1/v2 intentionally share the production idempotency namespace. A
        # shared key here would make the second call replay the first job and
        # falsely claim that both systems ran. Stable per-system keys preserve
        # retry safety while forcing two independent executions.
        system = "v2" if v2 else "legacy"
        digest = hashlib.sha256(
            (
                f"ticket-differential\0{execution_scope}\0{system}\0{logical_id}"
            ).encode(),
        ).hexdigest()
        headers["Idempotency-Key"] = f"diff-{digest}"
        # Each target receives only the token minted for its own audience.
        # Reusing the v2 token against a distinct legacy origin both breaks the
        # legacy WIF check and discloses a replayable v2 credential.
        headers["X-ForUs-Workload-Authorization"] = (
            f"Bearer {authorization_token}"
        )
        return headers

    async def _poll_terminal(
        *, response: httpx.Response, location: str, headers: Dict[str, str],
    ) -> Dict[str, Any]:
        poll_url = _same_origin_poll_url(response.url, location)
        deadline = asyncio.get_running_loop().time() + poll_timeout_s
        while True:
            polled = await client.get(poll_url, headers=headers)
            if polled.status_code == 404:
                return normalize_http_result({"poll_404_unexplained": True})
            polled.raise_for_status()
            document = polled.json()
            if document.get("state") in {
                "succeeded", "partial", "failed", "timeout", "cancelled"
            }:
                return normalize_http_result(document)
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("poll excedió el timeout diferencial")
            retry_after = float(polled.headers.get("Retry-After") or poll_interval_s)
            await asyncio.sleep(min(30.0, max(poll_interval_s, retry_after)))

    async def legacy(case: Dict[str, Any]) -> Dict[str, Any]:
        headers = _headers(case, v2=False)
        response = await client.post(
            legacy_url, json=case["request"], headers=headers,
        )
        response.raise_for_status()
        document = response.json()
        if response.status_code == 202:
            location = (
                response.headers.get("Location")
                or document.get("poll_url")
                or document.get("status_url")
            )
            if not isinstance(location, str) or not location:
                raise ValueError("legacy no devolvió poll_url para su 202")
            return await _poll_terminal(
                response=response, location=location, headers=headers,
            )
        return normalize_http_result(document)

    async def v2(case: Dict[str, Any]) -> Dict[str, Any]:
        headers = _headers(case, v2=True)
        payload = dict(case["request"])
        payload.pop("idempotency_key", None)
        payload.pop("ticket_handler_mode", None)
        response = await client.post(v2_url, json=payload, headers=headers)
        response.raise_for_status()
        accepted = response.json()
        location = response.headers.get("Location") or accepted.get("status_url")
        if response.status_code != 202 or not location:
            raise ValueError("v2 no devolvió 202 + status_url")
        accepted_job_id = accepted.get("ticket_job_id")
        if not isinstance(accepted_job_id, str) or not accepted_job_id:
            raise ValueError("v2 no devolvió ticket_job_id estable")

        # Replay inmediato con la MISMA key: prueba únicamente el dedupe durable
        # de admisión. No observa ni afirma exactly-once delivery downstream.
        replay = await client.post(v2_url, json=payload, headers=headers)
        replay.raise_for_status()
        replay_document = replay.json()
        replay_failed = not (
            replay.status_code == 202
            and replay_document.get("ticket_job_id") == accepted_job_id
            and replay_document.get("idempotency_replayed") is True
        )

        result = await _poll_terminal(
            response=response, location=str(location), headers=headers,
        )
        result["idempotency_replay_observed"] = True
        result["idempotency_replay_failed"] = replay_failed
        return result

    return legacy, v2, client


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Arnés diferencial legacy vs v2")
    parser.add_argument("--cases", required=True, help="JSON con casos sanitizados")
    parser.add_argument("--out", required=True, help="ruta del reporte JSON")
    parser.add_argument("--legacy-url", default=os.getenv("TICKET_LEGACY_URL"))
    parser.add_argument("--v2-url", default=os.getenv("TICKET_V2_URL"))
    parser.add_argument(
        "--legacy-audience",
        default=os.getenv("TICKET_DIFFERENTIAL_LEGACY_AUDIENCE"),
    )
    parser.add_argument(
        "--v2-audience",
        default=os.getenv("TICKET_DIFFERENTIAL_V2_AUDIENCE"),
    )
    parser.add_argument(
        "--main-sha",
        default=os.getenv("TICKET_DIFFERENTIAL_MAIN_SHA")
        or os.getenv("COMMIT_SHA"),
    )
    parser.add_argument(
        "--image-digest",
        default=os.getenv("TICKET_DIFFERENTIAL_IMAGE_DIGEST"),
    )
    parser.add_argument(
        "--evidence-uri",
        default=os.getenv("TICKET_DIFFERENTIAL_EVIDENCE_URI"),
    )
    parser.add_argument(
        "--execution-scope",
        required=True,
        help="identificador inmutable y único de esta ejecución",
    )
    parser.add_argument("--offline-no-upload", action="store_true")
    parser.add_argument("--poll-timeout-s", type=float, default=2700.0)
    parser.add_argument("--poll-interval-s", type=float, default=3.0)
    args = parser.parse_args(argv)
    legacy_api_key = os.getenv("TICKET_DIFFERENTIAL_LEGACY_API_KEY", "")
    v2_api_key = os.getenv("TICKET_DIFFERENTIAL_V2_API_KEY", "")
    required = {
        "--legacy-url": args.legacy_url,
        "--v2-url": args.v2_url,
        "--legacy-audience": args.legacy_audience,
        "--v2-audience": args.v2_audience,
        "--main-sha": args.main_sha,
        "--image-digest": args.image_digest,
        "--execution-scope": args.execution_scope,
        "TICKET_DIFFERENTIAL_LEGACY_API_KEY": legacy_api_key,
        "TICKET_DIFFERENTIAL_V2_API_KEY": v2_api_key,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        parser.error(
            "faltan entradas requeridas: " + ", ".join(sorted(missing))
        )
    deployed = os.getenv("APP_ENV", "").strip().lower() in {
        "staging", "production",
    }
    if args.offline_no_upload and deployed:
        parser.error("--offline-no-upload está prohibido en staging/production")
    if not args.evidence_uri and not args.offline_no_upload:
        parser.error("requiere --evidence-uri salvo en ejecución local explícita")

    try:
        legacy_audience = _require_https_audience(
            args.legacy_audience, "legacy audience",
        )
        v2_audience = _require_https_audience(
            args.v2_audience, "v2 audience",
        )
        _require_endpoint_for_audience(
            args.legacy_url, legacy_audience, "legacy URL",
        )
        _require_endpoint_for_audience(
            args.v2_url, v2_audience, "v2 URL",
        )
        if _EXECUTION_SCOPE_RE.fullmatch(args.execution_scope) is None:
            raise ValueError(
                "execution_scope debe ser un nombre inmutable lowercase de 1-63 chars"
            )
        # Validate immutable lineage before making any network request.
        build_artifact(
            {"passed": False},
            main_sha=args.main_sha,
            image_digest=args.image_digest,
        )
    except ValueError as exc:
        parser.error(str(exc))

    async def _run() -> Dict[str, Any]:
        cases = _load_cases(Path(args.cases))
        judge: SemanticJudge
        if args.offline_no_upload:
            judge = _naive_semantic_judge
            evaluator = {"method": "offline-token-jaccard-v1"}
        else:
            # Validate every reviewed rubric before minting tokens or making
            # either effectful system call. Evidence-producing executions
            # never fall back to a lexical/LLM heuristic without provenance.
            rubric_hash = rubric_set_sha256(cases)
            judge = _reviewed_lexical_rubric_judge
            evaluator = {
                "method": "reviewed-lexical-rubric-v1",
                "rubric_set_sha256": rubric_hash,
            }
        tokens = {
            audience: _fetch_identity_token(audience)
            for audience in {
                legacy_audience, v2_audience,
            }
        }
        legacy, v2, client = _build_http_runners(
            legacy_url=args.legacy_url,
            v2_url=args.v2_url,
            legacy_audience=legacy_audience,
            v2_audience=v2_audience,
            legacy_api_key=legacy_api_key,
            v2_api_key=v2_api_key,
            legacy_authorization_token=tokens[legacy_audience],
            v2_authorization_token=tokens[v2_audience],
            poll_timeout_s=args.poll_timeout_s,
            poll_interval_s=args.poll_interval_s,
            execution_scope=args.execution_scope,
        )
        try:
            return await run_differential(
                cases,
                legacy,
                v2,
                judge=judge,
                semantic_evaluator=evaluator,
            )
        finally:
            await client.aclose()

    try:
        report = asyncio.run(_run())
        artifact = build_artifact(
            report,
            main_sha=args.main_sha,
            image_digest=args.image_digest,
        )
        Path(args.out).write_text(
            json.dumps(artifact, indent=2, sort_keys=True) + "\n",
        )
        if args.evidence_uri:
            generation_uri = upload_artifact_write_once(
                artifact, args.evidence_uri,
            )
            print(f"evidence_uri={generation_uri}")
        print(_human_summary(report))
        return 0 if report["passed"] else 1
    except Exception as exc:  # noqa: BLE001 - CLI emits type only, no PII/body
        print(f"differential error: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
