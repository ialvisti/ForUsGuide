# F3 — Multi-Inquiry Split (financiero + seguridad/acceso): análisis sistemático y solución

> **Para un chat nuevo (sin contexto previo).** Este documento es autocontenido: describe el problema,
> lo ya hecho, las hipótesis de causa raíz, una metodología de análisis paso a paso, la solución recomendada
> con punteros exactos a archivo/función, y el plan de pruebas/aceptación. Sigue las secciones en orden.

---

## 0. TL;DR

Cuando un ticket de Participant Advisory mezcla **una acción financiera** (cash-out, rollover, loan, etc.)
con **un bloqueante de seguridad/acceso** (no puede iniciar sesión, email inválido/antiguo, email de reset
de password NO solicitado, problema de MFA), el sistema **NO separa** ese bloqueante en su propia inquiry
`account_access`. Resultado: la preocupación de seguridad se **dropea** o se **dobla** dentro de la respuesta
financiera, en vez de atenderse como inquiry propia.

Ya se aplicó un **fix de solo-prompt** (regla + ejemplo en `extract_inquiries.md`) que está **verificado a nivel
unitario** pero **falló en vivo** (el LLM no aplicó la regla). La solución robusta es un **guard determinístico
en el orquestador** que fuerce la 2ª inquiry `account_access` cuando detecte la señal en el ticket. Este
documento define cómo analizarlo y resolverlo.

---

## 1. Contexto del sistema

- Repo: `/Users/ivanalvis/Desktop/ForUsGuide` · app FastAPI en `kb-rag-system/`.
- Endpoint end-to-end: `POST /api/v1/handle-ticket` (extract → route → KQ | required-data→ForusBots→generate-response).
- Orquestación: `kb-rag-system/data_pipeline/ticket_orchestrator.py`.
- Extracción de inquiries (prompt LLM): `kb-rag-system/data_pipeline/agent_prompts/extract_inquiries.md`.
- Reporte de evaluación que originó este fix: `kb-rag-system/rag-testing/eval_reports/eval_2026-06-22.md`
  (busca "F3" y los casos C4/C5).
- Arnés de pruebas reusable: `kb-rag-system/rag-testing/` (helpers `call_handle_ticket`, etc.) y
  `kb-rag-system/rag-testing/eval_reports/` (raw captures de corridas previas).

### Cómo fluye la extracción (lo relevante)
1. `TicketOrchestrator.extract_inquiries(req)` (`ticket_orchestrator.py` ~líneas 99–135) llama al LLM con
   `prompts.build_extract_inquiries_prompt(...)` (carga `extract_inquiries.md`) y parsea un **array JSON** de
   `{inquiry, record_keeper, plan_type, topic, related_inquiries}` en objetos `ExtractedInquiry`
   (dataclass ~líneas 52–57).
2. El loop de dispatch (~línea 165) atiende `extracted[: 1 + self._max_related]` (default 4) — atiende
   **exactamente lo que el extractor devolvió**. `total_inquiries_in_ticket == len(extracted)`.
3. `handle_inquiry` (~líneas 139–155) clasifica y rutea CADA inquiry de forma independiente
   (rutas heterogéneas ya soportadas: una puede ir a `generate_response` y otra a `needs_more_info`/`knowledge_question`).
4. `related_inquiries` se pasa a `get_required_data` (~línea 225).

**Conclusión clave:** el downstream (dispatch heterogéneo + `related_inquiries`) **ya soporta 2 inquiries**.
El cuello de botella es **la extracción**: si el LLM devuelve 1 objeto, la mitad de seguridad se pierde.

---

## 2. Lo que YA está implementado (no rehacer)

- **Prompt (solo-prompt fix), en AMBOS archivos por paridad** (`test_prompt_parity.py` exige igualdad verbatim):
  - `kb-rag-system/data_pipeline/agent_prompts/extract_inquiries.md`
  - `External agents/Inquiry Extraction & Required-Data Builder agent .md`
  - Cambios: (a) bullet en STEP 5 — *"un bloqueante de seguridad/acceso … es SIEMPRE su propia inquiry
    `account_access`, nunca se fusiona"*; (b) RULE 11 reforzándolo; (c) un "Example — Financial Request +
    Security/Access Blocker (MUST split)".
