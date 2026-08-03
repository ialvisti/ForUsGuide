# Pendientes y estado actual

Última revisión: 2026-08-03.

Este documento es el punto de entrada para retomar trabajo en ForUsGuide. El
incidente de ejecuciones GCP del 2026-08-02 está cerrado; lo siguiente separa
las validaciones operativas aún pendientes de proyectos nuevos o parciales.

## Resumen ejecutivo

| Área | Estado | Próxima acción |
|---|---|---|
| Incidente `handle-ticket` | Corregido y activo en producción bajo `full` | Validar una ejecución real iniciada desde n8n |
| Git y CI | Base funcional `646c01f`; build canónico `SUCCESS` | Mantener los gates verdes |
| Runtime RAG | Producer y worker `Ready`; queue `RUNNING`; scheduler `ENABLED` | Observar ejecuciones reales y alertas |
| Infraestructura Terraform | Recursos vivos alineados, pero parte del bootstrap no está adoptada en state | Preparar adopción gobernada, revisar plan y aplicar sólo con aprobación |
| Consola administrativa `/tickets` | Etapas 1–3 implementadas; etapas 4–11 y 99 pendientes | Continuar por la etapa 4 |
| Integración n8n | Workflow sanitizado e importable | El owner de n8n debe ejecutar el caso real |
| Transporte hacia ForUsBots | Idempotencia durable activa sobre origen HTTP legacy | Migrar coordinadamente a HTTPS o ingress privado |

## Estado actual verificado

- GitHub y el checkout local sólo tienen la rama `main`.
- La base funcional auditada antes de este documento es
  `646c01f0f197f3d67cff0b85501f0c13bb52641b`.
- El build de esa base, `3a4f792a-742f-4fe2-a231-24b097c607fb`, terminó en
  `SUCCESS`; generó una imagen y completó tests, lint, tipos, auditoría de
  dependencias, SBOM y escaneo.
- Producción continúa en la versión del incidente `d60388b`:
  - producer `kb-rag-system-fulld60388b`, `Ready=True`, modo `full`;
  - worker `kb-rag-ticket-worker-incd60388b`, `Ready=True`, modo `full`;
  - reconciliador listo, queue `ticket-jobs-prod` en `RUNNING` y scheduler en
    `ENABLED`.
- Los ocho errores históricos tuvieron efecto upstream exitoso y no deben
  reintentarse.
- Los dos falsos `PINECONE_TRANSIENT_FAILURE` fueron bloqueos locales
  `UnsafeRetrievalQuery`; no hay evidencia de outage de Pinecone.
- ForUsBots ya ofrece idempotencia durable para las operaciones utilizadas por
  el RAG.

La base `646c01f` no está promovida al runtime productivo. Esto no deja el
incidente a medias: los cambios posteriores a `d60388b` son principalmente la
base parcial de la consola administrativa, documentación y mantenimiento de
CI. No se debe desplegar la consola hasta completar su servicio, seguridad,
infraestructura y rollout.

## Trabajo a medias

### P0 — Validación real desde n8n

El backend `full` y el workflow están listos, pero falta la prueba desde la
instancia real de n8n con sus credenciales existentes. El owner de n8n debe:

1. Ejecutar el caso operativo autorizado.
2. Confirmar aceptación, polling hasta estado terminal y ausencia de duplicados.
3. Confirmar que la ruta RAG termina sin `INTERNAL_ERROR`, sin external ID
   ausente y sin discrepancia entre ticket job y ForUsBots job.
4. Guardar sólo IDs técnicos, timestamps y estados en la evidencia; no copiar
   tokens, respuestas completas ni PII al repositorio.

Criterio de cierre: ejecución terminal `succeeded`, relación durable
consistente y resultado funcional confirmado por el operador de n8n.

### P1 — Adoptar la infraestructura bootstrap en Terraform

Queue, scheduler, reconciliador e IAM fueron alineados en vivo, pero no todo
está adoptado por el state de producción. Falta:

