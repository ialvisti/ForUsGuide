# Guía interna (Markdown) — Objeto JSON `kb_article_v2` para artículos de Knowledge Base

## 1. Resumen

### 1.1 Qué es el objeto
El objeto `kb_article_v2` es un **JSON estructurado** que representa un artículo de Knowledge Base (KB) transformado a un formato **estricto, consistente y anti-alucinación** para consumo por un **AI Support Agent interno** (por ejemplo, un agente en ChatGPT).

### 1.2 Para qué se usa
Se usa para:
- Convertir texto de un artículo KB en una **representación determinística y consultable**.
- Permitir que ChatGPT responda preguntas sobre el artículo sin inventar información, usando:
  - `summary` como capa compacta para recuperación rápida.
  - `details` como capa completa para decisiones, validaciones, pasos, FAQs y guardrails.
- Normalizar información (incluyendo links, emails, teléfonos) para prevenir inconsistencias de formato.

### 1.3 Reglas clave (visión general)
Reglas críticas del esquema:
- **Salida válida**: exactamente **3 keys top-level**: `metadata`, `summary`, `details`. No más, no menos.
- **No inventar**: usar SOLO `Article Content` + `Article Name` como fuente de verdad.
- **Completitud de schema**: **todas las keys definidas deben existir**; si no hay soporte:
  - Escalares → `null`
  - Arrays → `[]`
- **Formato de strings (anti-Markdown)**:
  - No usar Markdown dentro de strings JSON (sin `**bold**`, sin `[text](url)`, etc.).
  - Emails y URLs en **texto plano**; no `mailto:`.
  - Solo comillas rectas: `"` y `'` (si se necesita comilla dentro, escapar con `\"`).
- **Prohibido**: “ticket actions” (cerrar tickets, routing, tagging, escalations, workflows).
- **Fees**: si existen, deben ir en `details.fees` como objetos; si no existen → `details.fees = []`.
- **Plan variability**: si el artículo dice “may”, “depends”, etc., reflejarlo en:
  - `details.decision_guide`
  - `details.guardrails`
  - `details.response_frames`
- **Missing / Contradictory**:
  - Si la información es insuficiente/ambigua/contradictoria, NO se produce JSON.
  - Se produce SOLO un bloque Markdown con heading `Missing or Contradictory Information` y preguntas concretas.

---

## 2. Estructura del Objeto

### 2.1 Vista general del esquema (explicación jerárquica)
El objeto final SIEMPRE tiene esta forma:

- `metadata` (identidad, trazabilidad, clasificación)
- `summary` (capa compacta, multi-artículo, lista de reglas/pasos clave, flags)
- `details` (capa completa, pasos, reglas, fees, required_data, decision_guide, response_frames, etc.)

### 2.2 Árbol de keys (diagrama en texto)

root
├─ metadata
│  ├─ article_id
│  ├─ title
│  ├─ description
│  ├─ audience
│  ├─ record_keeper
│  ├─ plan_type
│  ├─ scope
│  ├─ tags[]
│  ├─ language
│  ├─ last_updated
│  ├─ schema_version
│  ├─ transformed_at
│  ├─ source_last_updated
│  └─ source_system
├─ summary
│  ├─ purpose
│  ├─ topic
│  ├─ subtopics[]
│  ├─ required_data_summary[]
│  ├─ key_business_rules[]
│  ├─ key_steps_summary[]
│  ├─ high_impact_faq_pairs[]
│  │  └─ { question, answer }
│  ├─ plan_specific_guardrails[]
│  └─ critical_flags
│     ├─ portal_required
│     ├─ mfa_relevant
│     └─ record_keeper_must_be
└─ details
   ├─ business_rules[]
   │  └─ { category, rules[] }
   ├─ fees[]
   │  └─ { service, fee, notes }
   ├─ steps[]
   │  └─ { step_number, type, visibility, description, notes }
   ├─ common_issues[]
   │  └─ { issue, resolution }
   ├─ examples[]
   │  └─ { scenario, outcome }
   ├─ additional_notes[]
   │  └─ { category, notes[] }
   ├─ faq_pairs[]
   │  └─ { question, answer }
   ├─ definitions[]
   │  └─ { term, definition }
   ├─ guardrails
   │  ├─ must_not[]
   │  └─ must_do_if_unsure[]
   ├─ references
   │  ├─ participant_portal
   │  ├─ internal_articles[]
   │  ├─ external_links[]
   │  └─ contact
   │     ├─ email
   │     ├─ phone
   │     └─ support_hours
   ├─ required_data
   │  ├─ must_have[]
   │  ├─ nice_to_have[]
   │  ├─ if_missing[]
   │  └─ disambiguation_notes[]
   ├─ decision_guide
   │  ├─ supported_outcomes[]
   │  ├─ eligibility_requirements[]
   │  ├─ blocking_conditions[]
   │  ├─ missing_data_conditions[]
   │  ├─ allowed_conclusions[]
   │  └─ not_allowed_conclusions[]
   └─ response_frames
      ├─ can_proceed
      ├─ blocked_missing_data
      ├─ blocked_not_eligible
      └─ ambiguous_plan_rules
         └─ { participant_message_components[], next_steps[], warnings[], questions_to_ask[], what_not_to_say[] }

### 2.3 Tabla resumen (key | tipo | requerido | descripción corta)

| Key | Tipo | Requerido | Descripción corta |
|---|---|---:|---|
| metadata | object | Sí | Identidad, clasificación y trazabilidad del artículo |
| metadata.article_id | string | Sí | ID único en lower_snake_case derivado del título |
| metadata.title | string | Sí | Título legible del artículo |
| metadata.description | string | Sí | descripcion detallada del artículo |
| metadata.audience | string | Sí | Público objetivo; normalmente "Internal AI Support Agent" |
| metadata.record_keeper | string\|null | Sí | Record keeper; inferible por prefijo en título si aplica |
| metadata.plan_type | string\|null | Sí | Tipo de plan (ej. "401(k)") si existe |
| metadata.scope | enum string | Sí | "global" \| "recordkeeper-specific" \| "plan-specific" |
| metadata.tags | string[] | Sí | 3–10 tags soportados por contenido/título |
| metadata.language | string | Sí | Idioma, por defecto "en-US" |
| metadata.last_updated | string\|null | Sí | Fecha real de actualización del artículo (si existe) |
| metadata.schema_version | string | Sí | Debe ser "kb_article_v2" |
| metadata.transformed_at | string | Sí | Fecha actual (YYYY-MM-DD) de transformación |
| metadata.source_last_updated | string\|null | Sí | Fecha fuente si aparece explícitamente |
| metadata.source_system | string\|null | Sí | Sistema fuente si se indica explícitamente |
| summary | object | Sí | Resumen multi-artículo para consumo rápido |
| summary.purpose | string | Sí | 1–2 frases del propósito |
| summary.topic | string\|null | Sí | Etiqueta consistente si es clara; si no, null |
| summary.subtopics | string[] | Sí | Subtemas (o []) |
| summary.required_data_summary | string[] | Sí | 3–7 bullets con prefijo (source_type), o [] |
| summary.key_business_rules | string[] | Sí | 3–8 reglas clave |
| summary.key_steps_summary | string[] | Sí | 3–7 pasos de alto nivel |
| summary.high_impact_faq_pairs | object[] | Sí | 2–5 FAQs de alto impacto (question/answer) |
| summary.plan_specific_guardrails | string[] | Sí | 1–5 guardrails específicos |
| summary.critical_flags | object | Sí | Flags: portal_required, mfa_relevant, record_keeper_must_be |
| details | object | Sí | Capa detallada operable del artículo |
| details.business_rules | object[] | Sí | Reglas por categoría (eligibility, docs, etc.) |
| details.fees | object[] | Sí | Fees estructurados; [] si no hay |
| details.steps | object[] | Sí | Pasos secuenciales con visibilidad y notas |
| details.common_issues | object[] | Sí | Problemas típicos y resolución |
| details.examples | object[] | Sí | Escenarios y outcome recomendado |
| details.additional_notes | object[] | Sí | Notas por categoría (ej. processing_times) |
| details.faq_pairs | object[] | Sí | FAQs detallados (question/answer) |
| details.definitions | object[] | Sí | Definiciones reusables (term/definition) |
| details.guardrails | object | Sí | Must_not y must_do_if_unsure |
| details.references | object | Sí | Portal, links, artículos internos, contacto |
| details.required_data | object | Sí | Must_have/nice_to_have/if_missing/disambiguation_notes |
| details.decision_guide | object | Sí | Guía determinística de outcomes y conclusiones |
| details.response_frames | object | Sí | Componentes de respuesta por outcome |

