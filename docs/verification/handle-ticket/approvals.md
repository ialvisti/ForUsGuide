# Registro de aprobaciones de gates — handle-ticket production completion

Reglas:

- Cada fila se llena **únicamente** al recibir una aprobación real, con el texto exacto
  `APROBADO <GATE> <ALCANCE>` del aprobador requerido por el plan.
- Una aprobación de un gate no autoriza el siguiente. Los rollbacks y revalidaciones citan hash.
- Nunca se registran valores de secretos/tokens; sólo IDs, hashes, URIs con generation y metadatos.

| Gate | Texto exacto | Usuario | Rol | Fecha/hora UTC | Alcance | Evidencia |
|---|---|---|---|---|---|---|
| — | — | — | — | — | — | — |

## Skips no críticos aceptados por owner

| Test/skip | Owner | Fecha | Aceptación escrita |
|---|---|---|---|
| — | — | — | — |

## Canales de alerta verificados (Tarea 11)

| Canal lógico | Owner | Hora del ack | Notas |
|---|---|---|---|
| — | — | — | — |
