/**
 * Ephemeral display preferences for the ticket console.
 *
 * Theme and language deliberately live only for the lifetime of this page. The
 * console handles customer-linked material, so a display preference is not a
 * reason to give the browser a durable store. Spanish translation is likewise
 * deliberately narrow: only exact reviewer-interface phrases and a short list
 * of bounded count/status patterns are eligible. Ticket text is never sent
 * through a general-purpose translator.
 */

const ENGLISH_TO_SPANISH = new Map([
  // Document, navigation, and preferences.
  ["Ticket Review Console", "Consola de revisión de tickets"],
  ["Skip to main content", "Ir al contenido principal"],
  ["ForUs Ticket Review", "Revisión de tickets ForUs"],
  ["Ticket evaluation workspace", "Espacio de evaluación de tickets"],
  ["Human-reviewed · Evidence-backed", "Revisión humana · Evidencia verificable"],
  ["Quality operations", "Operaciones de calidad"],
  ["Reviewer workspace", "Espacio de revisores"],
  ["Ticket reviews", "Evaluaciones de tickets"],
  ["Use the CRM to review the ticket and final answer, then record the quality decision here.", "Revisa el ticket y la respuesta final en el CRM, y después registra aquí la decisión de calidad."],
  ["RAG quality command center", "Centro de control de calidad RAG"],
  ["Review RAG answers with the full story in view.", "Revisa las respuestas RAG con todo el contexto a la vista."],
  ["Move from execution signal to human decision without losing context.", "Pasa de la señal de ejecución a la decisión humana sin perder contexto."],
  ["Review RAG answers with the full story in view. Move from execution signal to human decision without losing context.", "Revisa las respuestas RAG con todo el contexto a la vista. Pasa de la señal de ejecución a la decisión humana sin perder contexto."],
  ["Ticket evaluation flow", "Flujo de evaluación del ticket"],
  ["Ticket", "Ticket"],
  ["RAG response", "Respuesta RAG"],
  ["Human review", "Revisión humana"],
  ["One traceable quality loop", "Un ciclo de calidad trazable"],
  ["Current page snapshot", "Resumen de la página actual"],
  ["on this page", "en esta página"],
  ["Theme", "Tema"],
  ["Language", "Idioma"],
  ["English", "Inglés"],
  ["Spanish", "Español"],
  ["Dark", "Oscuro"],
  ["Light", "Claro"],
  ["Switch to dark mode", "Cambiar al modo oscuro"],
  ["Switch to light mode", "Cambiar al modo claro"],
  ["Environment", "Entorno"],
  ["Signed in", "Sesión iniciada"],
  ["Signed in as", "Sesión iniciada como"],
  ["Loading who you are signed in as…", "Cargando tu identidad…"],
  ["Role", "Rol"],
  ["viewer", "lector"],
  ["reviewer", "revisor"],
  ["admin", "administrador"],
  ["remediator", "remediador"],
  ["agent", "agente"],
  ["Checking the console…", "Comprobando la consola…"],
  ["Console dependencies are not ready", "Las dependencias de la consola no están listas"],
  ["Ready; evidence lookup is not configured", "Lista; la consulta de evidencia no está configurada"],
  ["Ready", "Lista"],
  ["unknown", "desconocido"],
  ["The server reports these as unavailable in this build: remediation batches.", "El servidor informa que estas funciones no están disponibles en esta versión: lotes de remediación."],

  // Session, request, and dependency failures.
  ["Your session ended", "Tu sesión terminó"],
  ["Reload the page to sign in again. Nothing was saved.", "Recarga la página para volver a iniciar sesión. No se guardó nada."],
  ["Ask an administrator for the reviewer role, then reload.", "Solicita a un administrador el rol de revisor y después recarga."],
  ["That record is not available", "Ese registro no está disponible"],
  ["It may have been removed, or it may be outside your scope.", "Puede haberse eliminado o estar fuera de tu alcance."],
  ["That combination of filters is not supported", "Esa combinación de filtros no es compatible"],
  ["The queue accepts a status set plus at most one facet, and an exact ticket lookup runs on its own. Clear a filter and try again.", "La cola acepta un conjunto de estados y, como máximo, una faceta; una búsqueda exacta de ticket se ejecuta por separado. Limpia un filtro e inténtalo de nuevo."],
  ["That page is no longer available", "Esa página ya no está disponible"],
  ["Returning to the first page of the current filters.", "Volviendo a la primera página de los filtros actuales."],
  ["This change needs the record version", "Este cambio necesita la versión del registro"],
  ["Reload the ticket and try again.", "Recarga el ticket e inténtalo de nuevo."],
  ["The record version was not usable", "La versión del registro no era válida"],
  ["Someone else changed this review", "Otra persona modificó esta revisión"],
  ["Reload to see their version before saving yours.", "Recarga para ver su versión antes de guardar la tuya."],
  ["That change conflicts with the review's current state", "Ese cambio entra en conflicto con el estado actual de la revisión"],
  ["Reload the review to see where it is now.", "Recarga la revisión para consultar su estado actual."],
  ["That evidence suggestion is no longer linkable", "Esa sugerencia de evidencia ya no se puede vincular"],
  ["A suggestion is short-lived and is bound to the ticket, the review, and you. Reload the ticket to get a current one.", "Una sugerencia dura poco y está vinculada al ticket, la revisión y tu sesión. Recarga el ticket para obtener una actual."],
  ["That request was already used for something else", "Esa solicitud ya se utilizó para otra acción"],
  ["Retry the action; a fresh key is generated each time.", "Reintenta la acción; cada vez se genera una clave nueva."],
  ["Too many requests", "Demasiadas solicitudes"],
  ["Refresh is paused briefly so the console stays within its bound.", "La actualización se pausa brevemente para mantener la consola dentro de su límite."],
  ["The ticket system is rate limiting this console", "El sistema de tickets está limitando las solicitudes de esta consola"],
  ["Durable review data is unaffected. Live ticket data will return shortly.", "Los datos durables de revisión no se ven afectados. Los datos en vivo del ticket volverán pronto."],
  ["Live ticket data is unavailable", "Los datos en vivo del ticket no están disponibles"],
  ["The durable review queue is unaffected, so the Review queue tab still works.", "La cola durable de revisión no se ve afectada, por lo que la pestaña de cola de revisión sigue funcionando."],
  ["The ticket system returned something unusable", "El sistema de tickets devolvió una respuesta no válida"],
  ["This is a service-side problem; the request id below identifies it.", "Es un problema del servicio; el ID de solicitud de abajo lo identifica."],
  ["Evidence lookup is unavailable", "La consulta de evidencia no está disponible"],
  ["Reviews and tickets are unaffected.", "Las revisiones y los tickets no se ven afectados."],
  ["That request was too large", "Esa solicitud era demasiado grande"],
  ["Shorten the text and try again.", "Acorta el texto e inténtalo de nuevo."],
  ["The request was missing its safety key", "A la solicitud le faltaba su clave de seguridad"],
  ["Retry the action.", "Reintenta la acción."],
  ["The console sent an unusable content type", "La consola envió un tipo de contenido no válido"],
  ["Reload the page; this indicates a stale script.", "Recarga la página; esto indica que el script está desactualizado."],
  ["The console is still starting", "La consola todavía se está iniciando"],
  ["Try again in a moment.", "Inténtalo de nuevo en un momento."],
  ["The server refused that request", "El servidor rechazó esa solicitud"],
  ["Check the filters and try again.", "Revisa los filtros e inténtalo de nuevo."],
  ["Reload to sign in again.", "Recarga para volver a iniciar sesión."],
  ["Not permitted", "No permitido"],
  ["Your role does not allow this.", "Tu rol no permite esta acción."],
  ["That change conflicts", "Ese cambio entra en conflicto"],
  ["Reload and try again.", "Recarga e inténtalo de nuevo."],
  ["This change needs the current record version", "Este cambio necesita la versión actual del registro"],
  ["Requests are paused briefly.", "Las solicitudes están pausadas brevemente."],
  ["A dependency returned something unusable", "Una dependencia devolvió una respuesta no válida"],
  ["This is a service-side problem.", "Es un problema del servicio."],
  ["A dependency is unavailable", "Una dependencia no está disponible"],
  ["Durable review data is unaffected.", "Los datos durables de revisión no se ven afectados."],
  ["Something went wrong on the server", "Ocurrió un problema en el servidor"],
  ["The request id below identifies this failure in the logs.", "El ID de solicitud de abajo identifica este fallo en los registros."],
  ["The request could not be completed", "No se pudo completar la solicitud"],
  ["Try again, or reload the page.", "Inténtalo de nuevo o recarga la página."],

  // Queue overview and filters.
  ["Tickets ready for evaluation", "Tickets listos para evaluar"],
  ["Choose a ticket, record the rating, and add only the context needed for follow-up.", "Elige un ticket, registra la calificación y agrega solo el contexto necesario para el seguimiento."],
  ["Open a ticket, record the evaluation, and continue with the next review.", "Abre un ticket, registra la evaluación y continúa con la siguiente revisión."],
  ["Technical filters", "Filtros técnicos"],
  ["Technical batch actions", "Acciones técnicas por lote"],
  ["RAG ticket evaluations", "Evaluaciones RAG de tickets"],
  ["Execution indicators", "Indicadores de ejecución"],
  ["The API returns bounded pages rather than a global aggregate. Each indicator clearly reports its current-page count.", "La API devuelve páginas acotadas en vez de un total global. Cada indicador señala claramente el conteo de la página actual."],
  ["Unreviewed executions", "Ejecuciones sin revisar"],
  ["Failed or partial RAG", "RAG fallido o parcial"],
  ["Active remediation", "Remediación activa"],
  ["The administrative API returns bounded pages and no global aggregate. Each indicator therefore keeps the global figure blank and states the count for the current page separately.", "La API administrativa devuelve páginas acotadas, sin un total global. Por eso, cada indicador muestra por separado el conteo de la página actual."],
  ["Counts reflect only the executions visible on this page.", "Los conteos reflejan solo las ejecuciones visibles en esta página."],
  ["RAG execution queue", "Cola de ejecuciones RAG"],
  ["Review queue", "Cola de revisión"],
  ["Every row is a durable, ticket-linked RAG execution. Ticket context is added after the run and never creates a queue item by itself.", "Cada fila es una ejecución RAG durable vinculada a un ticket. El contexto se agrega después de la ejecución y nunca crea por sí solo un elemento en la cola."],
  ["Every row was created by a ticket-associated RAG route. DevRev adds ticket context after the run is durable and cannot add a row by itself.", "Cada fila proviene de una ruta RAG asociada a un ticket. DevRev agrega el contexto cuando la ejecución ya es durable y no puede crear una fila por sí solo."],
  ["Filters", "Filtros"],
  ["Filter executions", "Filtrar ejecuciones"],
  ["Filter tickets", "Filtrar tickets"],
  ["Close filters", "Cerrar filtros"],
  ["Exact execution ID", "ID exacto de ejecución"],
  ["Exact ticket ID", "ID exacto de ticket"],
  ["RAG route", "Ruta RAG"],
  ["Any RAG route", "Cualquier ruta RAG"],
  ["Any outcome", "Cualquier resultado"],
  ["Knowledge Question", "Pregunta de conocimiento"],
  ["Run status", "Estado de ejecución"],
  ["Any run status", "Cualquier estado de ejecución"],
  ["Review status", "Estado de revisión"],
  ["Any review status", "Cualquier estado de revisión"],
  ["Refresh", "Actualizar"],
  ["Clear all", "Limpiar todo"],
  ["Active filters", "Filtros activos"],
  ["Selected executions", "Ejecuciones seleccionadas"],
  ["No executions selected", "No hay ejecuciones seleccionadas"],
  ["Select all on this page", "Seleccionar todo en esta página"],
  ["Clear selection", "Limpiar selección"],
  ["Create remediation batch", "Crear lote de remediación"],
  ["Select runs whose linked review has a version. Each review is frozen at the version shown.", "Selecciona ejecuciones cuya revisión vinculada tenga una versión. Cada revisión queda fijada en la versión mostrada."],
  ["Results", "Resultados"],
  ["Ticket-associated RAG executions with review and DevRev enrichment status.", "Ejecuciones RAG asociadas a tickets con estado de revisión y contexto de DevRev."],
  ["Authorized ticket-associated RAG invocations in stable ledger order.", "Invocaciones RAG autorizadas y asociadas a tickets, en orden estable del registro."],
  ["Select", "Seleccionar"],
  ["Select every execution on this page", "Seleccionar todas las ejecuciones de esta página"],
  ["Select every ticket on this page", "Seleccionar todos los tickets de esta página"],
  ["Execution", "Ejecución"],
  ["Execution ID", "ID de ejecución"],
  ["Invocation ID", "ID de invocación"],
  ["Job ID", "ID de trabajo"],
  ["Ticket ID", "ID de ticket"],
  ["Route", "Ruta"],
  ["DevRev context", "Contexto de DevRev"],
  ["Started", "Inicio"],
  ["Received", "Recibido"],
  ["Rating", "Calificación"],
  ["Reviewer", "Revisor"],
  ["Actions", "Acciones"],
  ["Review", "Evaluar"],
  ["Tickets ready for evaluation in stable queue order.", "Tickets listos para evaluar en el orden estable de la cola."],
  ["Tickets waiting for a quality decision, with their current review status and owner.", "Tickets pendientes de una decisión de calidad, con su estado de revisión y responsable actuales."],
  ["Previous page", "Página anterior"],
  ["Next page", "Página siguiente"],
  ["Pagination", "Paginación"],
  ["No RAG executions match these filters", "Ninguna ejecución RAG coincide con estos filtros"],
  ["Clear a filter, or verify the execution and ticket identifiers.", "Limpia un filtro o verifica los identificadores de ejecución y ticket."],
  ["That request failed", "La solicitud falló"],
  ["Refreshing…", "Actualizando…"],
  ["Showing the last successful answer; the newest request failed.", "Se muestra la última respuesta correcta; la solicitud más reciente falló."],
  ["This page is incomplete.", "Esta página está incompleta."],
  ["Dismiss this message", "Descartar este mensaje"],
  ["Freezing a batch needs the remediator role.", "Fijar un lote requiere el rol de remediador."],
  ["Select executions with a versioned review to create a remediation batch.", "Selecciona ejecuciones con una revisión versionada para crear un lote de remediación."],
  ["Your role does not allow this", "Tu rol no permite esta acción"],
  ["Freezing a remediation batch needs the remediator role.", "Fijar un lote de remediación requiere el rol de remediador."],
  ["Nothing to freeze", "No hay nada que fijar"],
  ["None of the selected tickets has a durable review yet.", "Ninguno de los tickets seleccionados tiene todavía una revisión durable."],

  // Shared states and taxonomy.
  ["Unreviewed", "Sin revisar"],
  ["Reviewed", "Revisado"],
  ["Triaged", "Clasificado"],
  ["Planned", "Planificado"],
  ["In progress", "En curso"],
  ["Changes proposed", "Cambios propuestos"],
  ["Verifying", "Verificando"],
  ["Resolved", "Resuelto"],
  ["Blocked", "Bloqueado"],
  ["Will not fix", "No se corregirá"],
  ["Succeeded", "Correcta"],
  ["Partial", "Parcial"],
  ["Failed", "Fallida"],
  ["Timed out", "Tiempo agotado"],
  ["Loaded", "Cargado"],
  ["Unavailable", "No disponible"],
  ["Unknown", "Desconocido"],
  ["Not recorded", "No registrado"],
  ["Not loaded", "No cargado"],
  ["Not set", "Sin definir"],
  ["Unassigned", "Sin asignar"],
  ["Historical value", "Valor histórico"],
  ["Correct", "Correcto"],
  ["Knowledge gap", "Vacío de conocimiento"],
  ["Knowledge conflict", "Conflicto de conocimiento"],
  ["Retrieval miss", "Fallo de recuperación"],
  ["Chunking or metadata", "Segmentación o metadatos"],
  ["Prompt instruction", "Instrucción del prompt"],
  ["Orchestration logic", "Lógica de orquestación"],
  ["Source data", "Datos de origen"],
  ["Privacy or compliance", "Privacidad o cumplimiento"],
  ["Other", "Otro"],
  ["Low", "Baja"],
  ["Medium", "Media"],
  ["High", "Alta"],
  ["Critical", "Crítica"],
  ["Fixed", "Corregido"],
  ["No change", "Sin cambios"],
  ["Duplicate", "Duplicado"],
  ["Accepted risk", "Riesgo aceptado"],
  ["Passed", "Aprobada"],
  ["Completed", "Completada"],

  // Detail shell and captured execution.
  ["Technical audit details", "Detalles de auditoría técnica"],
  ["Execution diagnostics, evidence, conversation, history, and remediation tools for technical audits.", "Diagnósticos de ejecución, evidencia, conversación, historial y herramientas de remediación para auditorías técnicas."],
  ["Evidence and audit details", "Evidencia y detalles de auditoría"],
  ["Evidence and audit details.", "Evidencia y detalles de auditoría."],
  ["Review the answer first, then open only the evidence and technical detail you need.", "Revisa primero la respuesta y después abre solo la evidencia y el detalle técnico que necesites."],
  ["Start with the recorded answer and decision, then open the evidence, diagnostics, or exact runtime data only when you need it.", "Comienza por la respuesta y la decisión registradas; abre la evidencia, los diagnósticos o los datos exactos de ejecución solo cuando los necesites."],
  ["Every safe recorded field remains available; technical keys are shown beside plain-language labels.", "Todos los campos seguros registrados permanecen disponibles; las claves técnicas aparecen junto a etiquetas en lenguaje claro."],
  ["Answer and decision", "Respuesta y decisión"],
  ["The participant-facing answer and the decision context recorded by the application.", "La respuesta visible para el participante y el contexto de decisión registrado por la aplicación."],
  ["What the system answered and why it chose this route.", "Qué respondió el sistema y por qué eligió esta ruta."],
  ["This is the answer recorded by the execution. It does not by itself prove what the participant saw; use the conversation visibility labels for that distinction.", "Esta es la respuesta registrada por la ejecución. Por sí sola no demuestra qué vio el participante; usa las etiquetas de visibilidad de la conversación para distinguirlo."],
  ["Recorded decision rationale", "Justificación registrada de la decisión"],
  ["These are the explicit classification and outcome explanations recorded by the application. Provider hidden chain-of-thought is not collected or shown.", "Estas son las explicaciones explícitas de clasificación y resultado registradas por la aplicación. No se recopila ni muestra el razonamiento interno del proveedor."],
  ["Evidence used", "Evidencia utilizada"],
  ["Every source and bounded excerpt captured for this execution.", "Todas las fuentes y los fragmentos acotados capturados para esta ejecución."],
  ["The final sources and bounded excerpts recorded with this answer.", "Las fuentes finales y los fragmentos acotados registrados con esta respuesta."],
  ["Final source articles", "Artículos fuente finales"],
  ["Sources attached to the persisted answer. Candidate retrieval records remain in the RAG evidence workspace above, so final sources and candidates are not conflated.", "Fuentes adjuntas a la respuesta persistida. Los registros candidatos de recuperación permanecen en el espacio de evidencia RAG anterior para no confundirlos con las fuentes finales."],
  ["Safe previews captured with the execution. Full knowledge-base chunks remain outside the browser privacy boundary.", "Vistas previas seguras capturadas con la ejecución. Los fragmentos completos de la base de conocimiento permanecen fuera del límite de privacidad del navegador."],
  ["Data coverage and diagnostics", "Cobertura de datos y diagnósticos"],
  ["Mapping, retrieval, and safety signals. Expand a group to inspect every recorded field.", "Señales de mapeo, recuperación y seguridad. Expande un grupo para consultar todos los campos registrados."],
  ["What the pipeline found, mapped, missed, or flagged.", "Lo que el flujo encontró, mapeó, omitió o marcó."],
  ["How to read this section.", "Cómo leer esta sección."],
  ["A missing field is a recorded coverage signal, not automatically a blocker. Compare it with the final outcome and rationale before deciding whether the answer is valid.", "Un campo faltante es una señal de cobertura registrada, no un bloqueo automático. Compáralo con el resultado final y la justificación antes de decidir si la respuesta es válida."],
  ["Ticket context loading", "Carga del contexto del ticket"],
  ["Recorded diagnostics", "Diagnósticos registrados"],
  ["Runtime and exact identifiers", "Ejecución e identificadores exactos"],
  ["Model, timing, retrieval metadata, and immutable execution identifiers.", "Modelo, tiempos, metadatos de recuperación e identificadores inmutables de la ejecución."],
  ["Execution identity, timing, model, and retrieval trace.", "Identidad, tiempos, modelo y traza de recuperación de la ejecución."],
  ["Execution overview", "Resumen de la ejecución"],
  ["Model, timing, and retrieval", "Modelo, tiempos y recuperación"],
  ["Ticket snapshot at execution", "Instantánea del ticket durante la ejecución"],
  ["The authorized ticket context attached after the RAG run was recorded.", "El contexto autorizado del ticket adjuntado después de registrar la ejecución RAG."],
  ["The CRM fields visible at the time of this review.", "Los campos del CRM visibles al momento de esta revisión."],
  ["Recorded ticket context", "Contexto registrado del ticket"],
  ["Internal owner identifier", "Identificador interno del responsable"],
  ["Back to the ticket list", "Volver a la lista de tickets"],
  ["Focused review", "Revisión enfocada"],
  ["Breadcrumb", "Ruta de navegación"],
  ["Close ticket detail", "Cerrar el detalle del ticket"],
  ["Ticket detail", "Detalle del ticket"],
  ["RAG execution", "Ejecución RAG"],
  ["Inquiry", "Consulta"],
  ["Attempt", "Intento"],
  ["Lease epoch", "Época del arrendamiento"],
  ["Generated answer", "Respuesta generada"],
  ["No generated answer was recorded.", "No se registró una respuesta generada."],
  ["An empty generated answer was recorded.", "Se registró una respuesta generada vacía."],
  ["Opening", "Introducción"],
  ["Key points", "Puntos clave"],
  ["Recommended steps", "Pasos recomendados"],
  ["Warnings", "Advertencias"],
  ["Action", "Acción"],
  ["Details", "Detalles"],
  ["Step", "Paso"],
  ["Answer", "Respuesta"],
  ["Response", "Respuesta"],
  ["Response to participant", "Respuesta para el participante"],
  ["Response source", "Origen de la respuesta"],
  ["Inquiries", "Consultas"],
  ["Total inquiries in ticket", "Total de consultas del ticket"],
  ["Generate Response", "Generar respuesta"],
  ["ForUsBots elapsed time (seconds)", "Tiempo de ForUsBots (s)"],
  ["ForUsBots job ID", "ID de tarea de ForUsBots"],
  ["ForUsBots participant elapsed time (seconds)", "Tiempo del participante en ForUsBots (s)"],
  ["ForUsBots participant job ID", "ID de tarea del participante en ForUsBots"],
  ["Used chunks", "Fragmentos utilizados"],
  ["Final outcome", "Resultado final"],
  ["Classification", "Clasificación"],
  ["Classifier", "Clasificador"],
  ["Confidence", "Confianza"],
  ["Recorded rationale", "Justificación registrada"],
  ["Checkpoint", "Punto de control"],
  ["Retrieval metadata", "Metadatos de recuperación"],
  ["Minimum score met", "Cumple la puntuación mínima"],
  ["Match count", "Cantidad de coincidencias"],
  ["Audience", "Audiencia"],
  ["Fallbacks used", "Alternativas utilizadas"],
  ["Field mapping", "Mapeo de campos"],
  ["Source plan", "Plan de origen"],
  ["Destination account", "Cuenta de destino"],
  ["LLM invoked", "LLM invocado"],
  ["Field", "Campo"],
  ["Required", "Obligatorio"],
  ["Fields not mapped", "Campos sin mapear"],
  ["Unmapped fields", "Campos sin mapear"],
  ["Mapped modules", "Módulos mapeados"],
  ["Data collection", "Recopilación de datos"],
  ["Safe for participant reply", "Respuesta segura para el participante"],
  ["Participant reply safe", "Respuesta segura para el participante"],
  ["Core eligibility supported", "Elegibilidad principal respaldada"],
  ["Primary decision signal", "Señal principal de decisión"],
  ["Mark ticket solved", "Marcar ticket como resuelto"],
  ["Stage reason", "Motivo de la etapa"],
  ["Escalation", "Escalamiento"],
  ["Requires escalation", "Requiere escalamiento"],
  ["Duration", "Duración"],
  ["Latency", "Latencia"],
  ["Input tokens", "Tokens de entrada"],
  ["Output tokens", "Tokens de salida"],
  ["Total tokens", "Tokens totales"],
  ["Metadata", "Metadatos"],
  ["Correlation", "Correlación"],
  ["Trace ID", "ID de trazabilidad"],
  ["Namespace", "Espacio de nombres"],
  ["Matches", "Coincidencias"],
  ["Fixture record", "Registro de prueba"],
  ["Unnamed field", "Campo sin nombre"],
  ["Empty text", "Texto vacío"],
  ["Yes", "Sí"],
  ["No", "No"],
  ["None recorded", "Ninguno registrado"],
  ["No fields recorded", "No se registraron campos"],
  ["Recorded details", "Detalles registrados"],
  ["Recorded answer details", "Detalles registrados de la respuesta"],
  ["Additional step details", "Detalles adicionales del paso"],
  ["Additional answer details", "Detalles adicionales de la respuesta"],
  ["Additional response record", "Registro adicional de la respuesta"],
  ["Additional generated answer record", "Registro adicional de la respuesta generada"],
  ["Guided structured response", "Respuesta estructurada guiada"],
  ["Response to participant channel", "Canal de respuesta para el participante"],
  ["Answer channel", "Canal de respuesta"],
  ["Response channel", "Canal de respuesta alternativa"],
  ["Additional recorded answer channel", "Canal adicional de respuesta registrado"],
  ["Recorded answer channels", "Canales de respuesta registrados"],
  ["Same recorded content as", "Mismo contenido registrado que"],
  ["Distinct recorded content shown separately at", "Contenido registrado distinto mostrado por separado en"],
  ["This recorded channel differs from the final answer, so it remains available separately.", "Este canal registrado difiere de la respuesta final, por lo que permanece disponible por separado."],
  ["This answer channel explicitly recorded no value.", "Este canal de respuesta registró explícitamente que no había valor."],
  ["Structured outcome rationale", "Justificación estructurada del resultado"],
  ["Identifiers", "Identificadores"],
  ["Model and prompt", "Modelo y prompt"],
  ["Index and deployment", "Índice y despliegue"],
  ["Score", "Puntuación"],
  ["Relevance", "Relevancia"],
  ["Lookup key version", "Versión de clave de búsqueda"],
  ["Ingress key version", "Versión de clave de ingreso"],
  ["Actor principal hash", "Hash del principal actor"],
  ["Retrieval correlation", "Correlación de recuperación"],
  ["Retrieval trust", "Confianza de recuperación"],
  ["Retrieval correlation source", "Origen de correlación de recuperación"],
  ["Missing retrieval provenance", "Falta procedencia de recuperación"],
  ["Exact link identifiers", "Identificadores exactos del enlace"],
  ["Review id", "ID de revisión"],
  ["Link id", "ID del enlace"],
  ["Source URL", "URL de origen"],
  ["Recorded evidence warnings", "Advertencias registradas de evidencia"],
  ["Recorded retrieval provenance", "Procedencia de recuperación registrada"],
  ["Retrieval record", "Registro de recuperación"],
  ["Retrieval records", "Registros de recuperación"],
  ["Complete recorded run", "Registro completo de la ejecución"],
  ["Reviewer subject", "Identificador de sujeto del revisor"],
  ["Reviewer display name", "Nombre visible del revisor"],
  ["Unavailable reason", "Motivo de indisponibilidad"],
  ["Exact timestamp", "Marca de tiempo exacta"],
  ["This persisted RAG execution recorded no source articles or bounded chunks.", "Esta ejecución RAG persistida no registró artículos fuente ni fragmentos acotados."],
  ["Detected inquiry", "Consulta detectada"],
  ["Detected topic", "Tema detectado"],
  ["Classification confidence", "Confianza de la clasificación"],
  ["Pipeline diagnostics", "Diagnósticos del flujo"],
  ["Recorded coverage gaps", "Vacíos de cobertura registrados"],
  ["Recorded runtime data", "Datos de ejecución registrados"],
  ["Structured message", "Mensaje estructurado"],
  ["Inquiry number", "Número de consulta"],
  ["Event digest", "Resumen criptográfico del evento"],
  ["Execution error", "Error de ejecución"],
  ["Hydration error code", "Código de error de carga de contexto"],
  ["Manual reconciliation required", "Requiere conciliación manual"],
  ["Deterministic mapped", "Mapeo determinista"],
  ["Rejected", "Rechazados"],
  ["Empty filters", "Filtros vacíos"],
  ["Structured rationale", "Justificación estructurada"],
  ["Classification rationale", "Justificación de la clasificación"],
  ["Outcome rationale", "Justificación del resultado"],
  ["This is the explicit classification and outcome rationale recorded by the application. Provider hidden chain-of-thought is not collected or shown.", "Esta es la justificación explícita de clasificación y resultado registrada por la aplicación. No se recopila ni muestra el razonamiento interno del proveedor."],
  ["Diagnostics", "Diagnósticos"],
  ["No diagnostics were recorded.", "No se registraron diagnósticos."],
  ["Coverage gaps", "Vacíos de cobertura"],
  ["No coverage gaps were recorded.", "No se registraron vacíos de cobertura."],
  ["Source articles", "Artículos fuente"],
  ["No source articles were recorded.", "No se registraron artículos fuente."],
  ["Bounded chunk evidence", "Evidencia acotada de fragmentos"],
  ["No bounded chunk evidence was recorded.", "No se registró evidencia acotada de fragmentos."],
  ["Model and timing", "Modelo y tiempos"],
  ["Model", "Modelo"],
  ["Timing", "Tiempos"],
  ["Retrieval", "Recuperación"],
  ["DevRev context status", "Estado del contexto de DevRev"],
  ["DevRev context loaded for this execution.", "El contexto de DevRev se cargó para esta ejecución."],
  ["This execution did not pass the authorized DevRev hydration boundary.", "Esta ejecución no superó el límite autorizado de hidratación de DevRev."],
  ["DevRev ticket context", "Contexto del ticket en DevRev"],
  ["Summary", "Resumen"],
  ["Stage", "Etapa"],
  ["State", "Estado"],
  ["Reported severity", "Severidad reportada"],
  ["Source channel", "Canal de origen"],
  ["Subtype", "Subtipo"],
  ["Owner", "Responsable"],
  ["Reporter", "Reportante"],
  ["Created", "Creado"],
  ["Updated", "Actualizado"],
  ["Review record", "Registro de revisión"],
  ["No summary is available", "No hay un resumen disponible"],
  ["Unassigned upstream", "Sin asignar en el origen"],
  ["Linked review unavailable", "Revisión vinculada no disponible"],
  ["Copy execution ID", "Copiar ID de ejecución"],
  ["Open in the ticket system", "Abrir en el sistema de tickets"],
  ["No verified upstream link is configured, so the identifier is offered for copying instead of a guessed address.", "No hay un enlace de origen verificado configurado; se ofrece copiar el identificador en vez de usar una dirección supuesta."],
  ["Add to remediation batch", "Agregar al lote de remediación"],
  ["Adds this review to a new remediation batch, frozen at the version on screen. The control is disabled when this deployment reports remediation as unavailable, or when your role may not curate a batch.", "Agrega esta revisión a un nuevo lote de remediación fijado en la versión visible. El control se desactiva cuando la remediación no está disponible o tu rol no puede administrar lotes."],
  ["Reload execution", "Recargar ejecución"],
  ["Loading this RAG execution…", "Cargando esta ejecución RAG…"],
  ["That RAG execution is not available or is outside your scope.", "Esa ejecución RAG no está disponible o queda fuera de tu alcance."],
  ["Your role does not allow reading this execution.", "Tu rol no permite consultar esta ejecución."],
  ["This RAG execution could not be loaded.", "No se pudo cargar esta ejecución RAG."],
  ["The linked review is temporarily unavailable; the execution is still auditable.", "La revisión vinculada no está disponible temporalmente; la ejecución aún se puede auditar."],

  // Workspace tabs and conversation.
  ["Ticket workspace", "Espacio de trabajo del ticket"],
  ["Conversation", "Conversación"],
  ["Read-only conversation", "Conversación de solo lectura"],
  ["RAG evidence", "Evidencia RAG"],
  ["Review history", "Historial de revisión"],
  ["Remediation", "Remediación"],
  ["Show", "Mostrar"],
  ["All", "Todo"],
  ["Messages", "Mensajes"],
  ["Participant-facing", "Visible para participantes"],
  ["Internal", "Interno"],
  ["AI or system", "IA o sistema"],
  ["Human agent", "Agente humano"],
  ["Events", "Eventos"],
  ["Unclassified", "Sin clasificar"],
  ["Participant", "Participante"],
  ["Ticket event", "Evento del ticket"],
  ["Unclassified author", "Autor sin clasificar"],
  ["Public", "Público"],
  ["Participant-visible", "Visible para participantes"],
  ["Private", "Privado"],
  ["Internal · not shown to participant", "Interno · no visible para el participante"],
  ["Not shown to the participant", "No visible para el participante"],
  ["Participant saw this", "El participante vio esto"],
  ["Ticket system", "Sistema de tickets"],
  ["Author not recorded", "Autor no registrado"],
  ["Show less", "Mostrar menos"],
  ["Show the whole message", "Mostrar el mensaje completo"],
  ["Replying to an earlier message", "En respuesta a un mensaje anterior"],
  ["In reply to entry", "En respuesta a la entrada"],
  ["Upstream entry type:", "Tipo de entrada de origen:"],
  ["Entry", "Entrada"],
  ["Entry unknown", "Entrada desconocida"],
  ["Technical details", "Detalles técnicos"],
  ["A change was recorded with no summary.", "Se registró un cambio sin resumen."],
  ["No body is shown here.", "No se muestra contenido aquí."],
  ["No body is shown here because the upstream body was not plain text.", "Aquí no se muestra contenido porque el contenido original no era texto sin formato."],
  ["No conversation entries have loaded for this ticket.", "No se han cargado entradas de conversación para este ticket."],
  ["No messages have loaded for this ticket.", "No se han cargado mensajes para este ticket."],
  ["No entries on the pages loaded so far match this filter.", "Ninguna entrada de las páginas cargadas coincide con este filtro."],
  ["A later page may contain some; the filter applies to what is loaded.", "Una página posterior podría contener resultados; el filtro se aplica a lo ya cargado."],
  ["Loading the conversation…", "Cargando la conversación…"],
  ["The conversation could not be loaded.", "No se pudo cargar la conversación."],
  ["Showing the pages already loaded; the newest request failed.", "Se muestran las páginas ya cargadas; la solicitud más reciente falló."],
  ["Loading another page…", "Cargando otra página…"],
  ["Load more conversation", "Cargar más conversación"],
  ["The upstream conversation pages forward only, so earlier pages stay above.", "La conversación de origen pagina solo hacia adelante, por lo que las páginas anteriores permanecen arriba."],
  ["Every entry the ticket system returned is loaded.", "Se cargaron todas las entradas devueltas por el sistema de tickets."],

  // Evidence and history.
  ["Retrieval and prompt evidence", "Evidencia de recuperación y prompt"],
  ["Persisted execution evidence", "Evidencia persistida de la ejecución"],
  ["Correlation and candidate evidence", "Evidencia de correlación y candidatos"],
  ["Confirmed evidence links", "Enlaces de evidencia confirmados"],
  ["Load more evidence links", "Cargar más enlaces de evidencia"],
  ["Source articles captured by this execution", "Artículos fuente capturados por esta ejecución"],
  ["Bounded chunks captured by this execution", "Fragmentos acotados capturados por esta ejecución"],
  ["This bounded evidence was stored with the RAG run; reviewer-confirmed links are listed separately below.", "Esta evidencia acotada se guardó con la ejecución RAG; los enlaces confirmados por revisores aparecen por separado abajo."],
  ["This bounded evidence was stored with the RAG run; correlation and candidate records are shown separately below.", "Esta evidencia acotada se guardó con la ejecución RAG; los registros de correlación y candidatos aparecen por separado abajo."],
  ["Sources attached to the persisted answer. Candidate retrieval records remain in the correlation section below, so final sources and candidates are not conflated.", "Fuentes adjuntas a la respuesta persistida. Los registros candidatos de recuperación permanecen en la sección de correlación de abajo para no confundirlos con las fuentes finales."],
  ["Article id", "ID del artículo"],
  ["Article title", "Título del artículo"],
  ["Source title", "Título de la fuente"],
  ["Recorded URL", "URL registrada"],
  ["Chunk types used", "Tipos de fragmento utilizados"],
  ["Used in answer", "Utilizado en la respuesta"],
  ["Maximum score", "Puntuación máxima"],
  ["Source id", "ID de fuente"],
  ["Chunk id", "ID de fragmento"],
  ["Chunk type", "Tipo de fragmento"],
  ["Chunk tier", "Nivel del fragmento"],
  ["Bounded preview", "Vista previa acotada"],
  ["Bounded chunks", "Fragmentos acotados"],
  ["Content hash", "Hash del contenido"],
  ["Observed vector id", "ID del vector observado"],
  ["Chunk ordinal", "Posición del fragmento"],
  ["Vectors observed at query time", "Vectores observados al consultar"],
  ["Executions found for this ticket", "Ejecuciones encontradas para este ticket"],
  ["Suggested correlations awaiting confirmation", "Correlaciones sugeridas pendientes de confirmación"],
  ["Source record", "Registro de origen"],
  ["Record schema", "Esquema del registro"],
  ["Occurred at", "Ocurrió el"],
  ["Endpoint", "Punto de conexión"],
  ["Correlation source", "Origen de la correlación"],
  ["Correlation trust", "Confianza de la correlación"],
  ["Internal ticket job", "Trabajo interno del ticket"],
  ["Request id hash", "Hash del ID de solicitud"],
  ["Evidence reference", "Referencia de evidencia"],
  ["Evidence digest", "Resumen criptográfico de evidencia"],
  ["Provider", "Proveedor"],
  ["Prompt template id", "ID de plantilla de prompt"],
  ["Prompt template hash", "Hash de plantilla de prompt"],
  ["Prompt configuration version", "Versión de configuración del prompt"],
  ["Rendered prompt trace hash", "Hash de trazabilidad del prompt renderizado"],
  ["Response hash", "Hash de respuesta"],
  ["Index", "Índice"],
  ["Index version", "Versión del índice"],
  ["Deployed revision", "Revisión desplegada"],
  ["Deployed commit", "Commit desplegado"],
  ["Deployed image digest", "Resumen de imagen desplegada"],
  ["Lookup result digest", "Resumen del resultado de consulta"],
  ["Key versions queried", "Versiones de clave consultadas"],
  ["Lookup truncated", "Consulta truncada"],
  ["Why it was suggested", "Motivo de la sugerencia"],
  ["Trust", "Confianza"],
  ["Suggestion expires", "La sugerencia vence"],
  ["Reason given", "Motivo indicado"],
  ["Linked by", "Vinculado por"],
  ["Link version", "Versión del enlace"],
  ["Linked at", "Vinculado el"],
  ["Confirmed links", "Enlaces confirmados"],
  ["Evidence service", "Servicio de evidencia"],
  ["Execution log", "Registro de ejecución"],
  ["Ticket execution", "Ejecución del ticket"],
  ["Ticket job", "Trabajo del ticket"],
  ["No correlation", "Sin correlación"],
  ["Suggested, unconfirmed", "Sugerida, sin confirmar"],
  ["Verified by the producing workload", "Verificada por la carga de trabajo productora"],
  ["Confirmed by a reviewer", "Confirmada por un revisor"],
  ["Answered", "Respondió"],
  ["Did not answer", "No respondió"],
  ["0 (pre-provenance legacy shape)", "0 (formato heredado anterior a procedencia)"],
  ["This execution failed", "Esta ejecución falló"],
  ["Suggested, not linked", "Sugerida, sin vincular"],
  ["No observed vectors were recorded for this execution. That is a gap in what was logged, not proof that nothing was retrieved.", "No se registraron vectores observados para esta ejecución. Es un vacío en lo registrado, no una prueba de que no se recuperó nada."],
  ["These identifiers are what the retrieval step returned at the time. They are not stable across a reindex, so treat them as a trace of that one query rather than as addresses to look up later.", "Estos identificadores son los que devolvió el paso de recuperación en ese momento. No son estables después de una reindexación; considéralos una traza de esa consulta, no direcciones para consultar más adelante."],
  ["The rendered prompt hash identifies this one execution's prompt text. The template id and template hash are what identify the version of the prompt; the trace hash changes whenever the inputs do.", "El hash del prompt renderizado identifica el texto del prompt de esta ejecución. El ID y el hash de la plantilla identifican la versión del prompt; el hash de trazabilidad cambia cuando cambian las entradas."],
  ["Unknown — this execution did not record an index version", "Desconocida — esta ejecución no registró una versión del índice"],
  ["No source article identifiers were recorded for this execution.", "No se registraron identificadores de artículos fuente para esta ejecución."],
  ["Article identifiers only. Article text is not read through this console, so what is shown here cannot drift from what was indexed.", "Solo se muestran identificadores de artículos. El texto de los artículos no se consulta desde esta consola, así que lo mostrado aquí no puede diferir de lo indexado."],
  ["These were not produced by the workload that answered the ticket, so the console will not treat them as evidence until a reviewer says they belong and why.", "Estos registros no fueron producidos por la carga de trabajo que respondió el ticket, por lo que la consola no los tratará como evidencia hasta que un revisor indique que corresponden y por qué."],
  ["This suggestion has expired. Reload the ticket to get a current one.", "Esta sugerencia venció. Recarga el ticket para obtener una vigente."],
  ["Why this evidence belongs to this ticket", "Por qué esta evidencia corresponde a este ticket"],
  ["Required. It is written to the audit ledger and cannot be edited later.", "Obligatorio. Se registra en el libro de auditoría y no se puede editar después."],
  ["Confirm this link", "Confirmar este enlace"],
  ["Why this link is wrong", "Por qué este enlace es incorrecto"],
  ["Required. An unexplained unlink is indistinguishable from tampering when the record is read back years later.", "Obligatorio. Un enlace desvinculado sin explicación no se puede distinguir de una manipulación cuando el registro se consulte años después."],
  ["Unlink", "Desvincular"],
  ["Recorded reason", "Motivo registrado"],
  ["Loading evidence…", "Cargando evidencia…"],
  ["Evidence has not been loaded yet.", "La evidencia aún no se ha cargado."],
  ["Evidence could not be loaded.", "No se pudo cargar la evidencia."],
  ["No defensible correlation for this ticket.", "No hay una correlación justificable para este ticket."],
  ["Some evidence may be incomplete.", "Parte de la evidencia puede estar incompleta."],
  ["Linked", "Vinculado"],
  ["Manually linked", "Vinculado manualmente"],
  ["Loading evidence links…", "Cargando enlaces de evidencia…"],
  ["Evidence links could not be loaded.", "No se pudieron cargar los enlaces de evidencia."],
  ["Showing the links already loaded; the newest request failed.", "Se muestran los enlaces ya cargados; la solicitud más reciente falló."],
  ["A ticket has evidence links only once it has a durable review.", "Un ticket tiene enlaces de evidencia solo cuando cuenta con una revisión durable."],
  ["Every evidence link is loaded.", "Se cargaron todos los enlaces de evidencia."],
  ["No retrieval or prompt provenance is available for this ticket.", "No hay procedencia de recuperación o prompt disponible para este ticket."],
  ["No reviewer has confirmed an evidence link for this ticket.", "Ningún revisor ha confirmado un enlace de evidencia para este ticket."],
  ["This ticket predates reliable ticket-to-RAG correlation, or its legacy execution did not include a ticket-system identifier. The conversation is available; retrieval and prompt provenance cannot be reconstructed reliably.", "Este ticket es anterior a una correlación confiable entre ticket y RAG, o su ejecución heredada no incluyó un identificador del sistema de tickets. La conversación está disponible; la procedencia de recuperación y prompt no puede reconstruirse de forma confiable."],
  ["The evidence service is not configured for this deployment, so no retrieval or prompt provenance can be read. Reviews and conversation are unaffected.", "El servicio de evidencia no está configurado para este despliegue, por lo que no se puede consultar la procedencia de recuperación o prompt. Las revisiones y la conversación no se ven afectadas."],
  ["The evidence service did not answer. This is a gap in the lookup, not proof that the ticket has no retrieval history.", "El servicio de evidencia no respondió. Es una brecha en la consulta, no una prueba de que el ticket carezca de historial de recuperación."],
  ["Review history could not be loaded.", "No se pudo cargar el historial de revisión."],
  ["A ticket has review history only once it has a durable review.", "Un ticket tiene historial de revisión solo cuando cuenta con una revisión durable."],
  ["Loading review history…", "Cargando el historial de revisión…"],
  ["No audit events have loaded for this review.", "No se han cargado eventos de auditoría para esta revisión."],
  ["Load more review history", "Cargar más historial de revisión"],
  ["The events on this page link to one another as an unbroken chain. The hashes themselves are verified where they are written.", "Los eventos de esta página forman una cadena continua. Los hashes se verifican al escribirse."],
  ["The audit ledger is returned as a single bounded page, so there is no further page to fetch. Reload the ticket to see events recorded since.", "El registro de auditoría se devuelve en una única página acotada. Recarga el ticket para ver los eventos registrados después."],
  ["Review created", "Revisión creada"],
  ["Review updated", "Revisión actualizada"],
  ["Review linked by the ticket system", "Revisión vinculada por el sistema de tickets"],
  ["Evidence linked", "Evidencia vinculada"],
  ["Evidence unlinked", "Evidencia desvinculada"],
  ["Review linkage reversed", "Vínculo de revisión revertido"],
  ["Legal hold set", "Retención legal activada"],
  ["Legal hold cleared", "Retención legal eliminada"],

  // Evaluation form and edit conflict.
  ["Evaluation", "Evaluación"],
  ["Rate the final answer and document the correction, if any.", "Califica la respuesta final y documenta la corrección, si corresponde."],
  ["Advanced review fields", "Campos avanzados de revisión"],
  ["Topic", "Tema"],
  ["Legacy Type", "Tipo histórico"],
  ["Historical classification retained for continuity. It is not the observation type above, and one is never derived from the other.", "Clasificación histórica conservada por continuidad. No corresponde al tipo de observación anterior; ninguno se deriva del otro."],
  ["Observation type", "Tipo de observación"],
  ["The root-cause taxonomy this console adds. Distinct from Legacy Type.", "Taxonomía de causa raíz que agrega esta consola, distinta del tipo histórico."],
  ["Unsafe or incorrect", "Insegura o incorrecta"],
  ["Major correction required", "Requiere una corrección importante"],
  ["Partially useful", "Parcialmente útil"],
  ["Correct with minor improvement", "Correcta con una mejora menor"],
  ["Correct, complete, and appropriately scoped", "Correcta, completa y con alcance adecuado"],
  ["Clear rating", "Limpiar calificación"],
  ["Assigned reviewer", "Revisor asignado"],
  ["Leave unchanged", "Dejar sin cambios"],
  ["Assign to the signed-in reviewer", "Asignar al revisor con sesión iniciada"],
  ["Comments", "Comentarios"],
  ["Expected behavior", "Comportamiento esperado"],
  ["What the assistant should have answered, and why.", "Lo que el asistente debió responder y por qué."],
  ["Severity", "Severidad"],
  ["Remediation target", "Objetivo de remediación"],
  ["Status", "Estado"],
  ["Closing this review", "Cierre de esta revisión"],
  ["A closing status is refused without a defensible record of how the outcome was verified.", "No se acepta un estado de cierre sin un registro defendible de cómo se verificó el resultado."],
  ["Outcome", "Resultado"],
  ["Verification summary", "Resumen de verificación"],
  ["Verification rationale", "Justificación de verificación"],
  ["Machine-checked test evidence is attached by the remediation agent, not typed here, so a review closed from this screen has to say in words why the outcome is defensible.", "La evidencia de pruebas verificada automáticamente la adjunta el agente de remediación; no se escribe aquí. Por eso, al cerrar una revisión desde esta pantalla debes explicar con palabras por qué el resultado es defendible."],
  ["Branch", "Rama"],
  ["Commit", "Commit"],
  ["Save review", "Guardar revisión"],
  ["Saving…", "Guardando…"],
  ["Discard my changes", "Descartar mis cambios"],
  ["Unsaved changes", "Cambios sin guardar"],
  ["Someone else saved this review first", "Otra persona guardó esta revisión primero"],
  ["Your unsaved values", "Tus valores sin guardar"],
  ["Currently saved on the server", "Guardado actualmente en el servidor"],
  ["Discard mine and reload the server version", "Descartar mis cambios y recargar la versión del servidor"],
  ["Keep editing mine", "Seguir editando mis cambios"],
  ["Reapply my values over the server version", "Volver a aplicar mis valores sobre la versión del servidor"],
  ["Administrators only. This re-sends your values against the version you are being shown, so the other reviewer's change is replaced rather than merged. It is never sent automatically.", "Solo administradores. Vuelve a enviar tus valores contra la versión mostrada, reemplazando el cambio de la otra persona en vez de combinarlo. Nunca se envía automáticamente."],
  ["This ticket has no durable review yet. Saving creates one and then applies your evaluation to it.", "Este ticket aún no tiene una revisión durable. Al guardar se crea una y después se aplica tu evaluación."],
  ["Your role can read this review but not change it. Ask an administrator for the reviewer role.", "Tu rol puede leer esta revisión, pero no modificarla. Solicita a un administrador el rol de revisor."],
  ["Your role cannot change this review.", "Tu rol no puede modificar esta revisión."],
  ["Unassigned.", "Sin asignar."],
  ["Assigned to you.", "Asignada a ti."],
  ["Your role cannot change the assignment.", "Tu rol no puede cambiar la asignación."],
  ["This review has unsaved changes. Leaving now discards them. Continue?", "Esta revisión tiene cambios sin guardar. Si sales ahora, se descartarán. ¿Continuar?"],
  ["Discard your unsaved changes and restore the saved review?", "¿Descartar tus cambios sin guardar y restaurar la revisión guardada?"],
  ["Unlink this evidence from the review? The reason will remain in the audit ledger.", "¿Desvincular esta evidencia de la revisión? El motivo permanecerá en el registro de auditoría."],
  ["The linked review is unavailable", "La revisión vinculada no está disponible"],
  ["This execution stays readable, but reviewer fields cannot be changed until its review record is available.", "Esta ejecución sigue disponible para lectura, pero los campos de revisión no se pueden cambiar hasta que su registro esté disponible."],
  ["Nothing to save", "No hay cambios que guardar"],
  ["No field differs from the stored review.", "Ningún campo difiere de la revisión guardada."],
  ["Review saved", "Revisión guardada"],
  ["A reason is required", "Se requiere un motivo"],
  ["Say why this evidence belongs to this ticket; it is written to the audit ledger.", "Indica por qué esta evidencia pertenece al ticket; el motivo se registra en el historial de auditoría."],
  ["The link and its reason are in the ledger.", "El enlace y su motivo están en el registro."],
  ["An unexplained unlink cannot be told apart from tampering later.", "Más adelante, desvincular sin explicación no se podría distinguir de una alteración."],
  ["Execution ID copied", "ID de ejecución copiado"],
  ["Could not copy automatically", "No se pudo copiar automáticamente"],

  // Remediation workspace.
  ["Remediation and resolution", "Remediación y resolución"],
  ["Remediation batch", "Lote de remediación"],
  ["Reason or attestation for the batch decision", "Motivo o constancia de la decisión del lote"],
  ["Codex prompt, for manual copying", "Prompt de Codex para copiar manualmente"],
  ["Not closed", "Sin cerrar"],
  ["Resolution", "Resolución"],
  ["Machine-checked verification", "Verificación automática"],
  ["Remediation batches", "Lotes de remediación"],
  ["Batch", "Lote"],
  ["Frozen observations", "Observaciones fijadas"],
  ["Version", "Versión"],
  ["Created by", "Creado por"],
  ["Claimed by", "Reclamado por"],
  ["Prompt template", "Plantilla de prompt"],
  ["Claim", "Reclamación"],
  ["Holder", "Titular"],
  ["Lease expires", "Vencimiento del arrendamiento"],
  ["Last heartbeat", "Último pulso"],
  ["Continuous since", "Continuo desde"],
  ["Proposed change", "Cambio propuesto"],
  ["Plan", "Plan"],
  ["Uncommitted because", "Sin commit porque"],
  ["Change request", "Solicitud de cambio"],
  ["What the agent ran", "Lo que ejecutó el agente"],
  ["What the verifier ran", "Lo que ejecutó el verificador"],
  ["Verifier attestation", "Constancia del verificador"],
  ["Verified by", "Verificado por"],
  ["Decision", "Decisión"],
  ["Reason", "Motivo"],
  ["Copy Codex prompt", "Copiar prompt de Codex"],
  ["No durable review, so nothing is planned or resolved yet.", "No hay una revisión durable, así que todavía no hay nada planificado ni resuelto."],
  ["The server reports batches as disabled in this deployment, so a review cannot be added to one from here.", "El servidor informa que los lotes están deshabilitados en este despliegue, por lo que una revisión no se puede agregar a un lote desde aquí."],
  ["No remediation batch is selected. Choose reviews in the queue and create one to hand a group of observations to the agent.", "No hay ningún lote de remediación seleccionado. Elige revisiones en la cola y crea uno para entregar al agente un grupo de observaciones."],
  ["Required for every batch decision except “Mark ready”, and recorded in the append-only audit ledger. For “Start verification” this is your attestation that you did not author the change.", "Se requiere para cada decisión del lote excepto “Marcar como listo” y se registra en el registro de auditoría inmutable. Para “Iniciar verificación”, es tu constancia de que no creaste el cambio."],
  ["Shown only when the browser refuses clipboard access. The prompt is not stored by this console; it is fetched, handed over, and dropped.", "Se muestra solo cuando el navegador rechaza el acceso al portapapeles. Esta consola no almacena el prompt: se obtiene, se entrega y se descarta."],
]);