---

## 3. Guía Key por Key (detallada)

> Nota: En toda la guía, “requerido” significa “la key debe existir en el JSON”. Aun si no hay datos, debe existir con `null` o `[]` según corresponda.

### 3.1 `metadata`
- Key exacta: `metadata`
- Ruta: `root.metadata`
- Tipo: `object`
- Requerido: Sí (siempre)
- Descripción: Contiene identidad del artículo, clasificación, tags y trazabilidad.
- Validaciones:
  - Debe incluir TODAS sus subkeys definidas.
  - `schema_version` debe ser exactamente `"kb_article_v2"`.
  - `transformed_at` debe ser fecha de hoy en `YYYY-MM-DD`.
- Valores por defecto:
  - `audience`: "Internal AI Support Agent" salvo evidencia clara de otro público.
  - `language`: "en-US" salvo evidencia de otro idioma.
- Ejemplos correctos (estructura):
  - metadata con campos completos
  - metadata con nulos donde no hay soporte
- Ejemplo incorrecto:
  - Omitir `source_system` o `source_last_updated` (deben existir aunque sean null)
- Edge cases:
  - `record_keeper` puede inferirse del prefijo del título (ej. "LT:" → "LT Trust") si el prompt lo permite y es claro.

#### 3.1.1 `metadata.article_id`
- Key exacta: `article_id`
- Ruta: `root.metadata.article_id`
- Tipo: `string`
- Requerido: Sí
- Descripción: Identificador corto único en `lower_snake_case` basado en el título.
- Reglas/validaciones:
  - Formato recomendado (regex orientativa): `^[a-z0-9]+(_[a-z0-9]+)*$`
  - Debe ser “short” (evitar frases largas).
- Valores permitidos: cualquier string que cumpla formato lower_snake_case.
- Default: ninguno (se genera siempre).
- Ejemplos correctos:
  - `"hardship_withdrawal_overview"`
  - `"lt_trust_distribution_process"`
- Ejemplo incorrecto:
  - `"Hardship Withdrawal Overview"` (tiene espacios y mayúsculas)
- Edge cases:
  - Si el título contiene siglas, preferir minúsculas (ej. "401k" o "401_k" según convención interna, pero mantener consistencia).

#### 3.1.2 `metadata.title`
- Ruta: `root.metadata.title`
- Tipo: `string`
- Requerido: Sí
- Descripción: Título legible del artículo (idealmente el Article Name).
- Validaciones:
  - No vacío.
- Ejemplos correctos:
  - `"LT Trust: Hardship Withdrawals"`
  - `"Distribution Termination: Withdrawal vs Rollover"`
- Incorrecto:
  - `""` (vacío)
- Edge cases:
  - Si el Article Name incluye prefijo útil (record keeper), conservarlo.

#### 3.1.3 `metadata.description`
- Ruta: `root.metadata.description`
- Tipo: `string`
- Requerido: Sí
- Descripción: Descripcion completa y detallada que explica de qué trata el Artículo.
- Validaciones:
  - No vacío.
- Ejemplos correctos:
  - `"Guide a former employee through requesting a termination distribution from a ForUsAll-administered 401(k), including cash withdrawal, rollover, or a split transaction, using the participant portal or a RightSignature electronic form when portal access is not possible. This article is specific to the recordkeeper LT Trust."`
- Incorrecto:
  - `""` (vacío)
- Edge cases:
  - Si el contenido del articulo incluye prefijos útiles (record keeper), conservarlos.

#### 3.1.4 `metadata.audience`
- Ruta: `root.metadata.audience`
- Tipo: `string`
- Requerido: Sí
- Descripción: Público objetivo. Normalmente interno.
- Valores permitidos:
  - Recomendado fijo: `"Internal AI Support Agent"` (salvo evidencia).
- Default:
  - `"Internal AI Support Agent"`
- Ejemplos correctos:
  - `"Internal AI Support Agent"`
  - `"Internal Support Agent"` (solo si el artículo lo exige explícitamente; si no, evitar cambios)
- Incorrecto:
  - `null` (esta key no debe ser null bajo el estándar; el prompt indica usar string usualmente)
- Edge cases:
  - Si el artículo está dirigido a participantes, se puede ajustar, pero debe estar soportado por el texto.

#### 3.1.5 `metadata.record_keeper`
- Ruta: `root.metadata.record_keeper`
- Tipo: `string | null`
- Requerido: Sí
- Descripción: Nombre del record keeper.
- Reglas:
  - Población solo si:
    - El cuerpo lo menciona explícitamente, o
    - El título tiene un prefijo claro (ej. "LT:" / "LT Trust:").
  - Si no hay soporte → `null`.
- Ejemplos correctos:
  - `"LT Trust"`
  - `null`
- Incorrecto:
  - `"Fidelity"` (si no aparece en título/contenido)
- Edge cases:
  - Títulos ambiguos sin prefijo explícito → `null`.

#### 3.1.6 `metadata.plan_type`
- Ruta: `root.metadata.plan_type`
- Tipo: `string | null`
- Requerido: Sí
- Descripción: Tipo de plan, p.ej. `"401(k)"`.
- Reglas:
  - Solo si el artículo lo dice explícitamente.
- Ejemplos correctos:
  - `"401(k)"`
  - `null`
- Incorrecto:
  - `"403(b)"` si no aparece.
- Edge cases:
  - Si el artículo usa variaciones (401k vs 401(k)), conservar tal cual o normalizar solo si se indica.

#### 3.1.7 `metadata.scope`
- Ruta: `root.metadata.scope`
- Tipo: `string (enum)`
- Requerido: Sí
- Descripción: Alcance del artículo.
- Valores permitidos:
  - `"global"`
  - `"recordkeeper-specific"`
  - `"plan-specific"`
- Reglas:
  - `"recordkeeper-specific"` si está atado a un record keeper.
  - `"plan-specific"` si se refiere a un plan/employer/plan ID específico.
  - `"global"` si aplica a múltiples record keepers/planes.
- Ejemplos correctos:
  - `"global"`
  - `"recordkeeper-specific"`
- Incorrecto:
  - `"rk-specific"` (no permitido)
- Edge cases:
  - Si el título sugiere record keeper, preferir `"recordkeeper-specific"`.

#### 3.1.8 `metadata.tags`
- Ruta: `root.metadata.tags`
- Tipo: `string[]`
- Requerido: Sí
- Descripción: 3–10 tags cortos del contenido/título.
- Reglas:
  - No inventar tags que no aparezcan o no estén fuertemente soportados.
  - Deben ser “short keyword tags”.
- Ejemplos correctos:
  - `["distribution", "rollover", "termination", "lt_trust"]`
  - `["hardship", "withdrawal", "eligibility"]`
- Incorrecto:
  - `[]` si sí hay conceptos claros (idealmente siempre 3–10; pero si el artículo fuera extremadamente ambiguo, aún debe existir; el prompt pide 3–10, así que es un requisito semántico, no de schema).
- Edge cases:
  - Si existe “Tags File” con catálogo permitido, usar solo los disponibles (si está adjunto). Si no hay catálogo, usar tags soportados por texto.

#### 3.1.9 `metadata.language`
- Ruta: `root.metadata.language`
- Tipo: `string`
- Requerido: Sí
- Descripción: Idioma del artículo.
- Regla:
  - Por defecto `"en-US"` salvo evidencia.
