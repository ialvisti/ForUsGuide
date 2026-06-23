# Etapa 5 — Endpoints, job store e híbrido

> Prerrequisito: leer `00-context.md` + Etapas 2 (config/modelos) y 4 (orquestador). Aquí se
> exponen los endpoints y se implementa el contrato híbrido (inline rápido / 202 + poll lento).

## Objetivo
Exponer `POST /api/v1/handle-ticket` y `GET /api/v1/tickets/{id}`, cablear el lifespan, el job store
in-process con idempotencia, el gating `TICKET_HANDLER_MODE`, y la observabilidad.

## Archivos a modificar/crear
- `kb-rag-system/api/main.py`  (endpoints, lifespan, deps, gating)
- `kb-rag-system/data_pipeline/ticket_jobs.py`  (NUEVO — job store in-process)
- `kb-rag-system/data_pipeline/execution_logger.py`  (extender)
- `kb-rag-system/Dockerfile`  (`--workers 1`)

## 1) Job store — `ticket_jobs.py`
```python
@dataclass
class TicketJob:
    ticket_job_id: str
    state: str                 # queued|running|succeeded|partial|failed|timeout
    created_at: float
    results: List[InquiryResult] = field(default_factory=list)
    forusbots_job_ids: List[str] = field(default_factory=list)
    error: Optional[str] = None

class TicketJobStore:
    def __init__(self, ttl_s): self._jobs = TTLCache(maxsize=1024, ttl=ttl_s)
    def create(self) -> TicketJob          # genera id, state="queued"
    def get(self, id) -> Optional[TicketJob]
    def set_state(self, id, **kw)
```
(`ticket_job_id`: usar `request.state.request_id` o un uuid del middleware — NO `Math.random` raro;
generar con `uuid4` está bien aquí.)

## 2) Lifespan (main.py, después de L287)
Construir y guardar en `app.state`:
```python
app.state.forusbots_client = ForusBotsClient(settings.FORUSBOTS_BASE_URL, settings.FORUSBOTS_AUTH_TOKEN,
    poll_interval_s=settings.FORUSBOTS_POLL_INTERVAL_S, max_wait_s=settings.FORUSBOTS_MAX_WAIT_S,
    max_inflight=settings.FORUSBOTS_MAX_INFLIGHT, result_cache_ttl_s=settings.FORUSBOTS_RESULT_CACHE_TTL_S, ...)
app.state.ticket_jobs = TicketJobStore(settings.TICKET_JOB_TTL_S)
app.state.ticket_idem = TTLCache(maxsize=1024, ttl=settings.TICKET_JOB_TTL_S)   # idem_key -> ticket_job_id
app.state.bg_tasks = set()    # mantener refs a tasks en background (evitar GC)
```
En shutdown (L300): `await app.state.forusbots_client.aclose()`.

Deps mirroring `get_rag_engine` (L345-380): `get_forusbots`, `get_ticket_jobs`,
`get_orchestrator` (arma `TicketOrchestrator(OrchestratorDeps(...))` con los engines de state).

## 3) `POST /api/v1/handle-ticket`
Modelar sobre `route_inquiry_endpoint` (L790-916). `dependencies=[Depends(verify_api_key)]`.
```
effective_mode = request.ticket_handler_mode or settings.TICKET_HANDLER_MODE
if effective_mode == "disabled": raise HTTPException(503)
# idempotencia
idem = request.idempotency_key or header "Idempotency-Key"
if idem and idem in app.state.ticket_idem: return <handle/resultado existente>

extracted = await orch.extract_inquiries(request)
if not extracted: return TicketHandleResponse(route_taken=needs_more_info, primary=<saludo>, ...)

# Clasificar la primaria (rápido) para decidir inline vs job
primary_route = await orch.classify_primary(extracted[0])     # o clasificar todas
slow = any(r == generate_response for r in routes)

if effective_mode == "knowledge_only":
    # honrar solo KQ; si la primaria es GR -> fallback (decir a n8n que use flujo legacy)
if effective_mode == "shadow":
    # correr pipeline pero devolver decisión "fallback" + loguear diff legacy-vs-nuevo

if not slow:                       # todas rápidas -> inline 200
    results = await orch.run_fast(extracted, request)
    return TicketHandleResponse(...)
else:                              # alguna lenta -> 202 + job
    job = app.state.ticket_jobs.create()
    if idem: app.state.ticket_idem[idem] = job.ticket_job_id
    task = asyncio.create_task(_run_slow_job(job, extracted, request, orch, exec_logger))
    app.state.bg_tasks.add(task); task.add_done_callback(app.state.bg_tasks.discard)
    return TicketJobHandle(ticket_job_id=job.ticket_job_id, state="queued",
                           poll_url=f"/api/v1/tickets/{job.ticket_job_id}", estimate={...})
```
`_run_slow_job`: set_state running → `await orch.run_ticket(...)` con `asyncio.wait_for(...,
TICKET_TOTAL_BUDGET_S)` → set_state(succeeded|partial|failed|timeout, results, forusbots_job_ids).
Nunca lanza fuera (captura y guarda `error`).

**Status code**: usar `JSONResponse(status_code=202, ...)` para el handle; `200` para inline.
Documentar `response_model` con `Union`/dos modelos o `responses=`.

## 4) `GET /api/v1/tickets/{ticket_job_id}`
`Depends(verify_api_key)`. Buscar en `app.state.ticket_jobs`; `404` si no existe (TTL vencido);
devolver `TicketStatusResponse` con state/results/forusbots_job_ids/elapsed.

## 5) Gating `TICKET_HANDLER_MODE`
Replicar `_apply_router_mode` (L776-787) → `_apply_ticket_handler_mode`. Escribir
`override_reason`/`original_route` en `metadata` como en route-inquiry (L867-869). Exponer el modo
activo en `/health` (L479).

## 6) Observabilidad — `execution_logger.py`
Agregar `log_ticket_execution(request_id, ticket_job_id, idempotency_key, route_summary,
per_inquiry_steps, forusbots_job_ids, total_duration_ms, error)` → colección nueva
`ticket_executions`. Mantener el patrón de tragarse fallas (L84-87). Dejar `log_execution` intacto.
Además `logger.info/warning` por paso con `request_id`.

## 7) Dockerfile
Cambiar el CMD a **`--workers 1`** (el trabajo es I/O-bound; un worker async basta y el job store
in-process queda visible al GET). Comentar el porqué.

## Definition of Done
- [ ] `POST /handle-ticket`: rutas rápidas → `200` inline; ruta lenta → `202 {ticket_job_id}`.
- [ ] `GET /tickets/{id}`: refleja queued→running→succeeded/partial/failed/timeout.
- [ ] Idempotency-Key: POST repetido no lanza segunda orquestación ni segundo scrape.
- [ ] `TICKET_HANDLER_MODE=disabled`→503; `shadow`→fallback+log diff; `knowledge_only`→solo KQ.
- [ ] Los 4 endpoints existentes intactos. `/health` muestra el modo. `--workers 1` en Dockerfile.
- [ ] Tasks en background con refs en `app.state.bg_tasks` (no GC). `aclose()` en shutdown.

## Cómo verificar
`tests/test_handle_ticket_endpoint.py` (Etapa 6) con `TestClient` y engines+cliente mockeados.
Manual: levantar API y probar ambas rutas (ver Etapa 6, sección end-to-end).
