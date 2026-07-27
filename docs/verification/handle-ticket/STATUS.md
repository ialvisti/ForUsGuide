# Estado de finalización de handle-ticket

Revisado el 2026-07-27 en la rama
`handle-ticket-production-finalization`.

## Resultado

La corrección de compatibilidad preserva el sistema existente:

- n8n continúa con `Authorization` de Cloud Run mediante
  `kb-rag-client` y el `X-API-Key` actual;
- no existe dependencia desplegable de AWS WIF, cuentas, ARN ni keys AWS;
- v1 y v2 aceptan la credencial legacy de n8n;
- el payload del n8n autenticado es válido sin directorio participant-plan;
- ForUsBots usa el contrato 2.5 observado, incluido el origen legacy exacto
  `http://35.224.156.104:10000`;
- submits ambiguos de ForUsBots permanecen fail-closed y no se reenvían a
  ciegas;
- la publicación al participante permanece en n8n/DevRev, sin cambios.

## Verificación actual

- suite CI local: **1349 passed, 14 skipped, 23 deselected**;
- Terraform 1.9.8 local: platform **20 passed**, staging **1 passed**,
  production validado y módulo **25 passed**;
- contrato vivo de ForUsBots: documentación/OpenAPI/health accesibles;
- Terraform ya no contiene pools/providers AWS WIF, cuentas
  `n8n-ticket-invoker-*` ni variables `ticket_wif_*`;
- `kb-rag-client` permanece como invocador IAM declarativo;
- el verificador mínimo de Cloud Build y su lectura `objectViewer` del bucket
  de source están declarados/importables; no posee publish/deploy/state;
- Cloud Build `1ab86e09-1a95-4695-96e2-6bbcba82d083`: **SUCCESS** sobre el
  commit de código `0405bf32fdf93cc44041dd4539428740a45fc25a`; pasaron los
  nueve steps de Python 3.12, Terraform, emulador Firestore, imagen runtime,
  imagen CI, E2E y release-controller con sus smokes;
- producción no fue mutada: no hubo apply, deploy, cambio de secretos,
  tráfico ni n8n.

Este build fue verify-only: no contiene push de imágenes, deploy, Terraform
apply, escritura de evidencia ni cambios de tráfico.

## Estado de tareas

| Área | Estado |
|---|---|
| Código API/worker/Firestore/Tasks/reconciler | completo localmente |
| Compatibilidad n8n | resuelta con el contrato existente |
| Contrato ForUsBots | resuelto contra docs y código 2.5 |
| Contratos externos que bloqueen merge | ninguno |
| CI/Terraform remoto del código actual | completo y verde |
| Merge | pendiente únicamente de push y revisión/aprobación del PR |
| Staging/producción | no iniciado; requiere gates de mutación explícitos |

## Definition of Done

Para merge: diff revisado, suite local y build remoto verdes, Terraform
validado, PR actualizado y sin checks pendientes.

Para rollout: sigue abierta hasta desplegar staging, ejecutar E2E real,
aprobar promociones y crear un rollback anchor endurecido. El merge no
autoriza esas mutaciones.