- Ejemplos correctos:
  - `"en-US"`
  - `"es-ES"` (solo si el artículo está en español; de lo contrario no)
- Incorrecto:
  - `"english"` (formato inválido)
- Edge cases:
  - Artículos mixtos: escoger el dominante.

#### 3.1.10 `metadata.last_updated`
- Ruta: `root.metadata.last_updated`
- Tipo: `string | null`
- Requerido: Sí
- Descripción: Fecha real de actualización del artículo (compat key).
- Reglas:
  - SOLO si el artículo explícitamente da una fecha.
  - Si no hay fecha → `null`.
  - Excepción: si tu sistema downstream requiere no-null, se puede setear = `transformed_at` PERO entonces agregar guardrail (ver details.guardrails.must_not) que advierta que no se use para freshness.
- Formato:
  - `YYYY-MM-DD`
- Ejemplos correctos:
  - `"2025-11-30"`
  - `null`
- Incorrecto:
  - `"11/30/2025"` (formato inválido)
- Edge cases:
  - Diferenciar `last_updated` vs `source_last_updated`: ambos dependen de evidencia.

#### 3.1.11 `metadata.schema_version`
- Ruta: `root.metadata.schema_version`
- Tipo: `string`
- Requerido: Sí
- Valor permitido:
  - Debe ser exactamente `"kb_article_v2"`.
- Ejemplos correctos:
  - `"kb_article_v2"`
  - `"kb_article_v2"` (siempre igual)
- Incorrecto:
  - `"v2"` / `"kb_article_v1"`

#### 3.1.12 `metadata.transformed_at`
- Ruta: `root.metadata.transformed_at`
- Tipo: `string`
- Requerido: Sí
- Descripción: Fecha de transformación (hoy).
- Formato: `YYYY-MM-DD`
- Ejemplos correctos:
  - `"2026-01-20"` (si hoy es 2026-01-20)
  - `"2026-01-20"`
- Incorrecto:
  - `null` (no permitido; siempre debe ser hoy)
- Edge cases:
  - Si el sistema corre en otra TZ, definir “hoy” consistentemente a nivel pipeline.

#### 3.1.13 `metadata.source_last_updated`
- Ruta: `root.metadata.source_last_updated`
- Tipo: `string | null`
- Requerido: Sí
- Descripción: Fecha fuente si el artículo lo menciona explícitamente.
- Formato: `YYYY-MM-DD`
- Ejemplos correctos:
  - `"2025-12-01"`
  - `null`
- Incorrecto:
  - `"Dec 1, 2025"` (no ISO)
- Edge cases:
  - No derivar “por intuición” aunque el artículo parezca reciente.

#### 3.1.14 `metadata.source_system`
- Ruta: `root.metadata.source_system`
- Tipo: `string | null`
- Requerido: Sí
- Descripción: Sistema fuente (Zendesk, Confluence, etc.) solo si se indica.
- Ejemplos correctos:
  - `"Zendesk"`
  - `null`
- Incorrecto:
  - `"Confluence"` si no aparece.
- Edge cases:
  - Si el pipeline sabe el origen pero no está en el artículo, igual debe ser null por regla de “no inventar”.

---

### 3.2 `summary`
- Key: `summary`
- Ruta: `root.summary`
- Tipo: `object`
- Requerido: Sí
- Descripción: Capa compacta, multi-artículo, útil para retrieval rápido.
- Validaciones:
  - Todas las subkeys deben existir.
  - `required_data_summary` si no vacío: cada item empieza con `(source_type)` donde source_type ∈ {participant_profile, plan_profile, message_text, agent_input, unknown}  
    NOTA: El prompt original para summary enumera (participant_profile/message_text/agent_input/unknown). Sin embargo el bloque de required_data enumera también plan_profile. Para consistencia operativa, se recomienda permitir `(plan_profile)` si se usa plan_profile en required_data. Si el pipeline es estricto a los 4 valores, entonces NO usar plan_profile en summary y mapear a `(unknown)` o ajustar reglas. Ver “Limitaciones / Pendientes”.

#### 3.2.1 `summary.purpose`
- Ruta: `root.summary.purpose`
- Tipo: `string`
- Requerido: Sí
- Descripción: 1–2 oraciones que dicen qué ayuda a hacer el artículo.
- Reglas:
  - Máximo 2 frases.
  - Debe ser específica, no genérica.
- Ejemplos correctos:
  - `"Explains how to process a participant distribution request and what data is required to proceed."`
  - `"Guides support agents on eligibility rules, steps, and guardrails for hardship withdrawals under this record keeper."`
- Incorrecto:
  - `"Helps with stuff."` (demasiado vago)
- Edge cases:
  - Si el artículo cubre múltiples procesos, resumir el principal y mencionar alcance.

#### 3.2.2 `summary.topic`
- Ruta: `root.summary.topic`
- Tipo: `string | null`
- Requerido: Sí
- Descripción: Etiqueta consistente (ej. "distribution", "loan") si es clara.
- Reglas:
  - Si no es claro → `null`.
  - No inventar taxonomías.
- Ejemplos correctos:
  - `"distribution"`
  - `null`
- Incorrecto:
  - `"participant_support"` (si no es una etiqueta consistente ni soportada)
- Edge cases:
  - Artículos mixtos: elegir el tópico dominante.

#### 3.2.3 `summary.subtopics`
- Ruta: `root.summary.subtopics`
- Tipo: `string[]`
- Requerido: Sí
- Descripción: Lista de subtemas.
- Reglas:
  - Si no hay → `[]`.
  - No inventar subtopics.
- Ejemplos correctos:
  - `["eligibility", "tax_withholding", "processing_steps"]`
  - `[]`
- Incorrecto:
  - `null` (debe ser array)
- Edge cases:
  - Evitar subtopics que implican hechos no presentes.

#### 3.2.4 `summary.required_data_summary`
- Ruta: `root.summary.required_data_summary`
- Tipo: `string[]`
- Requerido: Sí
- Descripción: Bullets cortos (3–7) que resumen must-have data points.
- Reglas:
  - Cada bullet empieza con `(source_type)`:
    - (participant_profile)
    - (plan_profile) (si se adopta)
    - (message_text)
    - (agent_input)
    - (unknown)
  - Debe corresponder a `details.required_data.must_have`.
  - No usar paths técnicos.
- Ejemplos correctos:
  - `["(message_text) Requested distribution amount.", "(agent_input) Participant delivery preference if required by the article.", "(participant_profile) Participant eligibility status as stated in the article."]`
  - `[]`
- Incorrecto:
  - `["Requested amount"]` (sin prefijo)
- Edge cases:
  - Si no hay required_data en el artículo, mantener `[]`.

#### 3.2.5 `summary.key_business_rules`
- Ruta: `root.summary.key_business_rules`
- Tipo: `string[]`
- Requerido: Sí
- Descripción: 3–8 reglas de negocio de alta prioridad.
- Reglas:
  - Cada regla debe estar soportada por el artículo.
  - Si hay fees, incluir 1–3 bullets de fees.
- Ejemplos correctos:
  - `["Do not guarantee the plan allows this feature; confirm plan rules if the article indicates variability.", "If fees apply, disclose only the amounts stated in the article."]`
  - `[]` (solo si realmente no hay reglas; poco común)
- Incorrecto:
  - Reglas inventadas (ej. “processing takes 5–7 days” si no está).
- Edge cases:
  - Regla de no prometer: útil si el artículo usa “may”.

#### 3.2.6 `summary.key_steps_summary`
- Ruta: `root.summary.key_steps_summary`
- Tipo: `string[]`
- Requerido: Sí
- Descripción: 3–7 pasos de alto nivel del flujo principal.
- Reglas:
  - Sin detalles excesivos; para eso está `details.steps`.
- Ejemplos correctos:
  - `["Collect required participant intent details.", "Verify eligibility constraints stated in the article.", "Provide participant with next steps and required acknowledgments."]`
  - `[]`
- Incorrecto:
  - Incluir “ticket routing” (prohibido).
