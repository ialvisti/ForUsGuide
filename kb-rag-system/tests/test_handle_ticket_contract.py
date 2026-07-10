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
