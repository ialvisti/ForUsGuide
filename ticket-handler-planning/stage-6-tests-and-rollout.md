# Etapa 6 — Tests, arnés de paridad y rollout

> Prerrequisito: leer `00-context.md` + todas las etapas implementadas. Aquí se prueba, se verifica
> contra el flujo legacy, y se hace el rollout por etapas.

## Objetivo
Cobertura unit + arnés golden/paridad + arnés diferencial (nuevo vs legacy) + secuencia de rollout
segura con `TICKET_HANDLER_MODE`.

## Archivos a crear/extender
- `kb-rag-system/tests/test_forusbots_client.py`  (NUEVO)
- `kb-rag-system/tests/test_handle_ticket_endpoint.py`  (NUEVO)
- `kb-rag-system/tests/test_ticket_orchestrator.py`  (NUEVO)
- `kb-rag-system/tests/test_ticket_handler_golden.py`  (NUEVO)
- `kb-rag-system/tests/test_prompt_parity.py`  (NUEVO)
- `kb-rag-system/tests/fixtures/ticket_golden_cases.py`  (NUEVO)
- `kb-rag-system/rag-testing/test_endpoints_stress.py`  (EXTENDER con `call_ticket_handler`)

## 1) Tests unit (LLM + httpx mockeados)
- **`test_forusbots_client.py`**: submit→202→poll(queued→running→succeeded); failed/canceled →
  `ForusBotsJobFailed`; nunca-terminal → `ForusBotsTimeout`; dedupe inflight (dos llamadas
  idénticas = un POST); tope de semáforo. Mock `httpx.AsyncClient` (respx o monkeypatch, patrón como
  el mock de OpenAI en `tests/test_llm_router.py`).
- **`test_ticket_orchestrator.py`**: parchear engines + cliente + `llm_router.call` (JSON enlatado
  por `task_type`). Afirmar branching (KQ incl. insufficient→NMI; GR incl. scrape-fallido→degraded;
  NMI), multi-inquiry con linkage, y que `diagnostics` se pobla.
- **`test_handle_ticket_endpoint.py`**: `TestClient` con fixture estilo `tests/test_api.py:34-53`.
  Escenarios: KQ→200 inline; GR→202 job→poll a succeeded; NMI; 401 sin X-API-Key;
  `TICKET_HANDLER_MODE=disabled`→503; `shadow`→decisión fallback; idempotency-key.

## 2) Golden / paridad
- **`fixtures/ticket_golden_cases.py`**: cosechar los ejemplos Input/Output **verbatim** de los 4
  `External agents/*.md` (Inquiry Extraction 4 ejemplos + edge cases; Knowledge Question Ej1-7 —
  esp. Ej5 form+insufficient, Ej6 form+body claro; Field mapper Ej1-4 + Rule 10 de la copia PA;
  Generate Response Ej1-6 — esp. Ej4 `incoming_rollover`, Ej6 `rollover`-pese-a-"previous employer").
- **`test_ticket_handler_golden.py`**: como es LLM-first, asertar **semánticamente** (no exact-match):
  `topic` == documentado; flag `insufficient_inquiry` coincide; `related_inquiries` linkage;
  array vacío→NMI; marcadores de dirección rollover (Ej6 "out"/"current employer's plan", NO
  "incoming"); `modules` contienen claves esperadas (`Latest Payroll`, nunca `years:all`);
  `collected_data` con claves snake_case, `Pay Date URL` removido, nulls preservados. Dos modos:
  (a) LLM mockeado con output documentado (valida plumbing+parsing); (b) `@pytest.mark.live` nocturno
  (paridad real de comportamiento).
- **`test_prompt_parity.py`**: afirmar que strings clave de cada `.md` (encabezados de reglas,
  tokens de topic, nombres de campo, pasos del árbol rollover) aparecen como substring en la
  constante correspondiente de `prompts.py`. Falla CI si el `.md` y el prompt divergen.

## 3) Arnés diferencial (nuevo vs legacy)
Extender `rag-testing/test_endpoints_stress.py` (ya tiene `call_generate_response`/
`call_knowledge_question`/`call_route_inquiry`, L129-184) con `call_ticket_handler(...)`. Correr un
lote de tickets reales por AMBOS caminos (la secuencia legacy de 4 endpoints y el nuevo endpoint) y
diffear: array de `/required-data`, `modules` de forusbots, body de `/generate-response`. Usar como
baseline `rag-testing/gr31_live_endpoint_capture.json` y los `stress_test_results_*`. Condicionar el
paso a `full` sobre: cero diffs en campos determinísticos + tasa aceptable de match semántico en
campos LLM.

## 4) Secuencia de rollout
Mirror de `ROUTER_MODE`. Default `TICKET_HANDLER_MODE=disabled`.
1. **disabled** (deploy a oscuras): endpoint existe, devuelve 503. Verificar que los 4 endpoints
   legacy siguen intactos.
2. **shadow**: el endpoint corre el pipeline completo pero responde "usar fallback"; `ExecutionLogger`
   registra el output que hubiera dado junto al legacy. Correr el arnés diferencial con tráfico real.
3. **knowledge_only**: re-rutear en n8n SOLO los tickets que resuelven a `knowledge_question` (sin
   datos de participante → menor blast radius). Resto sigue legacy.
4. **full**: orquestación completa. n8n hace una llamada (rápidas) o llamada+poll (lentas).
5. **Mantener los 4 endpoints** indefinidamente como fallback; `ticket_handler_mode` por-request
   permite forzar el camino legacy para debug.

## Definition of Done
- [ ] `pytest` (unit + golden mockeado + parity) en verde.
- [ ] Arnés diferencial corre y reporta diffs; campos determinísticos sin diffs.
- [ ] Documentado en README cómo avanzar cada etapa del rollout y cómo hace fallback n8n.

## Cómo verificar (end-to-end manual)
1. `.env`: `FORUSBOTS_AUTH_TOKEN`, `LLM_ROUTE_*`, `TICKET_HANDLER_MODE=full`.
2. `cd kb-rag-system && source venv/bin/activate && bash scripts/start_api.sh`.
3. Ruta KQ: `POST /api/v1/handle-ticket` con subject/body factual → `200` inline con answer.
4. Ruta GR: ticket de balance/elegibilidad → `202 {ticket_job_id}` → poll
   `GET /api/v1/tickets/{id}` a `succeeded`; confirmar `jobId` real de ForusBots en diagnostics y
   `collected_data` poblado desde el scrape.
5. Sandbox ForusBots (`/docs/sandbox/`): validar submit+poll del cliente contra el contrato live.