const SPANISH_TO_ENGLISH = new Map(
  Array.from(ENGLISH_TO_SPANISH, ([english, spanish]) => [spanish, english])
);

const MISSING_PROVENANCE_TO_SPANISH = new Map([
  ["index version", "versión del índice"],
  ["deployed revision", "revisión desplegada"],
  ["prompt template", "plantilla de prompt"],
  ["model", "modelo"],
  ["observed vectors", "vectores observados"],
  ["response hash", "hash de respuesta"],
  ["source articles", "artículos fuente"],
  ["a pre-Stage-4 record shape", "un formato de registro anterior a la Etapa 4"],
]);

const MISSING_PROVENANCE_TO_ENGLISH = new Map(
  Array.from(MISSING_PROVENANCE_TO_SPANISH, ([english, spanish]) => [spanish, english])
);

const CLOSING_REQUIREMENT_COMPONENTS = [
  ["an outcome", "un resultado"],
  ["a verification summary", "un resumen de verificación"],
  [
    "a verification rationale, since no machine-checked test evidence is attached",
    "una justificación de verificación, ya que no se adjuntó evidencia de pruebas verificada automáticamente",
  ],
];

const PROTECTED_CONTENT = [
  "script",
  "style",
  "textarea",
  "pre",
  "code",
  "time",
  "[contenteditable='true']",
  "[data-ticket-content]",
  "[data-user-content]",
  "[data-audit-value]",
  "[data-field-key]",
  ".entry-paragraph",
  ".cell-id-value",
  ".cell-title:not(.cell-attempt)",
  ".cell-comments",
  ".cell-reviewer > span:not(.cell-empty):not(.cell-legacy-tag)",
  "[data-row='ticket'] td[data-label='Route']",
  "#detail-target",
  "#detail-crumb",
  "#session-email",
].join(",");

