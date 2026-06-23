# Etapa 0 — Contexto compartido (LEER PRIMERO en cada chat)

## Qué estamos construyendo
Un único endpoint `POST /api/v1/handle-ticket` (+ `GET /api/v1/tickets/{id}` para poll) que ejecuta
in-process todo el flujo que hoy orquesta n8n para responder tickets de participantes 401(k).

## Flujo actual en n8n (lo que reemplazamos)
```
ticket → route-inquiry (clasifica)
  ├─ knowledge_question → [agente Knowledge Question] → /knowledge-question → LLM final
  └─ generate_response  → [agente Inquiry Extraction] → /required-data
                        → [agente Forusbots field-mapper] → ForusBots scrape-participant
                        → [agente Generate Response Body Builder] → /generate-response
```
Los 4 "agentes" son prompts LLM que viven como markdown en `/External agents/`:
- `Inquiry Extraction & Required-Data Builder agent .md`
- `Knowledge Question Inquiry Generator.md`
- `Forusbots field mapper.md`
- `Generate Response Body Builder.md`

(OJO: existen copias más viejas en `/PA/n8n prompts/`. Para `Forusbots field mapper` la copia de PA
—`PROMPT_MODULE_BUILDER_AGENT_V2.md`— es **más nueva** (tiene Rule 10 + aliases extra). Para
`Generate Response Body Builder` la copia de `External agents/` es la canónica.)

## Decisiones acordadas (NO re-litigar)
1. **Contrato híbrido**: knowledge_question / needs_more_info → respuesta inline (`200`).
   generate_response → `202 {ticket_job_id, poll_url}` + poll en `GET /api/v1/tickets/{id}`.
2. **Inputs = solo subject + body** (+ username/email). Sin `ticket_messages` ni `tag`.
   `record_keeper` es input explícito. El modelo deja opcionales `ticket_messages`/`tag` para
   forward-compat, pero la lógica NO depende de ellos.
3. **LLM-first**: los 4 agentes se reimplementan como llamadas LLM internas vía `LLMRouter`
   (nuevos task types + `LLM_ROUTE_*` + prompt builders en `prompts.py`). Sin ports
   determinísticos del mapper/flattener.
4. **Rollout por etapas**: `TICKET_HANDLER_MODE: disabled|shadow|knowledge_only|full` (default
   `disabled`), replicando la maquinaria de `ROUTER_MODE`. Mantener los 4 endpoints como fallback.

## Arquitectura del nuevo endpoint
```
POST /handle-ticket
 1. extract_inquiries (LLM)  → [{inquiry, record_keeper, plan_type, topic, related_inquiries}]
       └ array vacío → needs_more_info (saludo)
 2. por inquiry: inquiry_router.classify(inquiry) → {knowledge_question|generate_response|needs_more_info}
 ── RÁPIDO (inline, 200) ──
   knowledge_question: kb_question_synthesis (LLM) → ask_knowledge_question()
   needs_more_info:    devolver user_message del router
 ── LENTO (job en background, 202 + ticket_job_id) ──
   generate_response:  get_required_data() → forusbots_field_map (LLM) →
                       ForusBotsClient.scrape_participant() → gr_body_build (LLM) →
                       generate_response()
 3. envelope unificado (route_taken + resultado por inquiry + diagnostics)
```
Si TODAS las inquiries son rápidas → `200` inline. Si ALGUNA es generate_response →
`asyncio.create_task(...)` + `202`. Correr el servicio con **`--workers 1`** para que el job store
in-process sea visible al GET de poll.

## Contrato de ForusBots (verificado contra docs live)
- Base URL: `https://forusbots-6jyh.onrender.com` · Auth header: **`x-auth-token: <SHARED_TOKEN>`**
- `POST /forusbot/scrape-participant` body `{participantId, modules:[{key, fields}], return, strict,
  timeoutMs}` → **`202 {jobId, queuePosition, estimate{avgDurationSeconds}, capacitySnapshot
  {maxConcurrency:3, running, queued}}`**. **Siempre async.**
