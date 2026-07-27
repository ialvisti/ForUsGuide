"""
Contrato de compatibilidad con la entrega existente de n8n/DevRev.

Este servicio no publica el reply. Devuelve un estado y ``next_action`` para
que el workflow existente mantenga exactamente sus ramas de entrega.
"""

from __future__ import annotations

import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "participant_delivery"
CONTRACT = FIXTURES / "live_contract.sanitized.json"


def _load_polling_fixture():
    path = Path(__file__).parent / "fixtures" / "n8n_handle_ticket_polling.json"
    return json.loads(path.read_text())


class TestPublishableRule:
    """Las tres condiciones de publicabilidad se aplican en el consumidor."""

    def test_only_succeeded_send_reply_is_publishable(self):
        fx = _load_polling_fixture()
        on_state = fx["on_state"]
        # succeeded es el ÚNICO estado con guard de publicación
        assert on_state["succeeded"].get("guard"), (
            "succeeded debe estar guardado por las tres condiciones"
        )
        for technical in ("partial", "failed", "timeout", "cancelled"):
            assert on_state[technical]["publishable"] is False, (
                f"{technical} nunca es publicable"
            )

    def test_running_and_queued_are_not_publishable(self):
        fx = _load_polling_fixture()
        assert fx["on_state"]["running"]["publishable"] is False
        assert fx["on_state"]["queued"]["publishable"] is False


class TestFinalDeliveryContract:
    """No inventar un segundo canal de delivery ni bloquear el merge por él."""

    def test_no_invented_delivery_adapter_contract_is_required(self):
        assert not CONTRACT.exists()

    def test_ambiguous_delivery_without_reconciliation_defers_to_human(self):
        on_state = _load_polling_fixture()["on_state"]
        for state in ("partial", "failed", "timeout", "cancelled"):
            assert on_state[state]["publishable"] is False