const TRANSLATABLE_ATTRIBUTES = ["aria-label", "title", "placeholder", "data-label"];
const textHistory = new WeakMap();
const attributeHistory = new WeakMap();
let activePreferences = null;

function normalizeLanguage(value) {
  const locale = String(value ?? "").toLowerCase();
  return locale === "es" || locale.startsWith("es-") || locale.startsWith("es_")
    ? "es"
    : "en";
}

function splitSpacing(value) {
  const text = String(value ?? "");
  const leading = text.match(/^\s*/)?.[0] ?? "";
  const trailing = text.match(/\s*$/)?.[0] ?? "";
  const core = text.trim().replace(/\s+/g, " ");
  return { leading, core, trailing };
}

function normalizeCountNumber(value, language) {
  const digits = String(value ?? "").replace(/\D/g, "");
  if (digits === "") return String(value ?? "");
  return Number(digits).toLocaleString(language);
}

function translateMissingProvenanceList(value, labels) {
  return String(value ?? "")
    .split(", ")
    .map((name) => labels.get(name) ?? name)
    .join(", ");
}

function translateClosingRequirements(value, sourceIndex, targetIndex) {
  let remaining = String(value ?? "");
  const translated = [];
  for (const component of CLOSING_REQUIREMENT_COMPONENTS) {
    const source = component[sourceIndex];
    if (remaining === source) {
      translated.push(component[targetIndex]);
      remaining = "";
      break;
    }
    if (remaining.startsWith(`${source}, `)) {
      translated.push(component[targetIndex]);
      remaining = remaining.slice(source.length + 2);
    }
  }
  return remaining === "" && translated.length > 0 ? translated.join(", ") : null;
}