- Edge cases:
  - Si el artículo no describe pasos, usar [].

#### 3.2.7 `summary.high_impact_faq_pairs`
- Ruta: `root.summary.high_impact_faq_pairs`
- Tipo: `object[]` con objetos `{question, answer}`
- Requerido: Sí
- Reglas:
  - 2–5 pares.
  - Respuestas cortas y soportadas.
- Ejemplos correctos:
  - `[{"question":"How do I know if fees apply?","answer":"Only disclose fees if the article explicitly lists them; otherwise state the article does not specify fees."}]`
  - `[]` (si no hay FAQs claras)
- Incorrecto:
  - Respuestas largas o con promesas no soportadas.
- Edge cases:
  - Evitar FAQs que requieren datos ausentes.

#### 3.2.8 `summary.plan_specific_guardrails`
- Ruta: `root.summary.plan_specific_guardrails`
- Tipo: `string[]`
- Requerido: Sí
- Descripción: Redlines y límites específicos de record keeper/plan.
- Ejemplos correctos:
  - `["Do not claim the participant can complete this in the portal unless the article explicitly states a portal URL and flow."]`
  - `[]`
- Incorrecto:
  - Guardrails genéricos que inventan sistemas (ej. “Use ForUsBots” si el artículo no lo dice; nota: el prompt menciona ForUsBots como ejemplo interno, pero sigue rigiendo “no inventar”).
- Edge cases:
  - Si `metadata.record_keeper` no es null, suele haber guardrails.

#### 3.2.9 `summary.critical_flags`
- Ruta: `root.summary.critical_flags`
- Tipo: `object`
- Requerido: Sí
- Subkeys:
  - `portal_required` (boolean)
  - `mfa_relevant` (boolean)
  - `record_keeper_must_be` (string | null)
- Reglas:
  - `portal_required`: true si el artículo requiere portal/URL/flujo portal.
  - `mfa_relevant`: true solo si el artículo menciona MFA.
  - `record_keeper_must_be`: nombre del RK si aplica; si no, null.
- Ejemplos correctos:
  - `{"portal_required":true,"mfa_relevant":false,"record_keeper_must_be":"LT Trust"}`
  - `{"portal_required":false,"mfa_relevant":false,"record_keeper_must_be":null}`
- Incorrecto:
  - Omitir una subkey.
- Edge cases:
  - Si portal URL no aparece pero se menciona “portal”, decidir con cautela y reflejar incertidumbre en guardrails.

---

### 3.3 `details`
- Key: `details`
- Ruta: `root.details`
- Tipo: `object`
- Requerido: Sí
- Descripción: Capa completa y operable: reglas, pasos, issues, ejemplos, required_data, decisioning y marcos de respuesta.
- Validaciones globales:
  - Todas las subkeys deben existir.
  - `details.fees` siempre existe (vacío si no hay fees).
  - `decision_guide.supported_outcomes` debe ser EXACTAMENTE:
    - `"can_proceed"`, `"blocked_missing_data"`, `"blocked_not_eligible"`, `"ambiguous_plan_rules"`
  - `response_frames` debe tener los 4 outcomes con todas sus subkeys arrays.

#### 3.3.1 `details.business_rules`
- Ruta: `root.details.business_rules`
- Tipo: `object[]` con `{category, rules[]}`
- Requerido: Sí
- Descripción: Reglas por categoría (eligibility, docs, timing, etc.)
- Reglas:
  - `rules` es `string[]` (NO tablas ASCII, NO objetos).
  - Solo reglas soportadas por el artículo.
- Ejemplos correctos:
  - `[{"category":"eligibility","rules":["Eligibility is determined by plan rules as stated in the article."]}]`
  - `[]`
- Incorrecto:
  - Guardar fees como tabla en `rules`.
- Edge cases:
  - Si hay plan variability, incluir reglas que indiquen “may/depends” sin inventar.

#### 3.3.2 `details.fees`
- Ruta: `root.details.fees`
- Tipo: `object[]` con `{service, fee, notes}`
- Requerido: Sí (siempre existe)
- Descripción: Tabla de fees estructurada.
- Reglas:
  - Si el artículo menciona fees con montos claros → poblar.
  - Si menciona fees sin montos claros → disparar Missing/Contradictory (no generar JSON).
  - Si no menciona fees → `[]`.
  - `notes` null salvo aclaración explícita.
- Ejemplos correctos:
  - `[{"service":"Distribution processing","fee":"$75","notes":null}]`
  - `[]`
- Incorrecto:
  - `[{"service":"Distribution","fee":75,"notes":null}]` (fee debe ser string, no number)
- Edge cases:
  - Fees con signos o condiciones: conservar string exacto (ej. "+$35", "No charge").