1. Completar el bootstrap publisher/controlador pre-G1B.
2. Importar/adoptar los recursos existentes sin recrearlos.
3. Revisar un plan que tenga cero `delete` y cero `replace` inesperados.
4. Obtener la aprobación operativa requerida.
5. Ejecutar `terraform apply` y comprobar después `No changes`.

No ejecutar `apply` desde este documento ni durante una auditoría de sólo
lectura. La guía vigente está en
[`REMEDIACION_EJECUCIONES_GCP_2026-08-02.md`](REMEDIACION_EJECUCIONES_GCP_2026-08-02.md).

### P1 — Completar la consola RAG `/tickets`

Implementado y fusionado en `main`:

- etapa 1: contratos, configuración aislada, modelos y guard de alcance;
- etapa 2: cliente DevRev read-only resiliente;
- etapa 3: repositorio Firestore de revisión, auditoría e idempotencia.

La etapa 3 tiene cobertura unitaria, pero todavía necesita cerrar su prueba con
emulador/entorno aislado como parte del flujo completo. El servicio admin no
está cableado, no tiene UI desplegada y no forma parte del tráfico productivo.

Orden pendiente obligatorio:

1. Etapa 4: hidratación DevRev y procedencia RAG.
2. Etapa 5: servicio admin, IAP, RBAC y API.
3. Etapas 6–7: lista, detalle, evaluación e historial en la UI.
4. Etapa 8: lotes de remediación human-in-the-loop y CLI.
5. Etapa 9: migración/exportación CSV segura.
6. Etapa 10: Terraform, IAP, base Firestore dedicada, IAM, secretos, retención
   y observabilidad.
7. Etapa 11: verificación end-to-end y rollout por staging.
8. Etapa 99: auditoría independiente y reparación final.

Fuente canónica:
[`tickets-development-plan/README.md`](tickets-development-plan/README.md).
No confundir esta consola con los endpoints existentes de ticket jobs bajo
`/api/v1/tickets/{ticket_job_id}`.

### P2 — Mantenimiento de CI

Restaurar el permiso mínimo de lectura de Artifact Registry para las service
accounts manuales usadas por builds `test-only`. No ampliar permisos de deploy.
El build canónico de `main` no está bloqueado por esta deuda.

### P2 — Transporte privado hacia ForUsBots

Reemplazar el origen HTTP legacy por HTTPS o ingress privado. Requiere cambio
coordinado en ForUsBots, configuración RAG, health checks y n8n. Mantener la
idempotencia y el rollback existentes durante la migración.

### P2 — Seguimiento operativo

- Vigilar ejecuciones reales, backlog, reconciliador y errores de negocio.
- Conservar el rollback `knowledge_only` hasta completar la observación acordada.
- Revertir ante `INTERNAL_ERROR`, external ID ausente o discrepancia durable.
- No reindexar ni modificar Pinecone como parte de estos pendientes. Cualquier
  trabajo futuro con vectores debe conservar namespaces y empezar con una
  auditoría de sólo lectura.

## Qué trabajar primero

1. Prueba real de n8n y evidencia sanitizada.
2. Adopción Terraform con plan sin destrucciones.
3. Etapa 4 de `/tickets`, siguiendo TDD y el orden del master plan.
4. Cierre de CI manual y migración de transporte.
5. Observación continua y actualización de este archivo cuando cambie un
   estado.

## No hacer

- No repetir los ocho jobs históricos.
- No ejecutar `terraform apply` sin plan revisado y aprobación.
- No desplegar la consola `/tickets` incompleta sobre el servicio público RAG.
- No copiar secretos, payloads completos ni PII a issues o documentación.
- No mutar ni reindexar Pinecone para validar este incidente.

## Documentación relacionada

- [`AUDITORIA_EJECUCIONES_GCP_2026-08-02.md`](AUDITORIA_EJECUCIONES_GCP_2026-08-02.md)
- [`REMEDIACION_EJECUCIONES_GCP_2026-08-02.md`](REMEDIACION_EJECUCIONES_GCP_2026-08-02.md)
- [`docs/verification/handle-ticket/STATUS.md`](docs/verification/handle-ticket/STATUS.md)
- [`tickets-development-plan/README.md`](tickets-development-plan/README.md)
