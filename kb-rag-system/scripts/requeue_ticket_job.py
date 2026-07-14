"""
CLI administrativa auditada de requeue (plan de finalización, Tarea 7 Paso 4).

Reemplaza la recomendación incorrecta del runbook de recrear el MISMO nombre
de task. Reglas:

- rechaza jobs terminales y jobs con un lease de ejecución activo;
- incrementa la generación TRANSACCIONALMENTE (el nombre viejo queda quemado);
- encola la generación nueva;
- registra ÚNICAMENTE el hash del job, la generación y la identidad del
  operador — jamás PII, payload ni tokens.

Reservada para incidentes; la reparación rutinaria la hace el reconciliador
automático (data_pipeline.ticket_reconciler).

Uso:
    APP_ROLE=reconciler python -m scripts.requeue_ticket_job --job-id JOB \\
        --operator alice@forusall.com
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging

logger = logging.getLogger("requeue_ticket_job")


def _job_hash(job_id: str) -> str:
    return hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:16]


async def requeue(repo, queue, *, job_id: str, operator: str) -> int:
    from data_pipeline.ticket_job_models import TERMINAL_STATES, utcnow

    record = await repo.get(job_id)
    if record is None:
        logger.error("requeue rechazado: job %s no existe", _job_hash(job_id))
        return 2
    if record.state in TERMINAL_STATES:
        logger.error("requeue rechazado: job %s es terminal (%s)",
                     _job_hash(job_id), record.state.value)
        return 3
    # lease de ejecución activo → no interferir con el worker vivo
    if record.lease_expires_at is not None and utcnow() <= record.lease_expires_at:
        logger.error("requeue rechazado: job %s tiene lease de ejecución activo",
                     _job_hash(job_id))
        return 4

    generation = await repo.bump_enqueue_generation(job_id)
    name = await queue.ensure_enqueued(job_id, generation)
    await repo.mark_enqueued(job_id, name)
    logger.info("requeue OK job_hash=%s generation=g%d operator=%s",
                _job_hash(job_id), generation, operator)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Requeue administrativo auditado")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--operator", required=True,
                        help="identidad del operador (para auditoría)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    from data_pipeline.ticket_reconciler import _build_from_settings

    repo, queue = _build_from_settings()

    async def _run():
        try:
            return await requeue(repo, queue, job_id=args.job_id,
                                 operator=args.operator)
        finally:
            await queue.aclose()

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