#### 3.3.3 `details.steps`
- Ruta: `root.details.steps`
- Tipo: `object[]` con `{step_number, type, visibility, description, notes}`
- Requerido: Sí
- Descripción: Flujo secuencial detallado.
- Reglas/validaciones:
  - `step_number` entero empezando en 1, incremental.
  - `type` ejemplos: "participant-facing", "internal-check".
  - `visibility` valores esperados:
    - `"both"` (compartible)
    - `"agent-only"` (solo interno)
  - `description` en inglés (según prompt) o consistente con article language; sin Markdown.
  - `notes` string o null (según el prompt, "notes" es “optional”, pero en schema aparece como string; si no hay notas, usar `null` o string vacío según convención. El ejemplo lo define como string, pero dice “optional”. Recomendación: usar `null` si no hay notas.
- Ejemplos correctos:
  - `{"step_number":1,"type":"participant-facing","visibility":"both","description":"Collect the required request details stated in the article.","notes":"Do not assume details not provided by the participant."}`
  - `{"step_number":2,"type":"internal-check","visibility":"agent-only","description":"Verify any eligibility constraints explicitly stated in the article.","notes":null}`
- Incorrecto:
  - `{"step_number":"1", ...}` (step_number debe ser integer)
- Edge cases:
  - Si el artículo no provee pasos claros, usar [] o pasos muy generales (sin inventar).

#### 3.3.4 `details.common_issues`
- Ruta: `root.details.common_issues`
- Tipo: `object[]` con `{issue, resolution}`
- Requerido: Sí
- Reglas:
  - Deben ser issues soportados por el artículo.
- Ejemplos correctos:
  - `[{"issue":"Participant did not specify required request details.","resolution":"Ask the participant for the missing required data points as listed in required_data.if_missing."}]`
  - `[]`
- Incorrecto:
  - Resolver con “escalate ticket” (ticket action prohibida).
- Edge cases:
  - Si no hay issues, [].

#### 3.3.5 `details.examples`
- Ruta: `root.details.examples`
- Tipo: `object[]` con `{scenario, outcome}`
- Requerido: Sí
- Reglas:
  - Escenarios realistas; outcome debe ser permitido por decision_guide.
- Ejemplos correctos:
  - `[{"scenario":"Participant asks if the plan allows a feature but the article says it may depend on plan.","outcome":"State that the article indicates plan variability and you cannot guarantee availability; request plan-specific confirmation if needed."}]`
  - `[]`
- Incorrecto:
  - Outcome promete algo no soportado (“Yes, it is allowed”).
- Edge cases:
  - Evitar detalles no mencionados.

#### 3.3.6 `details.additional_notes`
- Ruta: `root.details.additional_notes`
- Tipo: `object[]` con `{category, notes[]}`
- Requerido: Sí
- Categorías típicas:
  - `"processing_times"`, `"taxes"`, `"documentation"`, etc. SOLO si aparecen en el artículo.
- Ejemplos correctos:
  - `[{"category":"processing_times","notes":["The article states processing time is X days."]}]`
  - `[]`
- Incorrecto:
  - Inventar tiempos de procesamiento.
- Edge cases:
  - Si el artículo menciona “timelines vary”, anotarlo como tal (sin números).

#### 3.3.7 `details.faq_pairs`
- Ruta: `root.details.faq_pairs`
- Tipo: `object[]` con `{question, answer}`
- Requerido: Sí
- Reglas:
  - Respuestas detalladas, consistentes con artículo.
- Ejemplos correctos:
  - `[{"question":"What information is required to proceed?","answer":"Only the data points explicitly required by the article should be requested or retrieved."}]`
  - `[]`
- Incorrecto:
  - Respuesta incluye Markdown o links con formato markdown dentro del string.
- Edge cases:
  - Si hay high_impact_faq_pairs, puede haber overlap con faq_pairs; mantener consistencia.

#### 3.3.8 `details.definitions`
- Ruta: `root.details.definitions`
- Tipo: `object[]` con `{term, definition}`
- Requerido: Sí
- Reglas:
  - Definiciones en “plain English”.
  - Solo términos mencionados.
- Ejemplos correctos:
  - `[{"term":"Record keeper","definition":"The organization responsible for maintaining plan records and processing transactions, as referenced in the article."}]`
  - `[]`
- Incorrecto:
  - Definir términos no mencionados.
- Edge cases:
  - Si el artículo está muy técnico, extraer términos clave.

#### 3.3.9 `details.guardrails`
- Ruta: `root.details.guardrails`
- Tipo: `object` con `{must_not[], must_do_if_unsure[]}`
- Requerido: Sí
- Descripción: Prohibiciones y acciones en incertidumbre (anti-alucinación).
- Reglas:
  - `must_not`: prohibiciones claras. Ej.: no prometer, no inventar, no afirmar acceso a portal si no está.
  - `must_do_if_unsure`: instrucciones para manejar incertidumbre.
- Ejemplos correctos:
  - must_not:
    - `"Do not provide processing timelines not stated in the article."`
    - `"Do not claim where or how to access a referenced document unless explicitly stated."`
  - must_do_if_unsure:
    - `"State what the article does and does not specify, and request the missing details from the participant or internal systems as appropriate."`
- Incorrecto:
  - Incluir “close ticket” o “route to team”.
- Edge cases:
  - Si `last_updated` se setea = transformed_at por requerimiento downstream, debe incluir guardrail:  
    `"Do not use last_updated as the source of truth for freshness; use transformed_at and source_last_updated."`

#### 3.3.10 `details.references`
- Ruta: `root.details.references`
- Tipo: `object`
- Requerido: Sí
- Subkeys:
  - `participant_portal`: `string | null` (URL en texto plano)
  - `internal_articles`: `string[]`
  - `external_links`: `string[]` (URLs en texto plano)
  - `contact`: `object` con `email`, `phone`, `support_hours` (cada uno string|null)
- Reglas:
  - No inventar emails/phones/URLs.
  - Normalizar links: nada de `[text](url)` ni `mailto:`.
  - Teléfono se copia exactamente como el artículo.
- Ejemplos correctos:
  - `participant_portal`: `"https://example.com/portal"`
  - `contact.email`: `"help@forusall.com"`
  - `contact.phone`: `"844-401-2253"`
- Incorrecto:
  - `participant_portal`: `"[Portal](https://example.com/portal)"`
- Edge cases:
  - Si el artículo dice “contact support” sin detalles, `contact.*` = null.

#### 3.3.11 `details.required_data`
- Ruta: `root.details.required_data`
- Tipo: `object`
- Requerido: Sí
- Subkeys:
  - `must_have`: `object[]`
  - `nice_to_have`: `object[]`
  - `if_missing`: `object[]`
  - `disambiguation_notes`: `string[]`
- Estructura de cada item en must_have/nice_to_have:
  - `data_point` (string)
  - `meaning` (string)
  - `example_values` (string[])
  - `why_needed` (string)
  - `source_note` (string|null)
  - `source_type` (enum string): `"participant_profile" | "plan_profile" | "message_text" | "agent_input" | "unknown"`
- Reglas “source_type” (NORMATIVAS):
  - participant_profile: atributo/estado del participante recuperable internamente y requerido por el artículo.
  - plan_profile: atributo/estado del plan recuperable internamente y requerido por el artículo.
  - message_text: intención/datos declarados por participante en su mensaje.
  - agent_input: dato que el agente debe preguntar para continuar.
  - unknown: requerido pero no se puede inferir origen sin inventar.
- Anti-field creep:
  - No agregar data_points “porque serían útiles”; solo si el artículo lo requiere.
- example_values:
  - Si el artículo trae ejemplos, usarlos.
  - Si no, solo placeholders de formato que NO agreguen hechos (ej. `"A value stated in this article"`), o `[]`.

Ejemplos correctos (item):
  - {
      "data_point":"Requested amount",
      "meaning":"The amount the participant is requesting, as required by the article.",
      "example_values":["A value stated in this article"],
      "why_needed":"Used to determine what request to process and whether additional steps apply.",
      "source_note":null,
      "source_type":"message_text"
    }

Ejemplos incorrectos (item):
  - {
      "data_point":"Termination date",
      "meaning":"Date the participant terminated employment.",
      "example_values":["2025-01-01"],
      "why_needed":"Eligibility check.",
      "source_note":null,
      "source_type":"participant_profile"
    }
  Motivo: field creep; si el artículo no menciona termination date, no se puede incluir.

##### 3.3.11.1 `details.required_data.if_missing`
- Tipo: `object[]` con:
  - `missing_data_point` (string)
  - `ask_participant` (string|null)
  - `agent_note` (string)
- Reglas:
  - Si missing_data_point corresponde a item con source_type = participant_profile o plan_profile:
    - ask_participant = null
    - agent_note debe indicar recuperar de sistemas internos y no preguntar al participante.
  - Si source_type = message_text o agent_input:
    - ask_participant debe ser una pregunta directa al participante.
  - Si source_type = unknown:
    - ask_participant puede ser pregunta cautelosa o null; documentar incertidumbre en agent_note.

Ejemplos correctos:
  - {
      "missing_data_point":"Requested amount",
      "ask_participant":"What amount would you like to request?",
      "agent_note":"Do not proceed or confirm outcomes until the requested amount is provided."
    }
  - {
      "missing_data_point":"Participant eligibility status",
      "ask_participant":null,
      "agent_note":"Retrieve from admin/portal systems (ForUsBots); do not ask participant."
    }

Ejemplo incorrecto:
  - ask_participant con participant_profile:
    - {
        "missing_data_point":"Participant eligibility status",
        "ask_participant":"Are you eligible?",
        "agent_note":"..."
      }
  Motivo: viola regla (no preguntar al participante si es participant_profile).

##### 3.3.11.2 `details.required_data.disambiguation_notes`
- Tipo: `string[]`
- Reglas:
  - Bullets cortos para aclarar ambigüedades del artículo (solo si aparecen).
  - No inventar.
- Ejemplos correctos:
  - `["The article uses 'may' to indicate plan variability; do not guarantee availability."]`
  - `[]`
- Incorrecto:
  - Notas que agregan criterios nuevos.

#### 3.3.12 `details.decision_guide`
- Ruta: `root.details.decision_guide`
- Tipo: `object`
- Requerido: Sí
- Objetivo: Controlar conclusiones permitidas y outcomes, evitando improvisación.
- Subkeys:
  - `supported_outcomes`: fixed array EXACTA:
    - ["can_proceed","blocked_missing_data","blocked_not_eligible","ambiguous_plan_rules"]
  - `eligibility_requirements`: `string[]`
  - `blocking_conditions`: `string[]`
  - `missing_data_conditions`: `object[]`
    - { condition, missing_data_point, resulting_outcome, ask_participant }
  - `allowed_conclusions`: `string[]`
  - `not_allowed_conclusions`: `string[]`
- Reglas:
  - No inventar requisitos ni condiciones.
  - missing_data_conditions.resulting_outcome siempre `"blocked_missing_data"`.
  - ask_participant debe seguir lógica source_type (como required_data.if_missing).

Ejemplos correctos:
  - supported_outcomes:
    - ["can_proceed","blocked_missing_data","blocked_not_eligible","ambiguous_plan_rules"]
  - missing_data_conditions item:
    - {
        "condition":"The article requires the requested amount, but it was not provided.",
        "missing_data_point":"Requested amount",
        "resulting_outcome":"blocked_missing_data",
        "ask_participant":"What amount would you like to request?"
      }

Ejemplo incorrecto:
  - supported_outcomes alterado:
    - ["can_proceed","blocked_missing_data"]
  Motivo: debe incluir los 4.

Edge case: plan variability
- Si el artículo dice “may allow”:
  - not_allowed_conclusions debe incluir:
    - "Do not guarantee the plan allows <feature/process>."

#### 3.3.13 `details.response_frames`
- Ruta: `root.details.response_frames`
- Tipo: `object` con 4 objetos:
  - `can_proceed`, `blocked_missing_data`, `blocked_not_eligible`, `ambiguous_plan_rules`
- Cada outcome contiene:
  - `participant_message_components`: string[]
  - `next_steps`: string[]
  - `warnings`: string[]
  - `questions_to_ask`: string[]
  - `what_not_to_say`: string[]
- Reglas:
  - Todo debe estar soportado por artículo.
  - warnings solo si el artículo menciona taxes/penalties/processing warnings.
  - questions_to_ask solo para outcomes missing/ambiguous; en can_proceed normalmente [].
  - what_not_to_say: prohibiciones anti-promesas y anti-hallucination.

Ejemplos correctos (estructura mínima de un outcome):
  - "blocked_missing_data": {
      "participant_message_components":["I can help with this, but I need a bit more information first."],
      "next_steps":["Provide the missing required information listed below."],
      "warnings":["Do not assume plan rules not stated in the article."],
      "questions_to_ask":["What amount would you like to request?"],
      "what_not_to_say":["Do not confirm eligibility or processing timelines not stated in the article."]
    }

Ejemplo incorrecto:
  - Omitir un campo (p.ej. no incluir warnings) → invalida schema.

---

## 4. Ejemplos completos de Payload

> IMPORTANTE: Estos ejemplos son “plantillas” del esquema y muestran valores genéricos. En una transformación real, los strings deben venir del Article Name/Content. No inventar montos, links o reglas.

### 4.1 Ejemplo “mínimo válido”
{
  "metadata": {
    "article_id": "example_article",
    "title": "Example Article Title",
    "description": "Guide a former employee through requesting a termination distribution from a ForUsAll-administered 401(k), including cash withdrawal, rollover, or a split transaction, using the participant portal or a RightSignature electronic form when portal access is not possible. This article is specific to the recordkeeper LT Trust.",
    "audience": "Internal AI Support Agent",
    "record_keeper": null,
    "plan_type": null,
    "scope": "global",
    "tags": ["example", "kb", "support"],
    "language": "en-US",
    "last_updated": null,
    "schema_version": "kb_article_v2",
    "transformed_at": "2026-01-20",
    "source_last_updated": null,
    "source_system": null
  },
  "summary": {
    "purpose": "Describes an example KB topic and how an agent should respond using only supported content.",
    "topic": null,
    "subtopics": [],
    "required_data_summary": [],
    "key_business_rules": [],
    "key_steps_summary": [],
    "high_impact_faq_pairs": [],
    "plan_specific_guardrails": [],
    "critical_flags": {
      "portal_required": false,
      "mfa_relevant": false,
      "record_keeper_must_be": null
    }
  },
  "details": {
    "business_rules": [],
    "fees": [],
    "steps": [],
    "common_issues": [],
    "examples": [],
    "additional_notes": [],
    "faq_pairs": [],
    "definitions": [],
    "guardrails": {
      "must_not": [],
      "must_do_if_unsure": []
    },
    "references": {
      "participant_portal": null,
      "internal_articles": [],
      "external_links": [],
      "contact": {
        "email": null,
        "phone": null,
        "support_hours": null
      }
    },
    "required_data": {
      "must_have": [],
      "nice_to_have": [],
      "if_missing": [],
      "disambiguation_notes": []
    },
    "decision_guide": {
      "supported_outcomes": [
        "can_proceed",
        "blocked_missing_data",
        "blocked_not_eligible",
        "ambiguous_plan_rules"
      ],
      "eligibility_requirements": [],
      "blocking_conditions": [],
      "missing_data_conditions": [],
      "allowed_conclusions": [],
      "not_allowed_conclusions": []
    },
    "response_frames": {
      "can_proceed": {
        "participant_message_components": [],
        "next_steps": [],
        "warnings": [],
        "questions_to_ask": [],
        "what_not_to_say": []
      },
      "blocked_missing_data": {
        "participant_message_components": [],
        "next_steps": [],
        "warnings": [],
        "questions_to_ask": [],
        "what_not_to_say": []
      },
      "blocked_not_eligible": {
        "participant_message_components": [],
        "next_steps": [],
        "warnings": [],
        "questions_to_ask": [],
        "what_not_to_say": []
      },
      "ambiguous_plan_rules": {
        "participant_message_components": [],
        "next_steps": [],
        "warnings": [],
        "questions_to_ask": [],
        "what_not_to_say": []
      }
    }
  }
}

### 4.2 Ejemplo “completo recomendado”
{
  "metadata": {
    "article_id": "distribution_termination_withdrawal_or_rollover",
    "title": "Distribution Termination: Withdrawal or Rollover",
    "description": "Guide a former employee through requesting a termination distribution from a ForUsAll-administered 401(k), including cash withdrawal, rollover, or a split transaction, using the participant portal or a RightSignature electronic form when portal access is not possible. This article is specific to the recordkeeper LT Trust.",
    "audience": "Internal AI Support Agent",
    "record_keeper": null,
    "plan_type": "401(k)",
    "scope": "global",
    "tags": ["distribution", "termination", "withdrawal", "rollover", "taxes"],
    "language": "en-US",
    "last_updated": null,
    "schema_version": "kb_article_v2",
    "transformed_at": "2026-01-20",
    "source_last_updated": null,
    "source_system": null
  },
  "summary": {
    "purpose": "Helps an internal support agent determine what information is needed to handle a termination distribution request and provide the correct next steps without making unsupported promises.",
    "topic": "distribution",
    "subtopics": ["withdrawal", "rollover", "eligibility", "required_information"],
    "required_data_summary": [
      "(message_text) Participant's requested distribution option if the article requires selecting one.",
      "(message_text) Requested amount if the article requires an amount.",
      "(agent_input) Any additional confirmations the article requires the agent to ask for.",
      "(unknown) Any required document acknowledgments if the article requires them but does not specify the source."
    ],
    "key_business_rules": [
      "Do not guarantee the plan allows a specific distribution option if the article indicates plan variability.",
      "Do not state fees unless the article explicitly lists the amounts and fee lines.",
      "Do not provide timelines unless the article explicitly states them."
    ],
    "key_steps_summary": [
      "Collect required participant request details explicitly stated in the article.",
      "Confirm any eligibility requirements explicitly described.",
      "Provide participant-facing next steps and required acknowledgments without adding new information."
    ],
    "high_impact_faq_pairs": [
      {
        "question": "Can I guarantee this distribution option is available?",
        "answer": "No. Only confirm availability if the article explicitly states it without plan-variation language."
      },
      {
        "question": "What if the participant did not provide required details?",
        "answer": "Ask only for the missing required data points the article explicitly requires."
      }
    ],
    "plan_specific_guardrails": [
      "Do not claim portal steps or URLs unless explicitly provided by the article."
    ],
    "critical_flags": {
      "portal_required": false,
      "mfa_relevant": false,
      "record_keeper_must_be": null
    }
  },
  "details": {
    "business_rules": [
      {
        "category": "anti_hallucination",
        "rules": [
          "Only use facts stated in Article Name and Article Content; do not invent fees, timelines, or eligibility constraints.",
          "If the article indicates plan variability, do not guarantee availability."
        ]
      }
    ],
    "fees": [],
    "steps": [
      {
        "step_number": 1,
        "type": "participant-facing",
        "visibility": "both",
        "description": "Collect the participant's request details that the article explicitly requires to proceed.",
        "notes": "Do not add additional requirements not mentioned by the article."
      },
      {
        "step_number": 2,
        "type": "internal-check",
        "visibility": "agent-only",
        "description": "Verify eligibility requirements only if they are explicitly described in the article.",
        "notes": null
      },
      {
        "step_number": 3,
        "type": "participant-facing",
        "visibility": "both",
        "description": "Provide next steps and any disclosures explicitly stated in the article, without implying extra guarantees.",
        "notes": "If the article is silent on timelines, do not estimate."
      }
    ],
    "common_issues": [
      {
        "issue": "Missing required participant details.",
        "resolution": "Ask the participant the specific question in required_data.if_missing for each missing item, and do not proceed until answered."
      }
    ],
    "examples": [
      {
        "scenario": "Participant asks whether the plan allows an option, but the article uses 'may' or 'depends on your plan'.",
        "outcome": "Explain that availability depends on plan rules and you cannot guarantee it based on the article alone."
      }
    ],
    "additional_notes": [],
    "faq_pairs": [
      {
        "question": "What if the article does not mention fees?",
        "answer": "Set details.fees to an empty array and do not discuss fees unless the article explicitly lists them."
      }
    ],
    "definitions": [
      {
        "term": "Plan variability",
        "definition": "Language in the article indicating that rules may differ by plan, such as 'may allow' or 'depends on your plan'."
      }
    ],
    "guardrails": {
      "must_not": [
        "Do not include ticket actions such as closing, tagging, routing, or escalation steps.",
        "Do not provide processing timelines not stated in the article.",
        "Do not guarantee eligibility or plan features if the article indicates variability.",
        "Do not claim where or how to access a referenced document unless explicitly stated."
      ],
      "must_do_if_unsure": [
        "State what the article explicitly says and what it does not specify, and ask only for missing required data points or retrieve internal-only data from admin systems if applicable."
      ]
    },
    "references": {
      "participant_portal": null,
      "internal_articles": [],
      "external_links": [],
      "contact": {
        "email": null,
        "phone": null,
        "support_hours": null
      }
    },
    "required_data": {
      "must_have": [
        {
          "data_point": "Required request details",
          "meaning": "The minimum set of participant-provided details that the article explicitly requires to proceed.",
          "example_values": ["A value stated in this article"],
          "why_needed": "Without these details, the agent cannot proceed per the article.",
          "source_note": null,
          "source_type": "unknown"
        }
      ],
      "nice_to_have": [],
      "if_missing": [
        {
          "missing_data_point": "Required request details",
          "ask_participant": "Could you share the required details for your request as described in the article?",
          "agent_note": "Do not proceed until the required details explicitly stated by the article are provided."
        }
      ],
      "disambiguation_notes": [
        "If the article uses plan-variation language, treat feature availability as ambiguous unless explicitly confirmed."
      ]
    },
    "decision_guide": {
      "supported_outcomes": [
        "can_proceed",
        "blocked_missing_data",
        "blocked_not_eligible",
        "ambiguous_plan_rules"
      ],
      "eligibility_requirements": [],
      "blocking_conditions": [],
      "missing_data_conditions": [
        {
          "condition": "The article requires specific request details to proceed, but they were not provided.",
          "missing_data_point": "Required request details",
          "resulting_outcome": "blocked_missing_data",
          "ask_participant": "Could you share the required details for your request as described in the article?"
        }
      ],
      "allowed_conclusions": [
        "I can help, but I need the required details explicitly stated in the article to proceed.",
        "The article indicates plan rules may vary; I cannot guarantee availability without confirmation."
      ],
      "not_allowed_conclusions": [
        "Do not guarantee the plan allows a feature/process if the article indicates variability.",
        "Do not state fees, timelines, or eligibility constraints that are not explicitly stated in the article."
      ]
    },
    "response_frames": {
      "can_proceed": {
        "participant_message_components": [
          "Thanks for the information. Based on what the article specifies, we can proceed with the next steps."
        ],
        "next_steps": [
          "Follow the steps explicitly outlined in the article."
        ],
        "warnings": [],
        "questions_to_ask": [],
        "what_not_to_say": [
          "Do not promise processing times not stated in the article.",
          "Do not claim fees apply unless explicitly listed."
        ]
      },
      "blocked_missing_data": {
        "participant_message_components": [
          "I can help with this, but I need a bit more information first."
        ],
        "next_steps": [
          "Provide the missing required information."
        ],
        "warnings": [],
        "questions_to_ask": [
          "Could you share the required details for your request as described in the article?"
        ],
        "what_not_to_say": [
          "Do not confirm outcomes until missing information is provided."
        ]
      },
      "blocked_not_eligible": {
        "participant_message_components": [
          "Based on the eligibility rules explicitly stated in the article, this request cannot proceed."
        ],
        "next_steps": [
          "Share the eligibility constraint stated in the article and any allowed alternatives if explicitly provided."
        ],
        "warnings": [],
        "questions_to_ask": [],
        "what_not_to_say": [
          "Do not cite additional eligibility criteria not stated in the article."
        ]
      },
      "ambiguous_plan_rules": {
        "participant_message_components": [
          "The article indicates that plan rules may vary, so I cannot confirm this without additional plan-specific information."
        ],
        "next_steps": [
          "Request plan-specific confirmation only if the article instructs the agent to do so, or state that the article does not specify."
        ],
        "warnings": [],
        "questions_to_ask": [
          "Can you confirm any plan-specific details the article requires to determine availability?"
        ],
        "what_not_to_say": [
          "Do not guarantee availability when the article uses plan-variation language."
        ]
      }
    }
  }
}

### 4.3 Ejemplo “con errores” (y lista de errores)
Payload con errores:
{
  "metadata": {
    "article_id": "Example Article",
    "title": "Example Article Title",
    "description" "Example article Description",
    "audience": null,
    "record_keeper": "Fidelity",
    "plan_type": "403(b)",
    "scope": "rk-specific",
    "tags": ["one_tag"],
    "language": "english",
    "last_updated": "11/30/2025",
    "schema_version": "kb_article_v3",
    "transformed_at": null,
    "source_last_updated": "Dec 1, 2025",
    "source_system": "Confluence"
  },
  "summary": {
    "purpose": "This is fine.",
    "topic": "distribution",
    "subtopics": null,
    "required_data_summary": ["Requested amount"],
    "key_business_rules": [],
    "key_steps_summary": [],
    "high_impact_faq_pairs": [],
    "plan_specific_guardrails": [],
    "critical_flags": {
      "portal_required": "false",
      "mfa_relevant": false,
      "record_keeper_must_be": null
    }
  },
  "details": {
    "business_rules": [],
    "steps": [],
    "common_issues": [],
    "examples": [],
    "additional_notes": [],
    "faq_pairs": [],
    "definitions": [],
    "guardrails": {
      "must_not": [],
      "must_do_if_unsure": []
    },
    "references": {
      "participant_portal": "[Portal](https://example.com)",
      "internal_articles": [],
      "external_links": [],
      "contact": {
        "email": "[help@forusall.com](mailto:help@forusall.com)",
        "phone": "(844) 401-2253",
        "support_hours": null
      }
    },
    "required_data": {
      "must_have": [],
      "nice_to_have": [],
      "if_missing": [],
      "disambiguation_notes": []
    },
    "decision_guide": {
      "supported_outcomes": ["can_proceed"],
      "eligibility_requirements": [],
      "blocking_conditions": [],
      "missing_data_conditions": [],
      "allowed_conclusions": [],
      "not_allowed_conclusions": []
    },
    "response_frames": {}
  }
}

Lista de errores:
1) metadata.article_id inválido: contiene espacios y mayúsculas; debe ser lower_snake_case.
2) metadata.audience no debe ser null (debe ser string normalmente).
3) metadata.record_keeper y plan_type inventados si no están en el artículo (violación “no inventar”).
4) metadata.scope inválido: "rk-specific" no es uno de los valores permitidos.
5) metadata.tags inválido semánticamente: requiere 3–10 tags; aquí solo 1.
6) metadata.language inválido: debe ser "en-US" o formato similar, no "english".
7) metadata.last_updated formato inválido: debe ser YYYY-MM-DD.
8) metadata.schema_version inválido: debe ser "kb_article_v2".
9) metadata.transformed_at no puede ser null: debe ser hoy YYYY-MM-DD.
10) source_last_updated formato inválido: debe ser YYYY-MM-DD.
11) summary.subtopics no puede ser null: debe ser array.
12) summary.required_data_summary items deben tener prefijo (source_type); aquí no.
13) critical_flags.portal_required debe ser boolean, no string.
14) details.fees falta: siempre debe existir (aunque sea []).
15) references.participant_portal contiene Markdown link; debe ser URL en texto plano.
16) references.contact.email contiene Markdown mailto; debe ser email plano.
17) references.contact.phone reformateado; debe copiarse exactamente como en el artículo.
18) decision_guide.supported_outcomes debe contener exactamente los 4 outcomes.
19) response_frames incompleto: debe tener los 4 outcomes y todas sus subkeys.

