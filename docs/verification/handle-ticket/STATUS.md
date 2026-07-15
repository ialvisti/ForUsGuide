# Estado de ejecución del plan de finalización handle-ticket

Revisado el 2026-07-15 en el worktree aislado
`ForUsGuide-handle-ticket-finalization`, rama
`handle-ticket-production-finalization`. Las correcciones de la segunda
auditoría están consolidadas en `746eb28`; la evidencia actualizada queda en
el commit documental de cierre (18 commits sobre `3d48415`) y
siguen exclusivamente locales. No se hizo push ni PR y el worktree sucio
original no se modificó.

## Resultado ejecutivo

La afirmación anterior “Tareas 0–12 completas” era demasiado fuerte. El estado
correcto es:

- código/runtime/IaC ampliamente endurecidos y **717/735 tests locales pasan**;
- locks Python 3.12 y Terraform generados en Cloud Build por digest;
- producción sigue intacta en `kb-rag-system-00048-bkc`, 100% de tráfico,
  `TICKET_HANDLER_MODE=disabled`;
- ningún apply, deploy, cambio de IAM/tráfico/secrets/n8n ni activación ocurrió;
- Tarea 12 permanece incompleta porque falta un release-controller ejecutable
  e inmutable; Tareas 13–18 no comenzaron.

El trigger legacy `deploy-kb-rag-system` sigue activo y deploy-capable hasta
un futuro G1B. Por ello no debe hacerse merge/push a `main` antes de
neutralizarlo con el plan binario aprobado.

## Correcciones de la segunda auditoría

Además de las correcciones de Opus, se cerraron RED-first:

- Cloud Tasks admission: uso válido de stats/rate limits sin construir un
  request GA v2 imposible;
- worker con configuración core completa de RAG/LLM/ForusBots;
- `plan_type` canónico no sobrescribible por el LLM;
- WIF también en poll v1 y OIDC de tasks con audiencia/SA/email verificado;
- CAS transaccional de `enqueue_generation`;
- intent ForusBots durable antes del POST, fail-closed a reconciliación manual;
- errores upstream y excepciones sin payload raw en logs/polls/Firestore;
- SSN/cuentas/fechas textuales saneados antes de Pinecone;
- replay idempotente ligado a tenant;
- telemetría `ticket_executions` agregada y con TTL;
- heartbeat incapaz de resucitar un lease vencido;
- detect-secrets de CI leyendo el baseline actualizado real;
- tres provider locks publicados con nombres únicos y builders E2E fijados.

Detalle y limitaciones en `15-review-checklist.md`.

## Estado por tarea

| Tarea | Estado real |
|---|---|
| 0 preflight/contención | completa; snapshot GCP y worktree aislado |
| 1 contratos externos | inventario completo, pero **4 contratos siguen ausentes** |
| 2 regresiones RED | completa y ampliada en ambas revisiones |
| 3 imagen/locks | locks completos; build/smoke/audit autoritativos esperan callback OAuth |
| 4 auth/roles | código local cerrado; activación bloqueada por contrato participant-plan |
| 5 Firestore/TTL/cuotas | código/IaC cerrado; 11 tests de emulador pendientes del build remoto |
| 6 worker/fencing | cerrado localmente, incluida carrera de generación e intent externo |
| 7 Cloud Tasks/reconciler | cerrado localmente; staging real no ejecutado |
| 8 dependencias/probes | cerrado localmente; probes live gateadas |
| 9 diferencial/contratos | arnés real listo; export n8n y entrega final reales ausentes |
| 10 Terraform | módulo/roots/provider locks/validate tests presentes; ningún backend/apply |
| 11 observabilidad | métricas/alertas/dashboard/runbook locales; canales reales no aplicados |
| 12 CI/CD | **incompleta**: YAML/scripts endurecidos, pero falta empaquetar/probar el release-controller y cerrar scan/manifest E2E |
| 13–18 rollout | no iniciadas; bloqueadas por gates, contratos y Tarea 12 |

## GCP ejecutado en esta revisión

Sólo builds no desplegables y artefactos en el bucket Cloud Build existente:

- `6c5b340d-338e-407d-a81e-a03df2d5eb58`: locks Python, SUCCESS.
- `5716e603-bee8-4655-8255-0a01cc431864`: resolver Terraform SUCCESS,
  evidencia descartada por colisión de nombres.
- `a9ccc924-68a2-48af-87da-53a55ce9fff9`: tres provider locks, SUCCESS.

No se creó backend, API, queue, database, SA, trigger, secret, revisión o
tráfico. Autenticación no equivale a `APROBADO Gx <ALCANCE>`.

## Bloqueos que requieren intervención/autoridad nueva

1. Ejecutar y completar en una terminal interactiva
   `gcloud auth login --update-adc`; el intento de esta sesión esperó más de
   20 minutos sin recibir callback. Después se ejecuta el build autoritativo
   sin deploy.
2. Implementar y revisar el release-controller, luego producir su digest
   escaneado antes de pedir G1B.
3. Obtener los cuatro contratos: participant-plan, ForusBots
   HTTPS+idempotencia/reconcile, export real n8n + ARN WIF y entrega final
   idempotente.
4. Autorizar por separado push/PR si se desea publicar esta rama.
5. Registrar cada gate en `approvals.md` con
   `APROBADO <GATE> <ALCANCE>` y la evidencia exacta antes de cualquier
   mutación.

## Definition of Done

**Abierta.** El estado correcto es “hardening/rollout en progreso”. Aún no
existe staging activo ni el rollback anchor `hardened-disabled` de producción,
y no hay evidencia para G2–G10.
