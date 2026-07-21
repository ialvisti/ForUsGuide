"""Smoke test a nivel de IMAGEN (plan Tarea 3 Paso 5).

Se ejecuta DENTRO del contenedor construido:

    docker run --rm --entrypoint python IMAGE scripts/container_smoke.py

Verifica que la imagen sea completa para el primer ticket real:
1. la app FastAPI importa (sin lifespan: no toca red);
2. los cinco prompts Markdown de runtime existen y sus builders cargan;
3. los modelos Pydantic request/response del contrato de tickets instancian.

Sale con código != 0 ante cualquier ausencia. La única salida en éxito es la
línea sanitizada `container-smoke: ok` (sin paths, versiones ni env).
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

EXPECTED_PROMPTS = (
    "extract_inquiries.md",
    "forusbots_field_map.md",
    "gr_body_build.md",
    "kb_question_synthesis.md",
    "ticket_field_extract.md",
)


def _fail(reason: str) -> None:
    print(f"container-smoke: FAIL — {reason}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    # 1) la app importa (module-level; el lifespan no corre sin servidor)
    try:
        from api.main import app  # noqa: F401
    except Exception:
        traceback.print_exc()
        _fail("api.main no importa")

    # 2) prompts de runtime presentes + builders cargan desde disco
    from data_pipeline import prompts

    prompts_dir = Path(prompts.__file__).resolve().parent / "agent_prompts"
    missing = [n for n in EXPECTED_PROMPTS if not (prompts_dir / n).is_file()]
    if missing:
        _fail(f"faltan prompts de runtime en la imagen: {missing}")

    case_data = {
        "userData": {"pptId": "1", "planId": "2", "companyName": "C",
                     "companyStatus": "Ongoing", "companyStatusDetail": None},
        "ticketData": {"userId": None, "userName": "u", "userEmail": "e",
                       "ticketId": None, "emailSubject": "s", "emailBody": "b",
                       "tag": None, "firstContact": None, "ticket_messages": {}},
        "forusbots": {"recordKeeper": "LT Trust"},
    }
    try:
        builders = (
            prompts.build_extract_inquiries_prompt(case_data),
            prompts.build_kb_question_synthesis_prompt(
                {"ticketData": case_data["ticketData"]}),
            prompts.build_forusbots_field_map_prompt(
                [{"field": "balance", "description": "d", "why_needed": "w",
                  "required": True}]),
            prompts.build_gr_body_build_prompt([{"caseData": case_data}]),
            prompts.build_ticket_field_extract_prompt(
                [{"field": "amount"}], {"emailSubject": "s", "emailBody": "b"}),
        )
    except Exception:
        traceback.print_exc()
        _fail("un builder de prompts no cargó")
    for pair in builders:
        if not (pair and pair[0] and pair[1]):
            _fail("un builder de prompts devolvió contenido vacío")

    # 3) modelos request/response del contrato instancian
    try:
        from api.models import HandleTicketRequest, TicketJobAcceptedV2
        from data_pipeline.ticket_job_models import TicketJobState, new_job_record

        HandleTicketRequest.model_validate({
            "participant_id": "1", "plan_id": "2", "company_name": "C",
            "company_status": "Ongoing",
            "ticket": {"username": "u", "user_email": "e@x.com",
                       "email_subject": "s", "email_body": "b"},
        })
        TicketJobAcceptedV2(
            ticket_job_id="j",
            state=TicketJobState.QUEUED,
            status_url="/api/v2/ticket-jobs/j",
            retry_after_seconds=3, idempotency_replayed=False,
        )
        new_job_record(principal_id="smoke", request_fingerprint="0" * 64)
    except Exception:
        traceback.print_exc()
        _fail("los modelos del contrato no instancian")

    print("container-smoke: ok")


if __name__ == "__main__":
    main()