- `POST /forusbot/scrape-plan` body `{planId, modules}` → `202 {jobId}`. Módulos de plan:
  `basic_info, plan_design, onboarding, communications, extra_settings, feature_flags`.
- **Poll** `GET /forusbot/jobs/:id` → `{state: queued|running|succeeded|failed|canceled, result,
  error, stage, elapsedSeconds}`. `result` poblado en `succeeded`. Scrape promedio 45–120s.
- Errores: `400` validación, `401` token, `404` no encontrado, `409` no cancelable, `422` semántico.
- Módulos de participante y sus campos exactos están documentados en
  `External agents/Forusbots field mapper.md` (census, savings_rate, plan_details, loans, payroll,
  mfa) — esa es la fuente del prompt `forusbots_field_map`.

## Piezas existentes a REUSAR (no reimplementar)
Ruta: `kb-rag-system/`
- `data_pipeline/rag_engine.py`
  - `RAGEngine.get_required_data(inquiry, record_keeper, plan_type, topic, related_inquiries)` → `RequiredDataResponse` (`required_fields: Dict[str, List[RequiredField]]`, …) — L429
  - `RAGEngine.generate_response(inquiry, record_keeper, plan_type, topic, collected_data, max_response_tokens, total_inquiries_in_ticket)` → `GenerateResponseResult` — L738. Degrada elegante con datos parciales (emite `blocked_missing_data` + `questions_to_ask`, L1075-1098).
  - `RAGEngine.ask_knowledge_question(question)` → `KnowledgeQuestionResult` — L1358 (rápido)
- `data_pipeline/inquiry_router.py` — `InquiryRouterEngine.classify(inquiry)` → `ClassificationResult{route, confidence, reasoning, signals, user_message}` (L479). Guardas: confidence<0.55→needs_more_info; KQ top_score<0.40→needs_more_info. Parser JSON defensivo `_safe_parse_classifier_json` (L353-406) reutilizable.
- `data_pipeline/llm_router.py` — `LLMRouter.call(task_type, system_prompt, user_prompt, max_tokens, force_fallback)` (L152). Tabla de routes en `build_routes_from_settings` (L422-461) construida desde `settings.LLM_ROUTE_*`. Provider se infiere del prefijo del nombre del modelo (`gpt-*`/`gemini-*`).
- `data_pipeline/prompts.py` — convención: `SYSTEM_PROMPT_<TASK>` + `build_<task>_prompt(...)`. Precedente de prompt interno grande: `SYSTEM_PROMPT_CLASSIFY_INQUIRY` (L796-934).
- `data_pipeline/execution_logger.py` — `ExecutionLogger` a Firestore; patrón de tragarse fallas (L84-87).
- `api/main.py` — lifespan que arma `app.state.{pinecone_uploader, llm_router, rag_engine, inquiry_router, execution_logger}` (L218-301); deps `get_rag_engine`/`get_inquiry_router` (L345-380); auth `verify_api_key` (X-API-Key); patrón de handler `route_inquiry_endpoint` (L790-916) y gating `_apply_router_mode` (L776-787).
- `api/models.py` — modelos reutilizables: `KnowledgeQuestionResponse` (L503), `GenerateResponseResult` (L320), `SourceArticle`, `UsedChunk`, `RouteInquiryRequest`/`Response` (L545-618).
- `api/config.py` — `Settings` (pydantic BaseSettings) + `ROUTER_MODE` (L51-59) + `validate_settings` (L98-151).
- `httpx` ya es dependencia. `cachetools.TTLCache` ya se usa en `rag_engine.py`.

## Convención de calidad
- No romper los 4 endpoints existentes (son fallback).
- Cada llamada LLM nueva usa salida JSON (el router ya la fuerza) + parser defensivo.
- El scrape de ForusBots es async-only: NUNCA asumir respuesta síncrona.
