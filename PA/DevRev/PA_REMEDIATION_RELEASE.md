# Paquete de consumidor PA — publicación por componentes

Implementación, main y producción autorizados. DevRev workflow70 v28 ya incorpora la correlación del transporte; el consumidor final se verifica por separado antes de publicarlo. El manifiesto `../n8n/release-manifest.manifest` vincula siete reemplazos de nodos, dos cambios de parámetros, cero addenda, dos reemplazos completos de prompt (parser final y AGENT-3 candidato v11) y un terminal interno nuevo. Su estado es `local_candidate_not_published`: es un candidato local, sin publicar y sin autorización de publicación. Las versiones iniciales permanecen como baseline; el registro local de Fase 3 contiene el estado efectivo y la evidencia de cada publicación. `node PA/n8n/verify-package.js` comprueba integridad y sintaxis sin ejecutar flujos. No se enviaron respuestas a participantes ni se actualizaron reviews.

## Camino realmente observado

DevRev **AGENT-3, Participant Advisory AI Agent, 8.0 – Live** tiene un prompt distinto de SYSTEM_PROMPT y de las copias V2/V3 del repositorio. El enfoque de anteponer PA_REMEDIATION_ADDENDUM.md quedó superado: las previsualizaciones guardadas mostraron que las reglas heredadas seguían anulando el contrato antepuesto. El manifiesto propone ahora un único prompt completo de reemplazo, `candidates/pa-agent-system-v11.md`, sin addendum. Deploy muestra canales Inactive; no se probó su invocación directa ni se activaron canales.

El workflow **4 - Participant Advisory AI Respond, workflow-70 v27**, activo, conecta Ticket Updated → If Ticket Enriched → Get Ticket → If requester is a participant → Customer Message → Internal Notes → Ask Agent (AGENT-3) → Send Info to n8n. Ask AI - Generate Response está en un segmento desconectado del trigger y no explica el camino activo. Ask Agent recibe título, body/timeline externo y timeline interno. No se acreditó selección exclusiva de la última ejecución por esos nodos.

El 13 de septiembre se publicó v28 conservando esos ocho nodos y siete conexiones. Añade el header correlacionado y retira cinco nodos inalcanzables preexistentes con autorización explícita del usuario. v27 se conserva para rollback; ver FINAL_TRANSPORT_RELEASE.md.

Send Info to n8n entrega sólo agentResponse + ticketId a final-handling. El destino publicado **Decision, Response, Notes** ejecuta otro AI Agent para reparar JSON y aplicar su política de privacidad. Data extractor parsea ese resultado. Internal Message y Internal Stages crean notas **internas**; UpdateTicket cambia custom_fields.tnt__workflow_status a AI Response. No se observó envío externo ni escritura del stage Solved. set_stage_solved es una recomendación del modelo dentro de esa cadena.

El editor autorizado n8n ya fue inspeccionado con sesión autenticada. **FUG figuraba sin publicar en la observación del 10 de septiembre**; su versión previamente publicada se conserva como baseline. Esa lectura, como las versiones del manifiesto, está fechada y debe releerse antes de desplegar. La ejecución 184227 sólo sirvió para contrastar configuración y no se agregó al lote. Participant Search Fix sí tiene versión publicada; los fallos/error no se interpretan como cero coincidencias.

## Cambios concretos

| Superficie | Cambio y razón |
|---|---|
| GR/KQ formatters de FUG | Conservar ticket_job_id, orden de preguntas, cobertura, decisión, propósito de KQ y señales de revisión que antes se perdían. Mantener forma, etiquetas y escaping legacy. |
| Participant Search Fix / Validate Match1 | Corregir rama inalcanzable para múltiples resultados con empresa; validar count y distinguir cero real de null/error. |
| Participant Search Fix / salida unresolved | Conservar estado, conteos y tipos de identificadores aportados, sin valores PII. Ausencia de ejecución no equivale a usuario inexistente. |
| FUG / Knowledge Question | Enviar identity_context en la rama de búsqueda de cuenta fallida. KQ educativo directo conserva propósito general_knowledge en el backend. No se ha probado el bypass educativo en toda la clasificación legacy. |
| FUG / Stop Unsafe Terminal Result | Convertir resultado succeeded + human_review en paquete interno acotado; estados fallidos o incompatibles siguen fallando. Conectar a un nuevo terminal de nota interna, sin tags de enriquecimiento ni continuación al agente, para evitar bucles. |
| AGENT-3 y JSON parser | Responder por pregunta, distinguir verificación interna de pregunta al participante, conservar intención y no convertir generalización por privacidad en dato desconocido. Conservar tarifas y plazos. Aplicar la excepción de privacidad aprobada: sólo nombre propio y cifras verificadas con fuente/fecha; ocultar notas, autores y demás identificadores. |
| Final / Data extractor | Correlacionar ticketId con transporte, rechazar contrato inválido y mantener recomendación del modelo separada del estado. Sin decisión backend correlacionada independientemente, salida sólo para revisión interna. |