function spanishUiLabel(value) {
  return ENGLISH_TO_SPANISH.get(value) ?? spanishPattern(value);
}

function englishUiLabel(value) {
  return SPANISH_TO_ENGLISH.get(value) ?? englishPattern(value);
}

function spanishPattern(core) {
  let match = core.match(/^(\d+) on this page$/);
  if (match !== null) return `${match[1]} en esta página`;

  match = core.match(/^Remove the (.+) filter$/);
  if (match !== null) return `Quitar el filtro ${spanishUiLabel(match[1])}`;

  match = core.match(/^(\d+) fields?$/);
  if (match !== null) {
    return `${match[1]} campo${match[1] === "1" ? "" : "s"}`;
  }

  match = core.match(/^(\d+) items?$/);
  if (match !== null) {
    return `${match[1]} elemento${match[1] === "1" ? "" : "s"}`;
  }

  match = core.match(/^(\d+) source articles?$/);
  if (match !== null) {
    return `${match[1]} artículo${match[1] === "1" ? "" : "s"} fuente`;
  }

  match = core.match(/^(\d+) bounded chunks?$/);
  if (match !== null) {
    return `${match[1]} fragmento${match[1] === "1" ? "" : "s"} acotado${match[1] === "1" ? "" : "s"}`;
  }

  match = core.match(/^(\d+) observed vectors?$/);
  if (match !== null) {
    return `${match[1]} vector${match[1] === "1" ? "" : "es"} observado${match[1] === "1" ? "" : "s"}`;
  }

  match = core.match(/^(\d+) executions?$/);
  if (match !== null) {
    return `${match[1]} ejecución${match[1] === "1" ? "" : "es"}`;
  }

  match = core.match(/^([\d,.\s]+) warnings?$/);
  if (match !== null) {
    const count = normalizeCountNumber(match[1], "es");
    const singular = String(match[1]).replace(/\D/g, "") === "1";
    return `${count} advertencia${singular ? "" : "s"}`;
  }

  match = core.match(/^([\d,.\s]+) retrieval records?$/);
  if (match !== null) {
    const count = normalizeCountNumber(match[1], "es");
    const singular = String(match[1]).replace(/\D/g, "") === "1";
    return `${count} registro${singular ? "" : "s"} de recuperación`;
  }

  match = core.match(/^(\d+) suggestions?$/);
  if (match !== null) {
    return `${match[1]} sugerencia${match[1] === "1" ? "" : "s"}`;
  }

  match = core.match(/^Show (\d+) fields?$/);
  if (match !== null) {
    return `Mostrar ${match[1]} campo${match[1] === "1" ? "" : "s"}`;
  }

  match = core.match(/^Show (\d+) items?$/);
  if (match !== null) {
    return `Mostrar ${match[1]} elemento${match[1] === "1" ? "" : "s"}`;
  }

  match = core.match(/^Item (\d+)$/);
  if (match !== null) return `Elemento ${match[1]}`;

  match = core.match(/^Step (\d+)$/);
  if (match !== null) return `Paso ${match[1]}`;

  match = core.match(/^Retrieval record (\d+)$/);
  if (match !== null) return `Registro de recuperación ${match[1]}`;

  match = core.match(/^Suggestion (\d+)$/);
  if (match !== null) return `Sugerencia ${match[1]}`;

  match = core.match(/^(.+) — show ([\d,.\s]+) (items?|fields?)$/);
  if (match !== null) {
    const label = spanishUiLabel(match[1]);
    const count = normalizeCountNumber(match[2], "es");
    const singular = String(match[2]).replace(/\D/g, "") === "1";
    const noun = match[3].startsWith("item")
      ? `elemento${singular ? "" : "s"}`
      : `campo${singular ? "" : "s"}`;
    return `${label} — mostrar ${count} ${noun}`;
  }

  match = core.match(/^Continue into (.+) — ([\d,.\s]+) (items?|fields?)$/);
  if (match !== null) {
    const label = spanishUiLabel(match[1]);
    const count = normalizeCountNumber(match[2], "es");
    const singular = String(match[2]).replace(/\D/g, "") === "1";
    const noun = match[3].startsWith("item")
      ? `elemento${singular ? "" : "s"}`
      : `campo${singular ? "" : "s"}`;
    return `Continuar en ${label} — ${count} ${noun}`;
  }

  match = core.match(/^(Items|Fields) ([\d,.\s]+)–([\d,.\s]+) of ([\d,.\s]+) in (.+)$/);
  if (match !== null) {
    const kind = match[1] === "Items" ? "Elementos" : "Campos";
    const start = normalizeCountNumber(match[2], "es");
    const end = normalizeCountNumber(match[3], "es");
    const total = normalizeCountNumber(match[4], "es");
    return `${kind} ${start}–${end} de ${total} en ${spanishUiLabel(match[5])}`;
  }

  match = core.match(/^(.+) — ([\d,.\s]+) (items?|fields?)$/);
  if (match !== null) {
    const label = spanishUiLabel(match[1]);
    const count = normalizeCountNumber(match[2], "es");
    const singular = String(match[2]).replace(/\D/g, "") === "1";
    const noun = match[3].startsWith("item")
      ? `elemento${singular ? "" : "s"}`
      : `campo${singular ? "" : "s"}`;
    return `${label} — ${count} ${noun}`;
  }

  match = core.match(/^This execution did not pass the authorized DevRev hydration boundary\. Recorded reason: (.+)\.$/);
  if (match !== null) {
    return `Esta ejecución no superó el límite autorizado de hidratación de DevRev. Motivo registrado: ${match[1]}.`;
  }

  match = core.match(/^Not recorded for this execution: (.+)\.$/);
  if (match !== null) {
    const missing = translateMissingProvenanceList(
      match[1],
      MISSING_PROVENANCE_TO_SPANISH
    );
    return `No se registró para esta ejecución: ${missing}.`;
  }

  match = core.match(/^No retrieval or prompt provenance is available\. Reported reason: (.+)\.$/);
  if (match !== null) {
    return `No hay procedencia de recuperación o prompt disponible. Motivo reportado: ${match[1]}.`;
  }

  match = core.match(/^(Linked|Manually linked|Unavailable): (\d+) executions? available\.(?: (Some evidence may be incomplete\.))?$/);
  if (match !== null) {
    const status = ENGLISH_TO_SPANISH.get(match[1]) ?? match[1];
    const warning = match[3] ? ` ${ENGLISH_TO_SPANISH.get(match[3]) ?? match[3]}` : "";
    return `${status}: ${match[2]} ejecución${match[2] === "1" ? "" : "es"} disponible${match[2] === "1" ? "" : "s"}.${warning}`;
  }

  match = core.match(/^This persisted RAG execution recorded (\d+) source articles? and (\d+) bounded chunks?\.(?: (.+))?$/);
  if (match !== null) {
    const sources = `${match[1]} artículo${match[1] === "1" ? "" : "s"} fuente`;
    const chunks = `${match[2]} fragmento${match[2] === "1" ? "" : "s"} acotado${match[2] === "1" ? "" : "s"}`;
    const trailing = match[3] ? ` ${translateUiText(match[3], "es")}` : "";
    return `Esta ejecución RAG persistida registró ${sources} y ${chunks}.${trailing}`;
  }

  match = core.match(/^Page (\d+)$/);
  if (match !== null) return `Página ${match[1]}`;

  match = core.match(/^(\d+) executions? selected$/);
  if (match !== null) {
    return `${match[1]} ejecución${match[1] === "1" ? "" : "es"} seleccionada${match[1] === "1" ? "" : "s"}`;
  }

  match = core.match(/^(\d+) rows? on this page\.$/);
  if (match !== null) return `${match[1]} fila${match[1] === "1" ? "" : "s"} en esta página.`;

  match = core.match(/^(\d+) reviews? will be frozen at the version shown\.$/);
  if (match !== null) {
    return `${match[1]} revisión${match[1] === "1" ? "" : "es"} quedará${match[1] === "1" ? "" : "n"} fijada${match[1] === "1" ? "" : "s"} en la versión mostrada.`;
  }

  match = core.match(/^Currently (.+)\. Only the moves the review lifecycle allows are offered\.$/);
  if (match !== null) {
    const status = ENGLISH_TO_SPANISH.get(match[1]) ?? match[1];
    return `Estado actual: ${status}. Solo se ofrecen los cambios permitidos por el ciclo de revisión.`;
  }

  match = core.match(/^Currently (.+)\. Closing it needs a documented, defensible outcome\.$/);
  if (match !== null) {
    const status = ENGLISH_TO_SPANISH.get(match[1]) ?? match[1];
    return `Estado actual: ${status}. Para cerrarla se necesita un resultado documentado y justificable.`;
  }

  match = core.match(/^Closing this review needs (.+)\.$/);
  if (match !== null) {
    const requirements = translateClosingRequirements(match[1], 0, 1);
    if (requirements !== null) {
      return `Para cerrar esta revisión se requiere ${requirements}.`;
    }
  }

  match = core.match(/^Unassigned\. You may take an unassigned review or release your own\. Reassigning someone else's is an administrator action\.$/);
  if (match !== null) {
    return "Sin asignar. Puedes tomar una revisión sin asignar o liberar una asignada a ti. Reasignar la revisión de otra persona requiere un administrador.";
  }

  match = core.match(/^Assigned to you\. You may take an unassigned review or release your own\. Reassigning someone else's is an administrator action\.$/);
  if (match !== null) {
    return "Asignada a ti. Puedes tomar una revisión sin asignar o liberar una asignada a ti. Reasignar la revisión de otra persona requiere un administrador.";
  }

  match = core.match(/^Assigned to (.+)\. You may take an unassigned review or release your own\. Reassigning someone else's is an administrator action\.$/);
  if (match !== null) {
    return `Asignada a ${match[1]}. Puedes tomar una revisión sin asignar o liberar una asignada a ti. Reasignar la revisión de otra persona requiere un administrador.`;
  }

  match = core.match(/^Unassigned\. As an administrator you may take it or clear it\. Handing it to a third person needs their verified sign-in identity, which no route publishes, so it is not offered here\.$/);
  if (match !== null) {
    return "Sin asignar. Como administrador, puedes tomarla o dejarla sin asignar. Entregarla a una tercera persona requiere su identidad verificada de inicio de sesión, que ninguna ruta publica, por lo que esa opción no se ofrece aquí.";
  }

  match = core.match(/^Assigned to you\. As an administrator you may take it or clear it\. Handing it to a third person needs their verified sign-in identity, which no route publishes, so it is not offered here\.$/);
  if (match !== null) {
    return "Asignada a ti. Como administrador, puedes tomarla o dejarla sin asignar. Entregarla a una tercera persona requiere su identidad verificada de inicio de sesión, que ninguna ruta publica, por lo que esa opción no se ofrece aquí.";
  }

  match = core.match(/^Assigned to (.+)\. As an administrator you may take it or clear it\. Handing it to a third person needs their verified sign-in identity, which no route publishes, so it is not offered here\.$/);
  if (match !== null) {
    return `Asignada a ${match[1]}. Como administrador, puedes tomarla o dejarla sin asignar. Entregarla a una tercera persona requiere su identidad verificada de inicio de sesión, que ninguna ruta publica, por lo que esa opción no se ofrece aquí.`;
  }

  match = core.match(/^The upstream body was (\d+) characters\.$/);
  if (match !== null) return `El contenido original tenía ${match[1]} caracteres.`;

  match = core.match(/^([\d,.\s]+) of ([\d,.\s]+) characters$/);
  if (match !== null) {
    return `${normalizeCountNumber(match[1], "es")} de ${normalizeCountNumber(match[2], "es")} caracteres`;
  }

  match = core.match(/^(\d+) of 5$/);
  if (match !== null) return `${match[1]} de 5`;

  match = core.match(/^Select execution (.+)$/);
  if (match !== null) return `Seleccionar ejecución ${match[1]}`;

  match = core.match(/^Select ticket (.+)$/);
  if (match !== null) return `Seleccionar ticket ${match[1]}`;

  match = core.match(/^Review ticket (.+)$/);
  if (match !== null) return `Evaluar ticket ${match[1]}`;

  match = core.match(/^Changes will be recorded as (.+)\.$/);
  if (match !== null) return `Los cambios se registrarán como ${match[1]}.`;

  match = core.match(/^Open RAG execution (.+)$/);
  if (match !== null) return `Abrir ejecución RAG ${match[1]}`;

  match = core.match(/^Open execution (.+)$/);
  if (match !== null) return `Abrir ejecución ${match[1]}`;

  match = core.match(/^Request id (.+)$/);
  if (match !== null) return `ID de solicitud ${match[1]}`;

  match = core.match(/^Attempt (\d+)(.*)$/);
  if (match !== null) {
    const suffix = match[2].replace(/ · inquiry (\d+)$/, " · consulta $1");
    return `Intento ${match[1]}${suffix}`;
  }

  match = core.match(/^Observed vector (\d+)$/);
  if (match !== null) return `Vector observado ${match[1]}`;

  match = core.match(/^Source article (\d+)$/);
  if (match !== null) return `Artículo fuente ${match[1]}`;

  match = core.match(/^Retrieved chunk (\d+)$/);
  if (match !== null) return `Fragmento recuperado ${match[1]}`;

  match = core.match(/^Execution (\d+)$/);
  if (match !== null) return `Ejecución ${match[1]}`;

  match = core.match(/^Verification (\d+)$/);
  if (match !== null) return `Verificación ${match[1]}`;

  match = core.match(/^Exited (\d+)$/);
  if (match !== null) return `Terminó con código ${match[1]}`;

  match = core.match(/^(\d+) links? loaded\.$/);
  if (match !== null) return `${match[1]} enlace${match[1] === "1" ? "" : "s"} cargado${match[1] === "1" ? "" : "s"}.`;

  match = core.match(/^(\d+) events?, from the first change recorded\.$/);
  if (match !== null) return `${match[1]} evento${match[1] === "1" ? "" : "s"}, desde el primer cambio registrado.`;

  match = core.match(/^(\d+) events?; earlier events are not on this page\.$/);
  if (match !== null) return `${match[1]} evento${match[1] === "1" ? "" : "s"}; los anteriores no están en esta página.`;

  match = core.match(
    /^(\d+) entr(?:y|ies) loaded; (more remain|that is the whole conversation)\. (\d+) shown by this filter\.$/
  );
  if (match !== null) {
    const loaded = `${match[1]} entrada${match[1] === "1" ? "" : "s"} cargada${match[1] === "1" ? "" : "s"}`;
    const completion = match[2] === "more remain"
      ? "quedan más"
      : "es toda la conversación";
    return `${loaded}; ${completion}. ${match[3]} mostrada${match[3] === "1" ? "" : "s"} por este filtro.`;
  }

  match = core.match(/^(\d+) entr(?:y|ies) loaded; more remain\.$/);
  if (match !== null) return `${match[1]} entrada${match[1] === "1" ? "" : "s"} cargada${match[1] === "1" ? "" : "s"}; quedan más.`;

  match = core.match(/^(\d+) entr(?:y|ies) loaded; that is the whole conversation\.$/);
  if (match !== null) return `${match[1]} entrada${match[1] === "1" ? "" : "s"} cargada${match[1] === "1" ? "" : "s"}; es toda la conversación.`;

  match = core.match(/^(\d+) shown by this filter\.$/);
  if (match !== null) return `${match[1]} mostrada${match[1] === "1" ? "" : "s"} por este filtro.`;

  match = core.match(/^(\d+) of (\d+) loaded entries shown$/);
  if (match !== null) return `${match[1]} de ${match[2]} entradas cargadas visibles`;

  match = core.match(/^(\d+) further observed vectors are not listed\.$/);
  if (match !== null) return `No se muestran ${match[1]} vectores observados adicionales.`;

  match = core.match(/^Requests are paused for (\d+) more seconds?\.$/);
  if (match !== null) return `Las solicitudes están pausadas por ${match[1]} segundo${match[1] === "1" ? "" : "s"} más.`;

  match = core.match(/^Now at version (\d+)\.$/);
  if (match !== null) return `Ahora en la versión ${match[1]}.`;

  match = core.match(/^Select and copy it from the page: (.+)$/);
  if (match !== null) return `Selecciónalo y cópialo desde la página: ${match[1]}`;

  return core;
}

function englishPattern(core) {
  let match = core.match(/^(\d+) en esta página$/);
  if (match !== null) return `${match[1]} on this page`;

  match = core.match(/^Quitar el filtro (.+)$/);
  if (match !== null) return `Remove the ${englishUiLabel(match[1])} filter`;

  match = core.match(
    /^(\d+) entradas? cargadas?; (quedan más|es toda la conversación)\. (\d+) mostradas? por este filtro\.$/
  );
  if (match !== null) {
    const loaded = `${match[1]} entr${match[1] === "1" ? "y" : "ies"} loaded`;
    const completion = match[2] === "quedan más"
      ? "more remain"
      : "that is the whole conversation";
    return `${loaded}; ${completion}. ${match[3]} shown by this filter.`;
  }

  match = core.match(/^(\d+) entradas? cargadas?; quedan más\.$/);
  if (match !== null) {
    return `${match[1]} entr${match[1] === "1" ? "y" : "ies"} loaded; more remain.`;
  }

  match = core.match(/^(\d+) entradas? cargadas?; es toda la conversación\.$/);
  if (match !== null) {
    return `${match[1]} entr${match[1] === "1" ? "y" : "ies"} loaded; that is the whole conversation.`;
  }

  match = core.match(/^(\d+) campos?$/);
  if (match !== null) {
    return `${match[1]} field${match[1] === "1" ? "" : "s"}`;
  }

  match = core.match(/^(\d+) elementos?$/);
  if (match !== null) {
    return `${match[1]} item${match[1] === "1" ? "" : "s"}`;
  }

  match = core.match(/^(\d+) artículos? fuente$/);
  if (match !== null) {
    return `${match[1]} source article${match[1] === "1" ? "" : "s"}`;
  }

  match = core.match(/^(\d+) fragmentos? acotados?$/);
  if (match !== null) {
    return `${match[1]} bounded chunk${match[1] === "1" ? "" : "s"}`;
  }

  match = core.match(/^(\d+) (?:vector|vectores) observados?$/);
  if (match !== null) {
    return `${match[1]} observed vector${match[1] === "1" ? "" : "s"}`;
  }

  match = core.match(/^(\d+) (?:ejecución|ejecuciones)$/);
  if (match !== null) {
    return `${match[1]} execution${match[1] === "1" ? "" : "s"}`;
  }

  match = core.match(/^([\d,.\s]+) advertencias?$/);
  if (match !== null) {
    const count = normalizeCountNumber(match[1], "en");
    const singular = String(match[1]).replace(/\D/g, "") === "1";
    return `${count} warning${singular ? "" : "s"}`;
  }

  match = core.match(/^([\d,.\s]+) registros? de recuperación$/);
  if (match !== null) {
    const count = normalizeCountNumber(match[1], "en");
    const singular = String(match[1]).replace(/\D/g, "") === "1";
    return `${count} retrieval record${singular ? "" : "s"}`;
  }

  match = core.match(/^(\d+) sugerencias?$/);
  if (match !== null) {
    return `${match[1]} suggestion${match[1] === "1" ? "" : "s"}`;
  }

  match = core.match(/^Mostrar (\d+) campos?$/);
  if (match !== null) {
    return `Show ${match[1]} field${match[1] === "1" ? "" : "s"}`;
  }

  match = core.match(/^Mostrar (\d+) elementos?$/);
  if (match !== null) {
    return `Show ${match[1]} item${match[1] === "1" ? "" : "s"}`;
  }

  match = core.match(/^Elemento (\d+)$/);
  if (match !== null) return `Item ${match[1]}`;

  match = core.match(/^Paso (\d+)$/);
  if (match !== null) return `Step ${match[1]}`;

  match = core.match(/^Registro de recuperación (\d+)$/);
  if (match !== null) return `Retrieval record ${match[1]}`;

  match = core.match(/^Sugerencia (\d+)$/);
  if (match !== null) return `Suggestion ${match[1]}`;

  match = core.match(/^(.+) — mostrar ([\d,.\s]+) (elementos?|campos?)$/);
  if (match !== null) {
    const label = englishUiLabel(match[1]);
    const count = normalizeCountNumber(match[2], "en");
    const singular = String(match[2]).replace(/\D/g, "") === "1";
    const noun = match[3].startsWith("elemento")
      ? `item${singular ? "" : "s"}`
      : `field${singular ? "" : "s"}`;
    return `${label} — show ${count} ${noun}`;
  }

  match = core.match(/^Continuar en (.+) — ([\d,.\s]+) (elementos?|campos?)$/);
  if (match !== null) {
    const label = englishUiLabel(match[1]);
    const count = normalizeCountNumber(match[2], "en");
    const singular = String(match[2]).replace(/\D/g, "") === "1";
    const noun = match[3].startsWith("elemento")
      ? `item${singular ? "" : "s"}`
      : `field${singular ? "" : "s"}`;
    return `Continue into ${label} — ${count} ${noun}`;
  }

  match = core.match(/^(Elementos|Campos) ([\d,.\s]+)–([\d,.\s]+) de ([\d,.\s]+) en (.+)$/);
  if (match !== null) {
    const kind = match[1] === "Elementos" ? "Items" : "Fields";
    const start = normalizeCountNumber(match[2], "en");
    const end = normalizeCountNumber(match[3], "en");
    const total = normalizeCountNumber(match[4], "en");
    return `${kind} ${start}–${end} of ${total} in ${englishUiLabel(match[5])}`;
  }

  match = core.match(/^(.+) — ([\d,.\s]+) (elementos?|campos?)$/);
  if (match !== null) {
    const label = englishUiLabel(match[1]);
    const count = normalizeCountNumber(match[2], "en");
    const singular = String(match[2]).replace(/\D/g, "") === "1";
    const noun = match[3].startsWith("elemento")
      ? `item${singular ? "" : "s"}`
      : `field${singular ? "" : "s"}`;
    return `${label} — ${count} ${noun}`;
  }

  match = core.match(/^Esta ejecución no superó el límite autorizado de hidratación de DevRev\. Motivo registrado: (.+)\.$/);
  if (match !== null) {
    return `This execution did not pass the authorized DevRev hydration boundary. Recorded reason: ${match[1]}.`;
  }

  match = core.match(/^No se registró para esta ejecución: (.+)\.$/);
  if (match !== null) {
    const missing = translateMissingProvenanceList(
      match[1],
      MISSING_PROVENANCE_TO_ENGLISH
    );
    return `Not recorded for this execution: ${missing}.`;
  }

  match = core.match(/^No hay procedencia de recuperación o prompt disponible\. Motivo reportado: (.+)\.$/);
  if (match !== null) {
    return `No retrieval or prompt provenance is available. Reported reason: ${match[1]}.`;
  }

  match = core.match(/^(Vinculado|Vinculado manualmente|No disponible): (\d+) (?:ejecución|ejecuciones) (?:disponible|disponibles)\.(?: (Parte de la evidencia puede estar incompleta\.))?$/);
  if (match !== null) {
    const status = SPANISH_TO_ENGLISH.get(match[1]) ?? match[1];
    const warning = match[3] ? ` ${SPANISH_TO_ENGLISH.get(match[3]) ?? match[3]}` : "";
    return `${status}: ${match[2]} execution${match[2] === "1" ? "" : "s"} available.${warning}`;
  }

  match = core.match(/^Esta ejecución RAG persistida registró (\d+) artículos? fuente y (\d+) fragmentos? acotados?\.(?: (.+))?$/);
  if (match !== null) {
    const sources = `${match[1]} source article${match[1] === "1" ? "" : "s"}`;
    const chunks = `${match[2]} bounded chunk${match[2] === "1" ? "" : "s"}`;
    const trailing = match[3] ? ` ${translateUiText(match[3], "en")}` : "";
    return `This persisted RAG execution recorded ${sources} and ${chunks}.${trailing}`;
  }

  match = core.match(/^Página (\d+)$/);
  if (match !== null) return `Page ${match[1]}`;

  match = core.match(/^(\d+) ejecuciones? seleccionadas?$/);
  if (match !== null) return `${match[1]} execution${match[1] === "1" ? "" : "s"} selected`;

  match = core.match(/^(\d+) filas? en esta página\.$/);
  if (match !== null) return `${match[1]} row${match[1] === "1" ? "" : "s"} on this page.`;

  match = core.match(/^Estado actual: (.+)\. Solo se ofrecen los cambios permitidos por el ciclo de revisión\.$/);
  if (match !== null) {
    const status = SPANISH_TO_ENGLISH.get(match[1]) ?? match[1];
    return `Currently ${status}. Only the moves the review lifecycle allows are offered.`;
  }

  match = core.match(/^Estado actual: (.+)\. Para cerrarla se necesita un resultado documentado y justificable\.$/);
  if (match !== null) {
    const status = SPANISH_TO_ENGLISH.get(match[1]) ?? match[1];
    return `Currently ${status}. Closing it needs a documented, defensible outcome.`;
  }

  match = core.match(/^Para cerrar esta revisión se requiere (.+)\.$/);
  if (match !== null) {
    const requirements = translateClosingRequirements(match[1], 1, 0);
    if (requirements !== null) {
      return `Closing this review needs ${requirements}.`;
    }
  }

  match = core.match(/^Sin asignar\. Puedes tomar una revisión sin asignar o liberar una asignada a ti\. Reasignar la revisión de otra persona requiere un administrador\.$/);
  if (match !== null) {
    return "Unassigned. You may take an unassigned review or release your own. Reassigning someone else's is an administrator action.";
  }

  match = core.match(/^Asignada a ti\. Puedes tomar una revisión sin asignar o liberar una asignada a ti\. Reasignar la revisión de otra persona requiere un administrador\.$/);
  if (match !== null) {
    return "Assigned to you. You may take an unassigned review or release your own. Reassigning someone else's is an administrator action.";
  }

  match = core.match(/^Asignada a (.+)\. Puedes tomar una revisión sin asignar o liberar una asignada a ti\. Reasignar la revisión de otra persona requiere un administrador\.$/);
  if (match !== null) {
    return `Assigned to ${match[1]}. You may take an unassigned review or release your own. Reassigning someone else's is an administrator action.`;
  }

  match = core.match(/^Sin asignar\. Como administrador, puedes tomarla o dejarla sin asignar\. Entregarla a una tercera persona requiere su identidad verificada de inicio de sesión, que ninguna ruta publica, por lo que esa opción no se ofrece aquí\.$/);
  if (match !== null) {
    return "Unassigned. As an administrator you may take it or clear it. Handing it to a third person needs their verified sign-in identity, which no route publishes, so it is not offered here.";
  }

  match = core.match(/^Asignada a ti\. Como administrador, puedes tomarla o dejarla sin asignar\. Entregarla a una tercera persona requiere su identidad verificada de inicio de sesión, que ninguna ruta publica, por lo que esa opción no se ofrece aquí\.$/);
  if (match !== null) {
    return "Assigned to you. As an administrator you may take it or clear it. Handing it to a third person needs their verified sign-in identity, which no route publishes, so it is not offered here.";
  }

  match = core.match(/^Asignada a (.+)\. Como administrador, puedes tomarla o dejarla sin asignar\. Entregarla a una tercera persona requiere su identidad verificada de inicio de sesión, que ninguna ruta publica, por lo que esa opción no se ofrece aquí\.$/);
  if (match !== null) {
    return `Assigned to ${match[1]}. As an administrator you may take it or clear it. Handing it to a third person needs their verified sign-in identity, which no route publishes, so it is not offered here.`;
  }

  match = core.match(/^El contenido original tenía (\d+) caracteres\.$/);
  if (match !== null) return `The upstream body was ${match[1]} characters.`;

  match = core.match(/^([\d,.\s]+) de ([\d,.\s]+) caracteres$/);
  if (match !== null) {
    return `${normalizeCountNumber(match[1], "en")} of ${normalizeCountNumber(match[2], "en")} characters`;
  }

  match = core.match(/^Seleccionar ejecución (.+)$/);
  if (match !== null) return `Select execution ${match[1]}`;

  match = core.match(/^Seleccionar ticket (.+)$/);
  if (match !== null) return `Select ticket ${match[1]}`;

  match = core.match(/^Evaluar ticket (.+)$/);
  if (match !== null) return `Review ticket ${match[1]}`;

  match = core.match(/^Los cambios se registrarán como (.+)\.$/);
  if (match !== null) return `Changes will be recorded as ${match[1]}.`;

  match = core.match(/^Abrir ejecución RAG (.+)$/);
  if (match !== null) return `Open RAG execution ${match[1]}`;

  match = core.match(/^Abrir ejecución (.+)$/);
  if (match !== null) return `Open execution ${match[1]}`;

  match = core.match(/^ID de solicitud (.+)$/);
  if (match !== null) return `Request id ${match[1]}`;

  match = core.match(/^Ahora en la versión (\d+)\.$/);
  if (match !== null) return `Now at version ${match[1]}.`;

  match = core.match(/^Selecciónalo y cópialo desde la página: (.+)$/);
  if (match !== null) return `Select and copy it from the page: ${match[1]}`;

  return core;
}

/** Translate one allowlisted UI phrase without interpreting arbitrary prose. */
export function translateUiText(value, language = "en") {
  const { leading, core, trailing } = splitSpacing(value);
  if (core === "") return String(value ?? "");

  const locale = normalizeLanguage(language);
  const exact = locale === "es" ? ENGLISH_TO_SPANISH.get(core) : SPANISH_TO_ENGLISH.get(core);
  const translated = exact ?? (locale === "es" ? spanishPattern(core) : englishPattern(core));
  return `${leading}${translated}${trailing}`;
}

function isThemeControl(node) {
  return node?.nodeType === 1 && (node.id === "theme-toggle" || node.closest?.("#theme-toggle") !== null);
}

function isProtectedText(node) {
  const parent = node.parentElement;
  if (parent === null || isThemeControl(parent)) return true;
  return parent.closest(PROTECTED_CONTENT) !== null;
}

function renderText(node, language) {
  if (isProtectedText(node)) return;

  const current = node.nodeValue ?? "";
  const previous = textHistory.get(node);
  if (language === "en") {
    if (previous !== undefined && current === previous.translated) {
      node.nodeValue = previous.original;
    }
    textHistory.delete(node);
    return;
  }

  if (previous !== undefined && current === previous.translated) return;
  const translated = translateUiText(current, "es");
  if (translated === current) {
    textHistory.delete(node);
    return;
  }
  textHistory.set(node, { original: current, translated });
  node.nodeValue = translated;
}

function historiesFor(element) {
  let histories = attributeHistory.get(element);
  if (histories === undefined) {
    histories = new Map();
    attributeHistory.set(element, histories);
  }
  return histories;
}

function renderAttribute(element, name, language) {
  if (!element.hasAttribute(name) || isThemeControl(element)) return;

  const current = element.getAttribute(name) ?? "";
  const histories = historiesFor(element);
  const previous = histories.get(name);
  if (language === "en") {
    if (previous !== undefined && current === previous.translated) {
      element.setAttribute(name, previous.original);
    }
    histories.delete(name);
    return;
  }

  if (previous !== undefined && current === previous.translated) return;
  const translated = translateUiText(current, "es");
  if (translated === current) {
    histories.delete(name);
    return;
  }
  histories.set(name, { original: current, translated });
  element.setAttribute(name, translated);
}

function renderTree(root, language) {
  if (root.nodeType === 3) {
    renderText(root, language);
    return;
  }
  if (![1, 9, 11].includes(root.nodeType)) return;

  if (root.nodeType === 1) {
    for (const name of TRANSLATABLE_ATTRIBUTES) {
      renderAttribute(root, name, language);
    }
  }
  for (const child of Array.from(root.childNodes ?? [])) {
    renderTree(child, language);
  }
}

function clearChildren(node) {
  while (node.firstChild !== null) {
    node.removeChild(node.firstChild);
  }
}

function renderTimes(language) {
  for (const node of Array.from(document.querySelectorAll("time[datetime]"))) {
    const parsed = new Date(node.dateTime);
    if (Number.isNaN(parsed.getTime())) continue;
    node.textContent = parsed.toLocaleString(language, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }
}

function renderAuditNumbers(language) {
  for (const node of Array.from(document.querySelectorAll("[data-audit-number]"))) {
    const raw = Number(node.getAttribute("data-raw-value"));
    if (!Number.isFinite(raw)) continue;
    node.textContent = raw.toLocaleString(language, {
      maximumFractionDigits: 20,
      useGrouping: true,
    });
  }
}

function preferenceEvent(detail) {
  const EventType = document.defaultView?.CustomEvent ?? globalThis.CustomEvent;
  if (typeof EventType === "function") {
    return new EventType("preferenceschange", { bubbles: true, detail });
  }
  const event = document.createEvent("CustomEvent");
  event.initCustomEvent("preferenceschange", true, false, detail);
  return event;
}

/**
 * Install page-lifetime theme and language controls.
 *
 * Calling this more than once is safe: the previous listeners and observer are
 * removed before the new controls take ownership.
 */
export function initPreferences({
  themeToggle = document.getElementById("theme-toggle"),
  languageSelect = document.getElementById("language-select"),
} = {}) {
  activePreferences?.destroy();

  const root = document.documentElement;
  const view = document.defaultView ?? globalThis;
  const themeColor = document.getElementById("theme-color");
  const browserLanguages = Array.isArray(view.navigator?.languages)
    ? view.navigator.languages
    : [view.navigator?.language].filter(Boolean);
  const browserLanguage = browserLanguages.find((item) => normalizeLanguage(item) === "es")
    ?? browserLanguages[0]
    ?? root.lang
    ?? "en";
  let theme = root.dataset.theme === "dark" ? "dark" : "light";
  let language = normalizeLanguage(languageSelect?.value === "es" ? "es" : browserLanguage);
  let observer = null;

  function emitChange() {
    document.dispatchEvent(preferenceEvent({ theme, language }));
  }

  function syncThemeControl() {
    if (themeToggle === null) return;
    const dark = theme === "dark";
    const nextTheme = dark ? "light" : "dark";
    const nextLabel = language === "es"
      ? (dark ? "Cambiar al modo claro" : "Cambiar al modo oscuro")
      : (dark ? "Switch to light mode" : "Switch to dark mode");
    themeToggle.setAttribute("aria-pressed", dark ? "true" : "false");
    themeToggle.setAttribute("aria-label", nextLabel);
    themeToggle.setAttribute("title", nextLabel);

    const visibleLabel = themeToggle.querySelector("#theme-label");
    if (visibleLabel !== null) {
      visibleLabel.textContent = language === "es"
        ? (nextTheme === "dark" ? "Oscuro" : "Claro")
        : (nextTheme === "dark" ? "Dark" : "Light");
    }

    const icon = themeToggle.querySelector(".theme-icon[data-icon], [data-icon]");
    const nextIcon = dark ? "sun" : "moon";
    if (icon !== null && icon.dataset.icon !== nextIcon) {
      icon.dataset.icon = nextIcon;
      clearChildren(icon);
    }
  }

  function setTheme(nextTheme, { notify = true } = {}) {
    theme = nextTheme === "dark" ? "dark" : "light";
    root.dataset.theme = theme;
    themeColor?.setAttribute("content", theme === "dark" ? "#07101f" : "#f4f5f3");
    syncThemeControl();
    if (notify) emitChange();
    return theme;
  }

  function setLanguage(nextLanguage, { notify = true } = {}) {
    language = normalizeLanguage(nextLanguage);
    document.documentElement.lang = language;
    if (languageSelect !== null) languageSelect.value = language;
    renderTree(root, language);
    renderTimes(language);
    renderAuditNumbers(language);
    syncThemeControl();
    if (notify) emitChange();
    return language;
  }

  function onThemeClick() {
    setTheme(theme === "dark" ? "light" : "dark");
  }

  function onLanguageChange(event) {
    setLanguage(event.currentTarget?.value ?? "en");
  }

  function onMutations(records) {
    if (language !== "es") return;
    for (const record of records) {
      if (record.type === "characterData") {
        renderText(record.target, language);
      } else if (record.type === "attributes") {
        renderAttribute(record.target, record.attributeName, language);
      } else {
        for (const node of Array.from(record.addedNodes)) {
          renderTree(node, language);
        }
      }
    }
  }

  themeToggle?.addEventListener("click", onThemeClick);
  languageSelect?.addEventListener("change", onLanguageChange);

  const ObserverType = view.MutationObserver ?? globalThis.MutationObserver;
  if (typeof ObserverType === "function") {
    observer = new ObserverType(onMutations);
    observer.observe(root, {
      subtree: true,
      childList: true,
      characterData: true,
      attributes: true,
      attributeFilter: TRANSLATABLE_ATTRIBUTES,
    });
  }

  setTheme(theme, { notify: false });
  setLanguage(language, { notify: false });

  const controller = {
    get language() {
      return language;
    },
    get theme() {
      return theme;
    },
    setLanguage,
    setTheme,
    destroy() {
      observer?.disconnect();
      themeToggle?.removeEventListener("click", onThemeClick);
      languageSelect?.removeEventListener("change", onLanguageChange);
      if (activePreferences === controller) activePreferences = null;
    },
  };
  activePreferences = controller;
  return controller;
}
