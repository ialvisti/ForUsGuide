# Etapa 3 — Agentes LLM internos (portar prompts + wiring de routes)

> Prerrequisito: leer `00-context.md` + Etapa 2 (config con `LLM_ROUTE_*`). LLM-first: los 4
> agentes de n8n se vuelven 4 task types internos.

## Objetivo
Portar los 4 prompts de `External agents/*.md` a `prompts.py` como builders, registrarlos en el
route_map del `LLMRouter`, y dejar funciones invocables vía `llm_router.call(task_type=...)`.

## Archivos a modificar
- `kb-rag-system/data_pipeline/prompts.py`  (nuevos prompt builders)
- `kb-rag-system/data_pipeline/llm_router.py`  (route_map)

## Los 4 task types

| task_type | fuente canónica | contrato de salida (JSON) |
|---|---|---|
| `extract_inquiries` | `External agents/Inquiry Extraction & Required-Data Builder agent .md` | array `[{inquiry, record_keeper, plan_type, topic, related_inquiries}]` (o `[]`) |
| `kb_question_synthesis` | `External agents/Knowledge Question Inquiry Generator.md` | `{question}` o `{question:null, insufficient_inquiry:true}` |
| `forusbots_field_map` | `Forusbots field mapper.md` **reconciliado con** `PA/n8n prompts/PROMPT_MODULE_BUILDER_AGENT_V2.md` | `{modules:[{key, fields:[...]}], _unmapped:[...]}` |
| `gr_body_build` | `External agents/Generate Response Body Builder.md` (canónica) | body de `/generate-response`: `{inquiry, record_keeper, plan_type, topic, collected_data{participant_data, plan_data}, context, max_response_tokens, total_inquiries_in_ticket}` |

## Adaptaciones obligatorias (inputs = solo subject + body)
- `extract_inquiries`: **quitar** toda lógica de `ticket_messages` y de `tag`/tabla-tag→topic.
  Fuente = `email_body` (si vacío, `email_subject`). `record_keeper` pasa directo desde el input
  (no resolver prioridades). El topic se infiere del texto (mantener la lista de topics conocidos
  del prompt como guía). Mantener: split multi-inquiry, `related_inquiries` recíprocos, paráfrasis
  en 3ª persona, regla "array vacío solo si no hay pedido accionable", "mensaje corto sigue siendo
  inquiry". Mantener la regla de subject `"Participant Advisory - Form Submission"` (subject está
  presente).
- `kb_question_synthesis`: mantener **Rule 0** (insufficient detection) y la **regla crítica de
  Form-Submission** (si subject == "Participant Advisory - Form Submission" usar solo email_body).
  Ignorar reglas de hilo de mensajes.
- `forusbots_field_map`: **RECONCILIAR PRIMERO** — la copia de `External agents/` NO tiene la
  Rule 10 (descomposición de predicados `whether_*`/`has_*`/`is_*`) ni ~8 aliases
  (`total_vested_balance`, `employer_match_vested_balance`, `roth_deferral_balance`,
  `rollover_balance`, `active_loans`→derivar de `Outstanding Balance > 0`, `participant_s_name`,
  etc.) que sí tiene `PROMPT_MODULE_BUILDER_AGENT_V2.md`. Fusionar ambas en un único prompt canónico
  antes de portar, o se pierde lógica en silencio. Conservar: catálogo de campos exactos por módulo,
  reglas de desambiguación (account_balance vs loan Account Balance; vested_balance→Account Balance;
  `latest_payroll`→`Latest Payroll` NUNCA `years:all`), expansión de compuestos, `_unmapped`.
- `gr_body_build`: como inquiry+topic ya vienen del paso 1, el foco es construir `collected_data` +
  `context` desde el output del scrape + case data. Mantener: flatten de campos (tablas
  census/savings/payroll/loans/mfa/plan_details), strip de `Pay Date URL`, normalización MFA,
  semántica null/empty preservada, `loan_history` string→`[]`, `max_loans` a `plan_data`.

## Patrón en `prompts.py`
Replicar `SYSTEM_PROMPT_CLASSIFY_INQUIRY` (L796-934) + `build_classify_inquiry_prompt` (L943):
```python
SYSTEM_PROMPT_EXTRACT_INQUIRIES = """..."""        # portado, con # SOURCE: External agents/...md
def build_extract_inquiries_prompt(ticket, record_keeper, company_name, company_status) -> Tuple[str,str]: ...
SYSTEM_PROMPT_KB_QUESTION_SYNTHESIS = """..."""
def build_kb_question_synthesis_prompt(ticket) -> Tuple[str,str]: ...
SYSTEM_PROMPT_FORUSBOTS_FIELD_MAP = """..."""
def build_forusbots_field_map_prompt(required_fields_flat) -> Tuple[str,str]: ...
SYSTEM_PROMPT_GR_BODY_BUILD = """..."""
def build_gr_body_build_prompt(ppt_modules, case_data, inquiry, topic, record_keeper, plan_type) -> Tuple[str,str]: ...
```
Cada constante lleva un comentario de procedencia `# SOURCE: <archivo .md> @ <git-sha>` para la
disciplina de sincronización (ver Etapa 6, `test_prompt_parity.py`).

## Wiring en `llm_router.py`
En `build_routes_from_settings` (route_map L430-437) agregar:
```python
"extract_inquiries": settings.LLM_ROUTE_EXTRACT_INQUIRIES,
"kb_question_synthesis": settings.LLM_ROUTE_KB_QUESTION_SYNTHESIS,
"forusbots_field_map": settings.LLM_ROUTE_FORUSBOTS_FIELD_MAP,
"gr_body_build": settings.LLM_ROUTE_GR_BODY_BUILD,
```
Opcional: overrides en `_TASK_EFFORT_OVERRIDES` (L407) — `extract_inquiries` y
`forusbots_field_map` se benefician de razonamiento medio; `kb_question_synthesis` es simple. No se
necesita cambiar `LLMRouter.call` (es genérico por task_type).

## Parsing
Las 4 usan salida JSON (el router ya la fuerza con `response_format` / `application/json`). Reusar
el parser defensivo estilo `_safe_parse_classifier_json` (inquiry_router.py L353-406) — uno para
shape array (`extract_inquiries`) y otro para objeto.

## Definition of Done
- [ ] Los 4 prompts portados (con comentario `# SOURCE:`), reconciliando el field-mapper.
- [ ] Las 4 entradas en el route_map; `validate_settings` pasa.
- [ ] Función helper de parsing por shape (array/objeto) con tolerancia a fences.
- [ ] Test rápido: `await llm_router.call("extract_inquiries", *build_extract_inquiries_prompt(...))`
      devuelve JSON parseable (con un ticket de ejemplo de los `.md`).

## Cómo verificar
- Tests con `llm_router.call` mockeado devolviendo el output documentado en los ejemplos de los
  `.md` → el parser produce la estructura esperada.
- (Live, opcional) correr una vez cada agente con un ejemplo de su `.md` y comparar semánticamente.
