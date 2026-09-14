# Paquete de consumidor PA — publicación por componentes

Implementación, main y producción autorizados. DevRev workflow70 v28 ya incorpora la correlación del transporte; el consumidor final se verifica por separado antes de publicarlo. El manifiesto `../n8n/release-manifest.manifest` vincula siete reemplazos de nodos, dos cambios de parámetros, un addendum DevRev y el reemplazo completo del prompt del parser. Las versiones iniciales permanecen como baseline; el registro local de Fase 3 contiene el estado efectivo y la evidencia de cada publicación. `node PA/n8n/verify-package.js` comprueba integridad y sintaxis sin ejecutar flujos. No se enviaron respuestas a participantes ni se actualizaron reviews.

## Camino realmente observado

DevRev **AGENT-3, Participant Advisory AI Agent, 8.0 – Live** tiene un prompt distinto de SYSTEM_PROMPT y de las copias V2/V3 del repositorio. Conservar el texto y formato vigentes; anteponer sólo PA_REMEDIATION_ADDENDUM.md. Deploy muestra canales Inactive; no se probó su invocación directa ni se activaron canales.

El workflow **4 - Participant Advisory AI Respond, workflow-70 v27**, activo, conecta Ticket Updated → If Ticket Enriched → Get Ticket → If requester is a participant → Customer Message → Internal Notes → Ask Agent (AGENT-3) → Send Info to n8n. Ask AI - Generate Response está en un segmento desconectado del trigger y no explica el camino activo. Ask Agent recibe título, body/timeline externo y timeline interno. No se acreditó selección exclusiva de la última ejecución por esos nodos.

El 13 de septiembre se publicó v28 conservando esos ocho nodos y siete conexiones. Añade el header correlacionado y retira cinco nodos inalcanzables preexistentes con autorización explícita del usuario. v27 se conserva para rollback; ver FINAL_TRANSPORT_RELEASE.md.

Send Info to n8n entrega sólo agentResponse + ticketId a final-handling. El destino publicado **Decision, Response, Notes** ejecuta otro AI Agent para reparar JSON y aplicar su política de privacidad. Data extractor parsea ese resultado. Internal Message y Internal Stages crean notas **internas**; UpdateTicket cambia custom_fields.tnt__workflow_status a AI Response. No se observó envío externo ni escritura del stage Solved. set_stage_solved es una recomendación del modelo dentro de esa cadena.

El editor autorizado n8n ya fue inspeccionado con sesión autenticada. **FUG está sin publicar desde el 10 de septiembre**; su versión previamente publicada se conserva como baseline. La ejecución 184227 sólo sirvió para contrastar configuración y no se agregó al lote. Participant Search Fix sí tiene versión publicada; los fallos/error no se interpretan como cero coincidencias.

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

63 pruebas Node verifican candidatos con entradas sanitizadas, incluyendo fuente baseline que reproduce pérdidas, revisión en preguntas relacionadas, cobertura incompleta, denegación correcta, KQ educativo, identidad ambigua/error, correlación y handoff. Se incorporan a Cloud Build, con imagen Node fijada por digest. Esto no equivale a ejecución publicada de n8n.

Se compararon tres simulaciones baseline y tres candidatas de los dos prompts observados: cuatro preguntas, KQ educativo y custodia pendiente. La configuración LLM fue la de ForUsGuide, no una reproducción acreditada del modelo/entorno DevRev. El candidato conserva el orden y evita el checklist de identidad para educación. Esas seis salidas se conservan como evidencia anterior a la excepción de privacidad aprobada. El candidato actual incorpora una proyección cerrada de nombre/cifras con fuente y fecha y requiere evidencia explícita de identidad. La simulación candidate-v3 completó cinco casos: positivo conserva saludo/cifras, negativo generaliza valores, custodia no solicita documentos y cuatro preguntas conservan su orden. no se debe declarar conservación efectiva en producción ni publicación automática a partir de esa simulación.

La nota enriquecida contiene señales del backend, pero el transporte final no incluye una decisión independiente confiable. El candidato exige revisión interna; no certifica publicación a partir de texto reescrito por modelos. Si se requiere automatizar cierre/publicación, será necesaria correlación de ejecución fuera del LLM y prueba del consumidor real, dentro de autorización explícita.

La ejecución aislada real del parser mostró que anteponer el addendum no bastaba: las reglas antiguas seguían eliminando valores verificados. El manifiesto ahora sustituye el prompt completo por una sola política, mantiene la proyección cerrada y exige pruebas positivas y negativas en el editor real. El addendum de parser anterior queda como referencia histórica y no debe anteponerse al nuevo prompt. Las pruebas se ejecutan sólo sobre datos sintéticos, sin nodos de mensajes o actualización de tickets.

### Dependencia para nombres y cifras

El 11 de septiembre el usuario autorizó expresamente la excepción para el candidato. El backend construye `internal_disclosure_context` antes de combinar campos inferidos del ticket; publica únicamente la proyección `metadata.verified_participant_facts`. El formateador conserva esos campos con sus fuentes/fechas. Nombres ajenos, email, notas y autores no forman parte de esa proyección.

El usuario confirmó expresamente que se puede usar la cuenta que identifica el flujo PA. El candidato `handle-ticket-identity.js` agrega `identity_context` al cuerpo de Handle Ticket sólo si Participant Search declara Participant was found y sus IDs de participante/plan coinciden exactamente con Include Ticket data. Si falta una selección, hay IDs inválidos o discrepancia, devuelve null. El flag certifica esa asociación conforme al criterio aprobado; no acredita una autenticación adicional ni concede permiso de envío/cierre. La lectura actual confirma que el campo aún no está publicado. Se probaron el valor de la expresión, su aceptación por el modelo de API y la solicitud/extracción/mapping del nombre hasta el consumidor interno.

## Aplicación y rollback preparados

1. Releer versiones, estado y hashes; detenerse ante cambios materiales. Guardar exportación autorizada actual como rollback, sin credenciales en evidencia.
2. Aplicar contratos compatibles del backend y productor ForUsBots antes de exigir datos nuevos. Mantener permisos/configuración. La KB no cambia y no necesita reindexación para este paquete.
3. Preparar borradores de los nodos/prompt indicados, conservar formato y credenciales; conectar únicamente el terminal interno nuevo. No activar FUG por editarlo.
4. Ejecutar prueba controlada con publicación externa deshabilitada: payload antes/después de cada modelo, identidad, cuatro preguntas, handoff, correlación y efectos internos. Preservar hechos históricos; no responder a participantes.
5. Publicar sólo con autorización del usuario y después de esa prueba. Revertir exactamente nodos/addenda y conexiones del manifiesto a su exportación previa si falla. No alterar reviews ni sus evaluaciones.

Los hallazgos de credenciales del editor están documentados por separado para su propietario; no se incluyeron secretos en este paquete ni se modificaron permisos.