Los fragmentos Code son autocontenidos. knowledge-question-body.js es el **cuerpo** de una expresión: envolver con `{{ (() => { … })() }}` en el parámetro JSON existente. El nuevo terminal está especificado en el manifiesto y usa el contrato y credencial existentes de DevRev; no copiar encabezados con secretos. Las lecturas opcionales de nodos usan `isExecuted`, soportado por la [documentación oficial de n8n](https://github.com/n8n-io/n8n-docs/blob/main/docs/build/code-in-n8n/use-built-in-shortcuts/n8n-metadata.md).

## Verificación y límites

394 pruebas Node verifican candidatos con entradas sanitizadas, incluyendo fuente baseline que reproduce pérdidas, revisión en preguntas relacionadas, cobertura incompleta, denegación correcta, KQ educativo, identidad ambigua/error, correlación y handoff. Se incorporan a Cloud Build, con imagen Node fijada por digest. Esto no equivale a ejecución publicada de n8n.

Se compararon tres simulaciones baseline y tres candidatas de los dos prompts observados: cuatro preguntas, KQ educativo y custodia pendiente. La configuración LLM fue la de ForUsGuide, no una reproducción acreditada del modelo/entorno DevRev. El candidato conserva el orden y evita el checklist de identidad para educación. Esas seis salidas se conservan como evidencia anterior a la excepción de privacidad aprobada. El candidato actual incorpora una proyección cerrada de nombre/cifras con fuente y fecha y requiere evidencia explícita de identidad. La simulación candidate-v3 completó cinco casos: positivo conserva saludo/cifras, negativo generaliza valores, custodia no solicita documentos y cuatro preguntas conservan su orden. no se debe declarar conservación efectiva en producción ni publicación automática a partir de esa simulación.

La nota enriquecida contiene señales del backend, pero el transporte final no incluye una decisión independiente confiable. El candidato exige revisión interna; no certifica publicación a partir de texto reescrito por modelos. Si se requiere automatizar cierre/publicación, será necesaria correlación de ejecución fuera del LLM y prueba del consumidor real, dentro de autorización explícita.

La ejecución aislada real del parser mostró que anteponer el addendum no bastaba: las reglas antiguas seguían eliminando valores verificados. El manifiesto ahora sustituye el prompt completo por una sola política, mantiene la proyección cerrada y exige pruebas positivas y negativas en el editor real. El addendum de parser anterior queda como referencia histórica y no debe anteponerse al nuevo prompt. Las pruebas se ejecutan sólo sobre datos sintéticos, sin nodos de mensajes o actualización de tickets.

### AGENT-3 candidato v11 — sin publicar

El manifiesto vincula `candidates/pa-agent-system-v11.md` (sha256 `3ce2f261…`) como reemplazo completo del prompt de AGENT-3. **Es un candidato local sin publicar**: este paquete no autoriza su publicación y el prompt vigente sigue siendo el que haya en producción.

El borrador anterior, draft9, **falló su evaluación y no debe publicarse en ninguna forma**. Su hash se conserva en el manifiesto como `superseded_draft_sha256` sólo para poder identificar el artefacto superado; su archivo no forma parte de este paquete.

El `original_prompt_sha256` registrado es la **línea base histórica** observada el 11 de septiembre para AGENT-3 v8.0. No acredita el prompt vigente hoy: la base histórica no equivale al estado actual. Lo mismo vale para las versiones y estados de workflow del manifiesto, que son observaciones fechadas. Antes de cualquier despliegue hay que releer versión, estado y texto/hash del prompt vigente, guardar esa exportación como rollback y detenerse ante cualquier deriva; el rollback restaura exactamente ese estado capturado y no un hash inventado aquí.

La lista de archivos de Cloud Build incluye únicamente el candidato v11 necesario para verificar el manifiesto; los demás prompts privados permanecen excluidos. La prueba del contrato de subida exige ese archivo. La verificación remota de esta revisión del paquete queda pendiente hasta ejecutar su propio build.

#### Clases de evidencia, separadas

| Clase | Qué acredita | Qué no acredita |
|---|---|---|
| Verificador e integridad (`verify-package.js`) | Que las fuentes y prompts vinculados coinciden con sus hashes y que los fragmentos Code parsean. | Ningún comportamiento de modelo ni ejecución publicada. |
| Pruebas Node sobre datos sintéticos | Contratos del consumidor y del transporte. | Calidad del borrador del agente. |
| Simulación con réplica local del prompt | Comportamiento del texto candidato en un entorno replicado. | No es DevRev Preview, ni el modelo/entorno de DevRev, ni evidencia de agente desplegado. |

v11 **no se cargó en DevRev Preview** ni se ejecutó como agente; toda su evaluación es réplica local. No se probó re-comentar ningún ticket existente ni se tocó ticket alguno: no se enviaron mensajes a participantes, no se cambiaron stages y no se modificaron reviews. La valoración humana del texto sigue pendiente.

### Dependencia para nombres y cifras

El 11 de septiembre el usuario autorizó expresamente la excepción para el candidato. El backend construye `internal_disclosure_context` antes de combinar campos inferidos del ticket; publica únicamente la proyección `metadata.verified_participant_facts`. El formateador conserva esos campos con sus fuentes/fechas. Nombres ajenos, email, notas y autores no forman parte de esa proyección.

El usuario confirmó expresamente que se puede usar la cuenta que identifica el flujo PA. El candidato `handle-ticket-identity.js` agrega `identity_context` al cuerpo de Handle Ticket sólo si Participant Search declara Participant was found y sus IDs de participante/plan coinciden exactamente con Include Ticket data. Si falta una selección, hay IDs inválidos o discrepancia, devuelve null. El flag certifica esa asociación conforme al criterio aprobado; no acredita una autenticación adicional ni concede permiso de envío/cierre. La lectura actual confirma que el campo aún no está publicado. Se probaron el valor de la expresión, su aceptación por el modelo de API y la solicitud/extracción/mapping del nombre hasta el consumidor interno.

## Aplicación y rollback preparados

### FUG confirmado y prueba real de contratos — 14 septiembre

El usuario confirmó FUG (`BQvxrc3YiP4ZWbNQ`) como workflow vigente; la autorización existente de producción incluye su publicación deliberada. New Prod permanece intacto. El mapping cotejado por código es GR → `Format data for DevRev Internal notes`, KQ → `Format Data for DevRev`. El nodo `Format KQ for DevRev` está desactivado y no se modifica.

El borrador incorpora los cinco cambios de código/parámetros y un terminal `Record PA Human Review` con la credencial guardada `DevRev Production` (Header Auth). Sólo recibe desde `Stop Unsafe Terminal Result`; no tiene salidas. Su cuerpo usa `internal-handoff-body.js` para serializar JSON anidado, comillas y saltos de línea, conservando visibilidad interna. El grafo cambia de 44/48 a 45 nodos/49 conexiones, sin eliminar conexiones anteriores.

Una ejecución aislada real de n8n comprobó 19 condiciones con las cinco fuentes candidatas y entradas sintéticas: identidad coincidente/discrepante, cuatro preguntas, datos verificados/no verificados, bloqueo en pregunta relacionada, tres estados de búsqueda y handoff interno. No ejecutó nodos HTTP ni tickets originales. El nodo temporal se retiró después. Las pruebas Node y el verificador de integridad cubren también la serialización del terminal. Esto acredita contratos en el runtime; quedan pendientes la publicación verificada y los replays completos por observación.

1. Releer, en el momento del despliegue, versión y estado de cada workflow y el texto y hash del prompt vigente de AGENT-3; compararlos con lo registrado aquí y detenerse ante cualquier deriva material. Guardar primero la exportación autorizada actual como rollback exacto, sin credenciales en evidencia. Los valores del manifiesto son observaciones fechadas, no el estado vigente.
2. Aplicar contratos compatibles del backend y productor ForUsBots antes de exigir datos nuevos. Mantener permisos/configuración. La KB no cambia y no necesita reindexación para este paquete.
3. Preparar borradores de los nodos/prompt indicados, conservar formato y credenciales; conectar únicamente el terminal interno nuevo. No activar FUG por editarlo.
4. Ejecutar prueba controlada con publicación externa deshabilitada: payload antes/después de cada modelo, identidad, cuatro preguntas, handoff, correlación y efectos internos. Preservar hechos históricos; no responder a participantes.
5. Publicar sólo con autorización del usuario y después de esa prueba. Revertir exactamente nodos/addenda y conexiones del manifiesto a su exportación previa si falla. No alterar reviews ni sus evaluaciones.

Los hallazgos de credenciales del editor están documentados por separado para su propietario; no se incluyeron secretos en este paquete ni se modificaron permisos.
