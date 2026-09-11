# Paquete de consumidor PA — candidato local

Implementación aprobada; publicación pendiente. Ningún workflow, prompt remoto, mensaje ni review fue modificado. El manifiesto `../n8n/release-manifest.manifest` vincula siete reemplazos y dos addenda con las versiones y hashes contrastados el 11 de septiembre de 2026. `node PA/n8n/verify-package.js` comprueba integridad y sintaxis sin ejecutar flujos.

## Camino realmente observado

DevRev **AGENT-3, Participant Advisory AI Agent, 8.0 – Live** tiene un prompt distinto de SYSTEM_PROMPT y de las copias V2/V3 del repositorio. Conservar el texto y formato vigentes; anteponer sólo PA_REMEDIATION_ADDENDUM.md. Deploy muestra canales Inactive; no se probó su invocación directa ni se activaron canales.

El workflow **4 - Participant Advisory AI Respond, workflow-70 v27**, activo, conecta Ticket Updated → If Ticket Enriched → Get Ticket → If requester is a participant → Customer Message → Internal Notes → Ask Agent (AGENT-3) → Send Info to n8n. Ask AI - Generate Response está en un segmento desconectado del trigger y no explica el camino activo. Ask Agent recibe título, body/timeline externo y timeline interno. No se acreditó selección exclusiva de la última ejecución por esos nodos.

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
| AGENT-3 y JSON parser | Responder por pregunta, distinguir verificación interna de pregunta al participante, conservar intención y no convertir generalización por privacidad en dato desconocido. No modificar tarifas, plazos ni reglas de privacidad. |
| Final / Data extractor | Correlacionar ticketId con transporte, rechazar contrato inválido y mantener recomendación del modelo separada del estado. Sin decisión backend correlacionada independientemente, salida sólo para revisión interna. |

Los fragmentos Code son autocontenidos. knowledge-question-body.js es el **cuerpo** de una expresión: envolver con `{{ (() => { … })() }}` en el parámetro JSON existente. El nuevo terminal está especificado en el manifiesto y usa el contrato y credencial existentes de DevRev; no copiar encabezados con secretos. Las lecturas opcionales de nodos usan `isExecuted`, soportado por la [documentación oficial de n8n](https://github.com/n8n-io/n8n-docs/blob/main/docs/build/code-in-n8n/use-built-in-shortcuts/n8n-metadata.md).

## Verificación y límites

44 pruebas Node verifican candidatos con entradas sanitizadas, incluyendo fuente baseline que reproduce pérdidas, revisión en preguntas relacionadas, cobertura incompleta, denegación correcta, KQ educativo, identidad ambigua/error, correlación y handoff. Se incorporan a Cloud Build, con imagen Node fijada por digest. Esto no equivale a ejecución publicada de n8n.

Se compararon tres simulaciones baseline y tres candidatas de los dos prompts observados: cuatro preguntas, KQ educativo y custodia pendiente. La configuración LLM fue la de ForUsGuide, no una reproducción acreditada del modelo/entorno DevRev. El candidato conserva el orden y evita el checklist de identidad para educación. Permanecen límites: el parser borra nombres y cifras por su política vigente; puede alterar la recomendación solved y el agente aún puede sugerir documentos opcionales en un handoff interno. No declarar resueltos saludo personalizado, conservación de importes ni publicación automática.

La nota enriquecida contiene señales del backend, pero el transporte final no incluye una decisión independiente confiable. El candidato exige revisión interna; no certifica publicación a partir de texto reescrito por modelos. Si se requiere automatizar cierre/publicación, será necesaria correlación de ejecución fuera del LLM y prueba del consumidor real, dentro de autorización explícita.

## Aplicación y rollback preparados

1. Releer versiones, estado y hashes; detenerse ante cambios materiales. Guardar exportación autorizada actual como rollback, sin credenciales en evidencia.
2. Aplicar contratos compatibles del backend y productor ForUsBots antes de exigir datos nuevos. Mantener permisos/configuración. La KB no cambia y no necesita reindexación para este paquete.
3. Preparar borradores de los nodos/prompt indicados, conservar formato y credenciales; conectar únicamente el terminal interno nuevo. No activar FUG por editarlo.
4. Ejecutar prueba controlada con publicación externa deshabilitada: payload antes/después de cada modelo, identidad, cuatro preguntas, handoff, correlación y efectos internos. Preservar hechos históricos; no responder a participantes.
5. Publicar sólo con autorización del usuario y después de esa prueba. Revertir exactamente nodos/addenda y conexiones del manifiesto a su exportación previa si falla. No alterar reviews ni sus evaluaciones.

Los hallazgos de credenciales del editor están documentados por separado para su propietario; no se incluyeron secretos en este paquete ni se modificaron permisos.