---

## 5. Preguntas frecuentes (FAQ)

1) ¿Puedo agregar una key extra top-level como "raw_text" para guardar el artículo original?
- No. El objeto final debe tener exactamente 3 keys top-level: metadata, summary, details.

2) ¿Qué hago si el artículo menciona fees pero no da montos?
- Debes producir SOLO "Missing or Contradictory Information" y preguntar por los montos exactos. No generes JSON.

3) ¿Qué hago si el artículo menciona un PDF pero no dice dónde se obtiene?
- Solo es “missing” si se cumple A/B/C:
  A) El proceso requiere acceder al PDF para completar pasos y no hay instrucciones/URL.
  B) El PDF contiene info crítica no incluida en el artículo.
  C) El PDF es obligatorio y no se explica acceso.
  Si es benigno (“support confirmará”), entonces NO dispares missing: modela como nice_to_have + guardrail.

4) ¿Puedo poner tablas ASCII dentro de strings, por ejemplo para fees?
- No. Fees deben ir en details.fees como objetos. No uses ASCII tables.

5) ¿Qué diferencia hay entre summary.faq_pairs y details.faq_pairs?
- summary.high_impact_faq_pairs es breve (2–5) para retrieval rápido. details.faq_pairs puede ser más completo/detallado.

6) ¿Qué significa “no Markdown dentro de strings”?
- Dentro de JSON strings no deben aparecer `**bold**`, `[text](url)`, `mailto:`, ni formatos Markdown. Emails y URLs deben estar como texto plano.

