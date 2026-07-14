"""
Probes de dependencias en VIVO (plan de finalización, Tarea 8 Paso 4).

Marcadas ``live_dependencies``: CI las excluye
(``-m "not live_dependencies and not staging_e2e"``); corren sólo en su gate.
Separación de efectos:

- TLS/health de ForusBots y la probe read-only de Pinecone pueden ejecutarse
  con credenciales aprobadas (sin efectos participant-facing);
- submit/poll de ForusBots o CUALQUIER efecto sólo DESPUÉS de G4 y con
  identidades sintéticas.

Ningún resultado contiene participantes, tokens, bodies upstream ni texto LLM.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.live_dependencies


def _require(*env_vars):
    missing = [v for v in env_vars if not os.getenv(v)]
    if missing:
        pytest.skip(f"faltan variables live: {missing}")


class TestForusBotsTransportLive:

    async def test_health_https_no_downgrade(self):
        """TLS + hostname válido + /health, sin datos de participante. NO
        requiere G4 (sin efectos)."""
        _require("FORUSBOTS_BASE_URL", "FORUSBOTS_AUTH_TOKEN")
        from data_pipeline.forusbots_client import ForusBotsClient

        base = os.environ["FORUSBOTS_BASE_URL"]
        assert base.lower().startswith("https://"), (
            "la probe live exige HTTPS; el HTTP actual se retira en Tarea 16"
        )
        client = ForusBotsClient(base_url=base,
                                 auth_token=os.environ["FORUSBOTS_AUTH_TOKEN"])
        try:
            result = await client.health()
            assert result["tls"] is True
            assert result["status_code"] == 200
        finally:
            await client.aclose()


class TestPineconeReadOnlyLive:

    def test_pinecone_stats_and_probe_readonly(self):
        """describe_index_stats + consulta sanitizada; sin writes. NO requiere
        G4 (sólo lectura contra kb-articles-production/kb_articles)."""
        _require("PINECONE_API_KEY")
        from data_pipeline.pinecone_uploader import PineconeUploader

        uploader = PineconeUploader(
            index_name=os.getenv("INDEX_NAME", "kb-articles-production"),
            namespace=os.getenv("NAMESPACE", "kb_articles"),
        )
        out = uploader.verify_readonly()
        assert out["namespace"] == os.getenv("NAMESPACE", "kb_articles")
        assert out["total_vectors"] is not None


class TestForusBotsEffectfulLive:
    """Submit/poll con efectos: SÓLO tras G4 y con identidades sintéticas
    (FORUSBOTS_G4_APPROVED=1 lo desbloquea)."""

    async def test_synthetic_submit_poll_reconciles_on_ambiguous(self):
        if not os.getenv("FORUSBOTS_G4_APPROVED"):
            pytest.skip("submit/poll con efectos requiere G4 aprobado")
        _require("FORUSBOTS_BASE_URL", "FORUSBOTS_AUTH_TOKEN",
                 "FORUSBOTS_SYNTHETIC_PARTICIPANT")
        from data_pipeline.forusbots_client import (
            ForusBotsAmbiguousSubmit,
            ForusBotsClient,
        )

        client = ForusBotsClient.from_settings_env()  # pragma: no cover
        try:
            try:
                result = await client.scrape_participant(
                    os.environ["FORUSBOTS_SYNTHETIC_PARTICIPANT"],
                    [{"key": "balance", "fields": ["total"]}],
                )
                assert result.job_id
            except ForusBotsAmbiguousSubmit as amb:
                # POST ambiguo: reconciliar por correlation/key, jamás
                # reenviar a ciegas; si no se puede, manual_reconciliation
                assert amb.needs_reconciliation is True
        finally:
            await client.aclose()
