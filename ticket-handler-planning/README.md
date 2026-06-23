# Ticket Handler — Plan por etapas

Consolidar la orquestación de tickets de **n8n** (route-inquiry → required-data → ForusBots →
generate-response, con 4 agentes LLM) en **un solo endpoint end-to-end** dentro de la app FastAPI
de `kb-rag-system/`, con **mejor recuperación de ForusBots** (cliente async correcto).

Cada archivo `stage-*.md` está pensado para ejecutarse como **un chat independiente con Claude**.
Antes de empezar cualquier etapa, el chat debe leer **`00-context.md`** (contexto compartido:
problema, flujo actual, contrato de ForusBots, decisiones acordadas, piezas reutilizables).

## Decisiones acordadas (resumen)
1. **Contrato híbrido**: rutas rápidas (knowledge_question / needs_more_info) responden inline en
   una llamada; la ruta lenta (generate_response, dominada por el scrape async de ForusBots
   45–180s) devuelve `202 {ticket_job_id}` y n8n hace poll.
2. **Inputs = solo subject + body** (sin `ticket_messages` ni `tag` de DevRev). `record_keeper`
   llega como input explícito.
3. **LLM-first**: los 4 agentes de n8n se mantienen como llamadas LLM internas (prompts portados a
   `prompts.py`). Sin ports determinísticos.
4. **Rollout por etapas + arnés de paridad**: flag `TICKET_HANDLER_MODE`
   (disabled→shadow→knowledge_only→full); se mantienen los 4 endpoints existentes como fallback.

## Etapas y orden de dependencias

| Etapa | Archivo | Qué entrega | Depende de |
|---|---|---|---|
| 0 | `00-context.md` | Contexto compartido (leer siempre primero) | — |
| 1 | `stage-1-forusbots-client.md` | Cliente async de ForusBots (submit+poll, dedupe, semáforo) | — |
| 2 | `stage-2-config-and-models.md` | Settings (`FORUSBOTS_*`, `LLM_ROUTE_*`, `TICKET_*`) + modelos Pydantic | — |
| 3 | `stage-3-llm-agents-prompts.md` | Portar los 4 prompts de agente + wiring de routes LLM | Etapa 2 |
| 4 | `stage-4-orchestrator.md` | `TicketOrchestrator` (extract→classify→branch→kq/gr) | Etapas 1, 2, 3 |
| 5 | `stage-5-endpoints-and-jobs.md` | 2 endpoints FastAPI + job store + contrato híbrido + gating | Etapas 2, 4 |
| 6 | `stage-6-tests-and-rollout.md` | Tests unit, arnés golden/diferencial, rollout | Todas |

**Camino crítico**: 1 y 2 son paralelizables (independientes). 3 necesita 2. 4 necesita 1+2+3.
5 necesita 2+4. 6 al final.

## Cómo usar cada etapa como chat
1. Abrir un chat nuevo en `kb-rag-system/` (o en la raíz del repo).
2. Pegar/instruir: "Lee `ticket-handler-planning/00-context.md` y luego
   `ticket-handler-planning/stage-N-*.md` y ejecútalo."
3. Cada etapa termina con una sección **"Definition of Done"** y **"Cómo verificar"** — no avanzar
   a la siguiente etapa hasta que su DoD esté en verde.

## Regla transversal
No tocar ni romper los 4 endpoints existentes (`/required-data`, `/generate-response`,
`/knowledge-question`, `/route-inquiry`): son el fallback durante todo el rollout.