7) ¿Cuándo uso source_type = participant_profile vs message_text?
- participant_profile: datos internos del estado/atributo del participante requeridos por el artículo.
- message_text: intención o dato que normalmente viene del mensaje del participante.
- No mezclar: nunca marcar intención como participant_profile.

8) ¿Qué hago si no sé de dónde viene un dato requerido?
- Usa source_type = unknown y documenta la incertidumbre. En if_missing, ask_participant puede ser pregunta cautelosa o null; explica en agent_note.

9) ¿Puedo inferir el record_keeper desde el título?
- Sí, solo si el prefijo es claro y explícito (ej. "LT Trust: ..."). Si no, record_keeper = null.

10) ¿Qué hago si el artículo no tiene fechas de actualización?
- metadata.last_updated y metadata.source_last_updated deben ser null.

11) ¿Qué hago si downstream exige last_updated no-null?
- Puedes setear last_updated = transformed_at, pero DEBES agregar un guardrail explícito que indique no usar last_updated como fuente de freshness.

12) ¿Cómo evito “field creep” en required_data?
- Solo incluye data_points que el artículo requiere explícitamente o implica fuertemente como necesarios para completar el proceso descrito.

---

## 6. Checklist de Validación

Checklist técnico:
- Top-level keys EXACTAS: metadata, summary, details.
- No hay keys extra a nivel root.
- Todas las keys del schema existen:
  - Escalares sin soporte → null
  - Arrays sin soporte → []