- **Tests existentes:**
  - `tests/test_ticket_agent_prompts.py::TestPromptBuilders::test_extract_inquiries_prompt` — asserta que la
    regla `account_access` está en el system prompt.
  - `tests/test_ticket_orchestrator.py::TestExtraction::test_extract_splits_financial_and_account_access` —
    confirma que SI el LLM devuelve 2 objetos (cash-out + account_access con `related_inquiries`), el
    orquestador los carga correctamente. (Prueba el downstream, no la decisión del LLM.)

---

## 3. La falla verificada (evidencia)

Corrida end-to-end contra una instancia con el fix de prompt aplicado (artefactos en
`kb-rag-system/rag-testing/eval_reports/verify_C4_postfix2.json` y `verify_C5_postfix2.json`):

- **C4** — body: *"…no longer works… wants to speak with someone about cashing out their 401(k). They also
  received a password reset email but did not request it."*
  → `total_inquiries=1`, primary = `termination_distribution_request`. La inquiry de seguridad
  (reset no solicitado) **se dropeó por completo** (ni siquiera quedó en `escalation`).
- **C5** — body: ex-empleado + rollover a Schwab + *"login inaccessible … email no longer valid"*.
  → `total_inquiries=1`, primary = rollover (route GR). El acceso quedó **doblado** en `escalation.reason`,
  no como inquiry `account_access` propia.

**La regla está en el prompt (verificada en disco + unit test) pero el LLM no la aplicó.** Es decir:
**solo-prompt es insuficiente.**

### Confound importante (resolver/controlar antes de analizar)
La extracción cae a `openai:gpt-5.5` porque **`GEMINI_API_KEY` está inválido** (la política de la organización
en Google Cloud **prohíbe API keys**; ver §6). El modelo afinado de extracción es Gemini; el fallback gpt-5.5
puede seguir la regla peor. **Antes de concluir, arregla Gemini vía ADC (Vertex) y re-prueba** — o prueba
explícitamente en ambos modelos. No se sabe aún si solo-prompt bastaría con el modelo correcto.

---

## 4. Hipótesis de causa raíz (a discriminar)

H1. **Modelo:** el fallback gpt-5.5 no respeta la regla; con Gemini (Vertex/ADC) sí. → Arreglar Gemini y re-probar.
H2. **Fuerza/ubicación del prompt:** la regla compite con "Focus on the participant's CURRENT need" (STEP 5),
    que lleva al LLM a tratar la seguridad como secundaria/resuelta. → Reordenar/reforzar.
H3. **No-determinismo:** el split ocurre a veces. → Medir tasa de acierto en N corridas por caso.
H4. **Determinismo necesario:** ningún prompt garantiza el split en este dominio crítico. → Guard en el orquestador
    (solución recomendada, §5).

---

## 5. Solución recomendada — guard determinístico en el orquestador

Hacer el split **independiente del modelo**: detectar la señal de seguridad/acceso en el texto del ticket y,
si el extractor NO emitió una inquiry `account_access`, **inyectarla** (con `related_inquiries` cruzado).

### Dónde
`kb-rag-system/data_pipeline/ticket_orchestrator.py`, dentro de (o justo después de) `extract_inquiries(req)`
(~líneas 99–135), antes del `return extracted`.

### Diseño
1. **Detector de señal** (determinístico, conservador). Detectar sobre el cuerpo del ticket
   (`req.ticket.email_subject + email_body`, en minúsculas) frases como:
   - reset no solicitado: `"password reset" + ("didn't request"|"did not request"|"didn't ask"|"not request"|"never requested")`
   - no puede entrar: `"can't log in" | "cannot log in" | "can't access" | "cannot access" | "locked out" | "unable to log in"`
   - email inválido: `"email is no longer valid" | "email no longer valid" | "old email" | "invalid email" | "email … no longer …"`
   - MFA: `"mfa" | "authenticator" | "two-factor" | "verification code"`
   Usar coincidencia por frase acotada (ver `_contains_bounded_phrase` en `rag_engine.py`) para evitar falsos positivos
   (p.ej. no disparar por la palabra "email" sola). Mantener la lista conservadora; preferir falso-negativo a
   falso-positivo (un split de más confunde menos que uno de menos, pero igual evitar ruido).
