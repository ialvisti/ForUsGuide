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
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

THRESHOLDS_PATH = Path(__file__).with_name("ticket_differential_thresholds.json")

# firma de un runner: (caso) -> resultado normalizado del sistema
SystemRunner = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]
# firma del comparador semántico: (legacy_text, v2_text) -> [0,1]
SemanticJudge = Callable[[Optional[str], Optional[str]], float]


def load_thresholds(path: Path = THRESHOLDS_PATH) -> Dict[str, Any]:
    data = json.loads(path.read_text())
    return {k: v for k, v in data.items() if not k.startswith("__")}


def _naive_semantic_judge(legacy_text: Optional[str],
                          v2_text: Optional[str]) -> float:
    """Heurística determinística por defecto (Jaccard de tokens). En staging
    se sustituye por un juez LLM; nunca se afirma 95% sin ejecutar el juez."""
    if not legacy_text and not v2_text:
        return 1.0
    if not legacy_text or not v2_text:
        return 0.0
    a = set(legacy_text.lower().split())
    b = set(v2_text.lower().split())
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


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
    duplicate_reply: bool
    semantic_score: float
    notes: List[str] = field(default_factory=list)


def _det_facts(system_result: Dict[str, Any]) -> Dict[str, Any]:
    """Extrae los hechos DETERMINÍSTICOS comparables (IDs, módulos, conteos,
    límites de tokens) — nunca texto libre."""
    return {
        "forusbots_job_ids": sorted(system_result.get("forusbots_job_ids", []) or []),
        "modules": sorted(system_result.get("modules", []) or []),
        "total_inquiries": system_result.get("total_inquiries"),
        "token_limit": system_result.get("token_limit"),
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
    # publicación insegura: publicó pese a no cumplir las tres condiciones
    unsafe_publish = bool(v2.get("published")) and not publishable
    missing = (v2_inq is not None and legacy_inq is not None
               and v2_inq < legacy_inq)
    poll_404 = bool(v2.get("poll_404_unexplained"))
    duplicate = bool(v2.get("duplicate_reply"))
    semantic = judge(legacy.get("reply_text"), v2.get("reply_text"))

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
        duplicate_reply=duplicate,
        semantic_score=semantic,
        notes=notes,
    )


async def run_differential(
    cases: List[Dict[str, Any]],
    legacy_runner: SystemRunner,
    v2_runner: SystemRunner,
    *,
    judge: SemanticJudge = _naive_semantic_judge,
    thresholds: Optional[Dict[str, Any]] = None,
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
    dup_rate = sum(c.duplicate_reply for c in comparisons) / n
    poll404_rate = sum(c.v2_poll_404_unexplained for c in comparisons) / n
    semantic_min = min(c.semantic_score for c in comparisons)

    checks = {
        "deterministic_exact_match_rate": (
            det_rate, ">=", thresholds["deterministic_exact_match_rate"]),
        "unsafe_publish_rate": (
            unsafe_rate, "<=", thresholds["unsafe_publish_rate_max"]),
        "missing_inquiry_rate": (
            missing_rate, "<=", thresholds["missing_inquiry_rate_max"]),
        "duplicate_reply_rate": (
            dup_rate, "<=", thresholds["duplicate_reply_rate_max"]),
        "unexplained_poll_404_rate": (
            poll404_rate, "<=", thresholds["unexplained_poll_404_rate_max"]),
        "semantic_acceptability_min": (
            semantic_min, ">=", thresholds["semantic_acceptability_min"]),
    }
    failures = []
    for name, (value, op, bound) in checks.items():
        ok = value >= bound if op == ">=" else value <= bound
        if not ok:
            failures.append({"metric": name, "value": round(value, 4),
                             "op": op, "threshold": bound})

    return {
        "cases": n,
        "passed": not failures,
        "metrics": {name: round(v, 4) for name, (v, _o, _b) in checks.items()},
        "failures": failures,
        # sanitizado: sólo case_id y flags, jamás texto de participante
        "per_case": [
            {"case_id": c.case_id, "deterministic_exact": c.deterministic_exact,
             "next_action_match": c.next_action_match,
             "v2_unsafe_publish": c.v2_unsafe_publish,
             "semantic_score": round(c.semantic_score, 3), "notes": c.notes}
            for c in comparisons
        ],
    }


def _human_summary(report: Dict[str, Any]) -> str:
    lines = [f"differential: {report['cases']} casos — "
             f"{'PASÓ' if report['passed'] else 'FALLÓ'}"]
    for name, value in report["metrics"].items():
        lines.append(f"  {name}: {value}")
    for f in report["failures"]:
        lines.append(f"  ✗ {f['metric']}={f['value']} viola {f['op']} {f['threshold']}")
    return "\n".join(lines)


def main(argv=None) -> int:  # pragma: no cover - CLI, ejercitado en staging
    parser = argparse.ArgumentParser(description="Arnés diferencial legacy vs v2")
    parser.add_argument("--cases", required=True, help="JSON con casos sanitizados")
    parser.add_argument("--out", required=True, help="ruta del reporte JSON")
    args = parser.parse_args(argv)
    print("ticket_differential requiere runners live cableados en staging; "
          "no se ejecuta legacy/v2 desde la CLI local (Tarea 14).",
          file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
