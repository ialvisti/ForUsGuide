# Correlación del consumidor PA

La ejecución de producción n8n `185815` recibió `body` como texto con JSON inválido: el emisor interpola una respuesta del agente (que puede incluir bloques Markdown y comillas) entre comillas sin serializar. No se recupera un ID confiando en una reparación del modelo.

El workflow DevRev 70 añade `x-pa-ticket-id` mediante la referencia nativa **Get Ticket > Output > Display Id**. Conserva `Accept: application/json`, cuerpo, destino, conexión y condiciones de ejecución. El texto heredado se trata como contenido opaco del modelo. El parser n8n recibe un objeto externo JSON válido, construido por `final-parser-input.js`; `final-data-extractor.js` aplica la misma normalización y comprueba el ID devuelto. Los envelopes JSON válidos anteriores siguen admitidos. Un header inválido, discrepancia de IDs o texto opaco sin correlación detiene cualquier escritura.

El header proporciona correlación; no autentica al emisor ni certifica una decisión de negocio. Todas las salidas siguen siendo borradores internos con revisión requerida. No se habilita envío al participante ni cierre automático.

## Dependencias y publicación

1. Publicar primero el header del emisor, compatible con el consumidor anterior.
2. Aplicar la expresión de `final-parser-input.js` al campo de texto del parser y el extractor completo verificado. Mantener el addendum de privacidad aprobado.
3. Ejecutar sólo parser y extractor con entrada sintética fijada; nunca ejecutar los nodos de comentarios o actualización del ticket. Restaurar los datos fijados originales antes de publicar.
4. Publicar el consumidor y verificar de nuevo los valores persistidos.

La validación de DevRev detectó cinco nodos inalcanzables que ya existían en v27: Update Ticket, Hybrid Search - Knowledge Base, Ask AI - Generate Response y dos Add Internal Comment. El usuario autorizó el 13 de septiembre de 2026 retirarlos **únicamente del borrador v28**. La comparación del grafo confirma que los ocho nodos alcanzables y sus siete conexiones se conservan. La versión publicada v27 queda como rollback. La evidencia de ejecución, comparación y publicación vive en el registro local del lote.

## Verificación y rollback

Seis regresiones sanitizadas cubren texto opaco con header, JSON externo del parser, envelope JSON válido como string, discrepancia header/body, header ausente o inválido, y respuesta vacía o excesiva. Cuatro fallaron antes del cambio; la suite completa de consumidores pasó con 63 pruebas después.

Rollback: restaurar primero la versión previa del consumidor n8n y después v27 del workflow DevRev. No eliminar datos fijados originales ni cambiar credenciales, permisos o mensajes. El cambio sólo afecta artefactos PA; no modifica el código de API/worker/Bots ni la KB o el índice.