- JSON válido:
  - Solo comillas dobles para strings
  - Sin trailing commas
  - Objetos/arrays cerrados correctamente
- Strings sin Markdown:
  - No "[text](url)"
  - No "mailto:"
  - No "**" o "_"
  - No curly quotes
- Emails y URLs en texto plano.
- Teléfonos copiados exactamente del artículo.
- details.fees siempre presente:
  - Con objetos si fees claros
  - [] si no hay fees
- Si fees mencionadas sin montos claros → NO JSON; producir Missing/Contradictory.
- decision_guide.supported_outcomes es EXACTAMENTE:
  - can_proceed
  - blocked_missing_data
  - blocked_not_eligible
  - ambiguous_plan_rules
- response_frames tiene los 4 outcomes y cada uno contiene:
  - participant_message_components[]
  - next_steps[]
  - warnings[]
  - questions_to_ask[]
  - what_not_to_say[]
- No ticket actions en ningún lugar.
- required_data:
  - Items must_have/nice_to_have con source_type válido
  - No paths técnicos
  - No data_points inventados
- if_missing:
  - ask_participant = null si source_type es participant_profile o plan_profile
  - agent_note contiene instrucción de recuperar de sistemas internos cuando aplique
- summary.required_data_summary:
  - Si no vacío: cada bullet empieza con (source_type) permitido

Checklist semántico:
- Ninguna regla, fee, timeline, elegibilidad o contacto fue inventado.
- Si el artículo usa "may/depends", se refleja en guardrails, decision_guide y not_allowed_conclusions.

---

## 7. Errores comunes

1) Omitir keys del schema cuando no hay información.
- Solución: siempre incluir keys; usar null o [].

2) Inventar fees o decir “no hay fees” sin evidencia.
- Solución: si el artículo no menciona fees, details.fees = [] y no especular.

3) Incluir timelines estimados.
- Solución: solo usar timelines si el artículo los indica; si no, omitir y agregar guardrail.

4) Incluir Markdown en strings (links, bold, mailto).
- Solución: normalizar a texto plano: email y URL sin brackets/paréntesis.

5) Usar teléfono reformateado.
- Solución: copiar exactamente el formato del artículo (mismos guiones/dígitos).

6) Usar source_type incorrecto (intención como participant_profile).
- Solución: intención y elecciones del participante → message_text o agent_input.

7) “Field creep” en required_data (agregar datos útiles pero no requeridos).
- Solución: incluir solo data_points explícitamente requeridos por el artículo.

8) Mezclar información multi-plan sin reflejar plan variability.
- Solución: si el artículo indica variabilidad, agregar not_allowed_conclusions y guardrails que prohíben garantizar.

9) Poner fees como texto en business_rules o steps.
- Solución: fees solo en details.fees como objetos.

10) Falta de deterministicidad en decision_guide/response_frames.
- Solución: controlar allowed_conclusions/not_allowed_conclusions y componentes por outcome.

---

## Limitaciones / Pendientes

1) Inconsistencia potencial en `summary.required_data_summary` vs allowed source_type:
- El texto de summary.required_data_summary exige prefijos con (participant_profile/message_text/agent_input/unknown), pero required_data.source_type permite también plan_profile.  
  Recomendación: decidir una política:
  - Opción A: Permitir (plan_profile) también en summary y ajustar validación downstream.
  - Opción B: Mantener summary en los 4 valores; si un dato es plan_profile, reflejarlo como (unknown) en summary para no romper validación estricta, manteniendo plan_profile en details.required_data.

2) `details.steps[].notes` tipo y nulabilidad:
- El ejemplo lo define como string, pero “optional”. Esta guía recomienda `null` cuando no haya notas, pero el pipeline debe aceptar null. Si el pipeline requiere string, usar "" (string vacío) consistentemente.

3) Esta guía documenta el esquema “target” y reglas de transformación, pero no sustituye un validador de JSON (schema formal). Si se requiere, definir un JSON Schema externo para validación automática.
