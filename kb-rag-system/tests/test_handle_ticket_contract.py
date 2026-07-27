"""
Consumer-contract tests del flujo handle-ticket contra los fixtures de n8n.

Los fixtures (tests/fixtures/n8n_handle_ticket_*.json) son la referencia
congelada del contrato consumidor. Hoy están RECONSTRUIDOS desde el repo
(no existe export sanitizado del workflow real); su `_meta.provenance` lo
declara. Cuando exista el export real, reemplazar los fixtures manteniendo
estos tests en verde.

Decisiones de contrato que estos tests fijan (Task 1 del plan de remediación):

1. Fuente de verdad del input: ``email_subject`` + ``email_body``.
   ``ticket_messages``/``tag`` se aceptan en el wire pero el runtime los
   ignora (ya no existen en ``TicketInput``).
2. Versionado: v2 será ``202 + polling`` uniforme sobre un job durable;
   v1 se conserva como adapter sobre el MISMO motor (nunca dos motores).
3. Sólo los campos de respuesta al participante son publicables; estados
   técnicos (partial/failed/timeout/cancelled) van a legacy/humano.
4. El deadline de poll de n8n debe superar el budget total del servidor.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.models import HandleTicketRequest

FIXTURES = Path(__file__).parent / "fixtures"

# Estados que el servidor puede publicar hoy (v1) más los reservados para el
# job durable (v2). El fixture de polling debe cubrirlos TODOS: un estado sin
# rama en n8n significa comportamiento indefinido en producción.
SERVER_STATES = {
    "queued", "running", "succeeded", "partial", "failed", "timeout", "cancelled",
}
TERMINAL_TECHNICAL_STATES = {"partial", "failed", "timeout", "cancelled"}

ALLOWED_ACTIONS = {
    "continue_polling",
    "inspect_state",
    "publish_participant_reply",
    "use_legacy_or_human",
    "alert_ops_and_use_legacy",
    "backoff_and_retry",
    "retry_then_use_legacy",
}

# Únicos paths de la respuesta que pueden llegar al participante.
PUBLISHABLE_ALLOWLIST = {
    "primary.needs_more_info_message",
    "primary.knowledge_answer.answer",
    "primary.generate_response.response.response_to_participant",
}


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(scope="module")
def request_fixture() -> dict:
    return _load("n8n_handle_ticket_request.json")


@pytest.fixture(scope="module")
def polling_fixture() -> dict:
    return _load("n8n_handle_ticket_polling.json")


class TestRequestContract:
    def test_real_n8n_fixture_validates(self, request_fixture):
        """El payload del fixture valida contra el modelo actual del endpoint."""
        req = HandleTicketRequest.model_validate(request_fixture["request"])
        assert req.participant_id == "158948"
        assert req.plan_id == "580"
        assert req.ticket.email_subject
        assert req.ticket.email_body

    def test_thread_and_tag_are_wire_compatible_but_ignored(self, request_fixture):
        """n8n puede seguir enviando ticket_messages/tag, pero el runtime no
        los modela: la fuente de verdad es subject + body."""
        raw_ticket = request_fixture["request"]["ticket"]
        # El fixture congela que n8n los envía hoy…
        assert "ticket_messages" in raw_ticket
        assert "tag" in raw_ticket
        # …y el modelo los descarta (extra="ignore"): no existen en runtime.
        req = HandleTicketRequest.model_validate(request_fixture["request"])
        assert "ticket_messages" not in type(req.ticket).model_fields
        assert "tag" not in type(req.ticket).model_fields

    def test_fixture_declares_unverified_provenance(self, request_fixture):
        """Mientras no exista el export real, el fixture debe declararlo para
        que nadie lo confunda con el contrato verificado."""
        assert "RECONSTRUIDO" in request_fixture["_meta"]["provenance"]


class TestPollingContract:
    def test_polling_fixture_handles_every_terminal_state(self, polling_fixture):
        on_state = polling_fixture["on_state"]
        missing = SERVER_STATES - set(on_state)
        assert not missing, f"estados del servidor sin rama en n8n: {sorted(missing)}"
        for state, cfg in on_state.items():
            assert cfg["action"] in ALLOWED_ACTIONS, (state, cfg["action"])

    def test_technical_states_are_never_published(self, polling_fixture):
        on_state = polling_fixture["on_state"]
        for state in TERMINAL_TECHNICAL_STATES:
            cfg = on_state[state]
            assert cfg["publishable"] is False, state
            assert cfg["action"] != "publish_participant_reply", state

    def test_succeeded_publish_requires_all_three_safety_conditions(
            self, polling_fixture):
        """Un job shadow termina succeeded con metadata.fallback=true; n8n
        debe tener el guard para no publicar el saludo interno (HT-11)."""
        succeeded = polling_fixture["on_state"]["succeeded"]
        guard = succeeded.get("guard", "")
        for required in (
            "fallback", "send_participant_reply", "participant_reply_safe",
            "manual_reconciliation_required",
        ):
            assert required in guard, (
                f"el fixture no declara el guard {required} para succeeded"
            )

    def test_error_http_statuses_have_fail_safe_actions(self, polling_fixture):
        on_http = polling_fixture["on_http_status"]
        for code in ("404", "410", "invalid_json"):
            assert "legacy" in on_http[code] or "human" in on_http[code], code

    def test_only_participant_reply_field_is_publishable(self, polling_fixture):
        published = set(polling_fixture["publishable_fields"])
        assert published <= PUBLISHABLE_ALLOWLIST, (
            f"campos fuera del allowlist publicable: {published - PUBLISHABLE_ALLOWLIST}"
        )
        forbidden_markers = ("diagnostics", "metadata", "error", "state")
        for path in published:
            for marker in forbidden_markers:
                assert marker not in path, (path, marker)

    def test_retry_deadline_exceeds_server_job_deadline(
        self, request_fixture, polling_fixture
    ):
        """n8n no debe abandonar (y mandar a legacy) jobs que el servidor aún
        considera vivos: su deadline de poll supera el budget del servidor."""
        from api.config import settings

        for deadline in (
            request_fixture["_meta"]["polling"]["deadline_s"],
            polling_fixture["poll"]["deadline_s"],
        ):
            assert deadline > settings.TICKET_TOTAL_BUDGET_S, (
                f"poll deadline {deadline}s <= server budget "
                f"{settings.TICKET_TOTAL_BUDGET_S}s"
            )
            assert deadline >= settings.TICKET_JOB_DEADLINE_S + 300


# ---------------------------------------------------------------------------
# Producción (plan de finalización, Tarea 2) — el consumidor debe declarar
# ramas para los errores del productor v2. RED hasta reemplazar los fixtures
# reconstruidos por el export real (Tarea 9).
# ---------------------------------------------------------------------------

class TestProducerErrorBranches:

    def test_request_fixture_declares_v2_producer_error_handling(self):
        """El fixture del request debe declarar cómo maneja n8n los errores
        del POST v2: 409 (conflicto de idempotencia → investigación del
        operador), 413 (payload demasiado grande) y 429 (Retry-After)."""
        fixture = _load("n8n_handle_ticket_request.json")
        on_status = fixture.get("_meta", {}).get("http", {}).get("on_status") or \
            fixture.get("on_http_status")
        assert on_status, (
            "RED: el fixture del request no declara manejo por status HTTP "
            "del POST (Tarea 9 Paso 1.7)"
        )
        for code in ("409", "413", "429"):
            assert code in on_status, (
                f"RED: sin rama para {code} en el POST v2 — comportamiento "
                "indefinido en producción"
            )

    def test_polling_fixture_routes_410_to_legacy_or_human(self):
        """El receipt de idempotencia sobrevive al job y GET devuelve 410
        durante todo el horizonte: n8n debe tratarlo como no-publicable."""
        fixture = _load("n8n_handle_ticket_polling.json")
        branch = fixture["on_http_status"].get("410")
        assert branch, "sin rama para 410"
        text = json.dumps(branch).lower()
        assert "legacy" in text or "human" in text, (
            f"la rama 410 ({branch!r}) no deriva a legacy/humano"
        )