2. **Guard:** si la señal está presente Y ninguna inquiry extraída tiene `topic == "account_access"`:
   - Construir el texto de la inquiry de acceso a partir de la señal detectada (tercera persona, p.ej.
     *"Participant reports they cannot access their account / received an unsolicited password-reset email and is
     concerned about unauthorized access."*).
   - Crear un `ExtractedInquiry(topic="account_access", record_keeper=req.record_keeper, plan_type=..., inquiry=<texto>,
     related_inquiries=[<textos de las financieras>])`.
   - Poblar `related_inquiries` de las inquiries financieras existentes con el texto de la nueva (cross-link).
   - Insertarla en la lista (al final, para no robarle el slot primario a la financiera; el loop de dispatch
     atiende hasta `1 + _max_related`, default 4, así que cabe).
3. **No duplicar:** si el extractor YA emitió una `account_access`, no inyectar.
4. **Logging:** registrar cuando el guard inyecta (para observabilidad y para medir cuán seguido el LLM falla el split).

### Reutilizar
- `_contains_bounded_phrase` / `_contains_any` (en `rag_engine.py`) — o replicar helpers locales en el orquestador.
- `account_access` ya es un topic conocido (`extract_inquiries.md`).
- El dispatch heterogéneo y `related_inquiries` ya funcionan — no tocar.

### Riesgos / cuidar
- Falsos positivos (split espurio): la palabra "email"/"access" aparece en muchos tickets. Por eso exigir
  **frases compuestas** (reset+not-requested, can't+log-in, email+no-longer-valid), no tokens sueltos.
- No romper el caso de 1-sola-inquiry de seguridad (p.ej. ticket que es SOLO acceso) — ahí el extractor ya
  debería devolver `account_access`; el guard no debe duplicar.
- Mantener el fix de prompt (es complementario y barato).

---

## 6. Nota: arreglar Gemini (bloquea/condiciona el análisis de H1)

La política de la organización **prohíbe API keys** (mensaje en Google Cloud: *"API Keys are Disallowed … use
Application Default Credentials (ADC) instead"*). Por eso `GEMINI_API_KEY` está inválido y toda llamada LLM
cae al fallback OpenAI. **El código ya soporta Vertex/ADC** (`data_pipeline/llm_router.py` ~líneas 110–119:
`genai.Client(vertexai=True, project=..., location=...)` cuando `USE_VERTEX_AI=true` + `GCP_PROJECT`).

**Para arreglar (sin key):**
1. En `.env`: `USE_VERTEX_AI=true`, `GCP_PROJECT=<tu-proyecto>`, `GCP_LOCATION=<región, p.ej. us-central1>`,
   y vaciar `GEMINI_API_KEY`. (Confirmar nombres exactos en `api/config.py` ~líneas 42–43 y alrededores;
   `has_gemini` chequea `USE_VERTEX_AI and GCP_PROJECT`.)
2. Autenticar ADC en el entorno donde corre el server: `gcloud auth application-default login` (local) o el
   script de la consola `bash <(curl -sSL https://storage.googleapis.com/cloud-samples-data/adc/setup_adc.sh)`,
   o adjuntar una service account (Cloud Run/GCP deploy).
3. Asegurar `google-genai` instalado (ya lo está; el import funciona).
4. Verificar: reiniciar el server y confirmar que `/health` está OK y que `route-inquiry` ya **no** loguea
   `gemini … 400 API_KEY_INVALID … Falling back to openai`.

Hacer esto **antes** de re-probar F3 permite discriminar H1 (¿el split funciona con Gemini?).

---

## 7. Metodología de análisis (pasos para el chat nuevo)

1. **Leer** `eval_2026-06-22.md` (sección F3 + casos C4/C5) y los `verify_C4/C5_postfix2.json`.
2. **Arreglar Gemini/ADC** (§6) en una instancia de prueba (NO tocar el server de producción en :8000;
   levantar una instancia temporal en otro puerto, p.ej. `uvicorn api.main:app --port 8001`).
3. **Instrumentar el output crudo del extractor:** loguear el array JSON que devuelve `extract_inquiries`
   (system+user prompt y respuesta) para C4/C5, para ver exactamente qué emite el LLM.
4. **Medir H1/H3:** correr C4 y C5 N=5 veces cada uno con Gemini (Vertex) y contar cuántas veces hace el split
   (`total_inquiries==2`, con una `account_access`). Repetir con el fallback OpenAI. Reportar tasas.
5. **Decidir:** si Gemini hace split de forma confiable (≥4/5), quizá baste prompt + arreglar Gemini.
   Si no, implementar el **guard determinístico** (§5).
6. **Implementar** el guard (si aplica) + mantener el prompt.
7. **Verificar** (§8) y reportar antes/después.

---

## 8. Plan de pruebas / criterios de aceptación

### Unit (venv: `kb-rag-system/venv/bin/python -m pytest`)
- Nuevo: `tests/test_ticket_orchestrator.py` — el guard inyecta `account_access` cuando el LLM stub devuelve
  SOLO la financiera pero el ticket tiene señal de seguridad; NO duplica cuando ya viene `account_access`;
  NO dispara en tickets sin señal (no falso positivo).
- Mantener verdes: `test_extract_splits_financial_and_account_access`, `test_prompt_parity`,
  `test_ticket_agent_prompts`.

### End-to-end (instancia temporal, NO :8000)
- C4 → `total_inquiries == 2`; topics `{termination_distribution_request, account_access}`; la inquiry de
  seguridad atendida (no dropeada).
- C5 → `total_inquiries == 2`; topics `{rollover (o termination_distribution_request), account_access}`;
  el rollover SE procesa Y el acceso se atiende como inquiry propia.
- Harness: usar/copiar `rag-testing/...` runner apuntando a la instancia temporal (ver `eval_reports/` para
  el formato de captura). Correr **secuencialmente** (un caso a la vez) para evitar timeouts por saturación
  del worker (los GR son pesados).

### Criterios de aceptación
- [ ] Gemini/ADC operativo (sin fallback 400) en la instancia de prueba.
- [ ] C4 y C5 producen 2 inquiries con una `account_access`, de forma **repetible** (≥4/5 corridas).
- [ ] La inquiry de seguridad recibe una respuesta accionable (verificación de identidad / alerta de acceso
      no autorizado / siguiente paso), no solo un "contacta a Support" genérico.
- [ ] Sin falsos positivos: tickets puramente financieros (sin señal de acceso) siguen con 1 inquiry.
- [ ] Suite de tests verde (salvo las 3 fallas pre-existentes ya conocidas, ver §9).

---

## 9. Restricciones y notas

- **No commitear sin OK del usuario.** Varios archivos del subsistema ticket-handler están **untracked** (WIP):
  `data_pipeline/forusbots_*.py`, `ticket_orchestrator.py`, `agent_prompts/`, etc. Un commit parcial sería
  inconsistente; commitear el subsistema completo solo con aprobación.
- **Paridad de prompts:** cualquier edición a `extract_inquiries.md` debe replicarse byte-idéntica en
  `External agents/Inquiry Extraction & Required-Data Builder agent .md` (lo valida `test_prompt_parity.py`).
  Forma simple: editar el empaquetado y `cp` al spec.
- **Fallas de tests pre-existentes (ajenas a F3, no regresiones):**
  (1) `test_llm_router.py::...test_unknown_task_raises` — `asyncio.get_event_loop()` deprecado en Python 3.14;
  (2) `test_kb_datapoint_alignment.py` — `first_contribution_posted_status` en un artículo no relacionado;
  (3) `test_blocking_intent.py` (loan) — contradicción `decision_guide.missing_data_conditions` ↔ `nice_to_have`
  ("Participant Name"). Ninguna la introduce F3.
- **No tocar el server de producción en :8000.** Verificar siempre en instancia temporal en otro puerto.

---

## 10. Punteros rápidos (archivos/funciones)

| Qué | Dónde |
|---|---|
| Extracción de inquiries | `data_pipeline/ticket_orchestrator.py::extract_inquiries` (~99–135) |
| Dataclass de inquiry | `data_pipeline/ticket_orchestrator.py::ExtractedInquiry` (~52–57) |
| Dispatch heterogéneo + cap | `data_pipeline/ticket_orchestrator.py` (~línea 165, `for ext in extracted[: 1 + self._max_related]`) |
| Prompt de extracción | `data_pipeline/agent_prompts/extract_inquiries.md` (STEP 5 ~129, RULES, ejemplo MUST split) |
| Spec de dominio (paridad) | `External agents/Inquiry Extraction & Required-Data Builder agent .md` |
| Builder del prompt | `data_pipeline/prompts.py::build_extract_inquiries_prompt` |
| Helpers de detección | `data_pipeline/rag_engine.py::_contains_bounded_phrase / _contains_any` |
| LLM routing / Vertex-ADC | `data_pipeline/llm_router.py` (~110–119, `_call_gemini` ~297) |
| Config Gemini/Vertex | `api/config.py` (~42–43, `has_gemini` ~146) |
| Reporte de eval (origen) | `kb-rag-system/rag-testing/eval_reports/eval_2026-06-22.md` |
| Evidencia de la falla | `kb-rag-system/rag-testing/eval_reports/verify_C4_postfix2.json`, `verify_C5_postfix2.json` |
