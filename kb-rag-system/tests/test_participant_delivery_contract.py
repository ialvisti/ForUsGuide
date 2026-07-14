"""
Contrato de entrega final idempotente al participante (plan Tarea 9 Paso 4).

El sistema que publica el reply (DevRev vía el nodo n8n `final-handling`) vive
fuera de este repo, así que el fixture del contrato es un DESIDERÁTUM
verificable: define qué debe cumplir la entrega para que ``full`` pueda
activarse (STOP de GR). Mientras el contrato real no llegue (bloqueo de
Tarea 1 §4), el fixture declara los campos como PENDIENTE y los tests exigen
que el flujo derive a humano ante ambigüedad — nunca que garantice
exactly-once sin evidencia.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
    """El contrato idempotente de entrega final (Tarea 1 §4)."""

    def test_contract_fixture_present_or_documented_pending(self):
        """Sin el contrato real, el fixture NO debe existir con datos
        inventados; el bloqueo se documenta en 01-external-contracts.md."""
        if not CONTRACT.exists():
            pytest.skip(
                "contrato de entrega final PENDIENTE (Tarea 1 §4): no se "
                "inventa un fixture; ver docs/verification/handle-ticket/"
                "01-external-contracts.md"
            )
        contract = json.loads(CONTRACT.read_text())
        # cuando exista, debe declarar los campos que habilitan exactly-once
        assert "stable_key_accepted" in contract
        assert "delivery_id_reconciliation" in contract
        assert "max_redelivery_horizon_days" in contract
        assert "receiver_dedupe_retention_days" in contract
        assert "ambiguous_timeout_semantics" in contract

    def test_ambiguous_delivery_without_reconciliation_defers_to_human(self):
        """Si el canal no soporta key/consulta por correlation ID, el flujo
        deriva a humano ante ambigüedad y mantiene bloqueada la publicación
        automática — el ledger no convierte un timeout ambiguo en exactly-once."""
        if not CONTRACT.exists():
            pytest.skip("contrato de entrega final PENDIENTE (Tarea 1 §4)")
        contract = json.loads(CONTRACT.read_text())
        if not contract.get("stable_key_accepted") \
                and not contract.get("delivery_id_reconciliation"):
            assert contract.get("on_ambiguous") == "defer_to_human", (
                "sin idempotencia observable, un timeout ambiguo DEBE derivar "
                "a humano, no reenviar a ciegas"
            )
