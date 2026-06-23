# Etapa 1 — Cliente async de ForusBots

> Prerrequisito: leer `00-context.md`. Esta etapa es **independiente** (no depende de otras).
> Es el núcleo de la "mejor recuperación de ForusBots".

## Objetivo
Un cliente async que hable correctamente con ForusBots: `submit (202 + jobId)` → `poll
GET /forusbot/jobs/:id` hasta estado terminal, con dedupe, semáforo de concurrencia, backoff y
manejo de errores. Sin tocar el resto de la app todavía.

## Archivo a crear
`kb-rag-system/data_pipeline/forusbots_client.py`

## Diseño

```python
class ForusBotsError(Exception): ...
class ForusBotsTimeout(ForusBotsError): ...
class ForusBotsJobFailed(ForusBotsError):
    def __init__(self, state, error): self.state, self.error = state, error; super().__init__(...)

@dataclass
class ScrapeResult:
    job_id: str
    state: str                     # succeeded|failed|canceled|timeout
    result: Optional[Dict[str, Any]]
    error: Optional[str]
    elapsed_seconds: Optional[float]
    stages: List[str] = field(default_factory=list)
    queue_position: Optional[int] = None

class ForusBotsClient:
    def __init__(self, base_url, auth_token, *, poll_interval_s=3.0, poll_backoff=1.3,
                 poll_max_interval_s=10.0, max_wait_s=200.0, http_read_timeout_s=15.0,
                 max_inflight=2, result_cache_ttl_s=180): ...
    async def aclose(self): await self._client.aclose()

    async def scrape_participant(self, participant_id, modules, *, strict=False, return_="data") -> ScrapeResult
    async def scrape_plan(self, plan_id, modules, *, strict=False, return_="data") -> ScrapeResult
```

### Reglas de implementación
1. **Un solo `httpx.AsyncClient` compartido**, header `x-auth-token`. `httpx.Timeout(connect=5,
   read=http_read_timeout_s, write=10, pool=5)` — estos son timeouts **por llamada HTTP**, distintos
   del `max_wait_s` end-to-end del loop de poll.
2. **Submit**: `POST {base}/forusbot/scrape-participant` con `{participantId, modules, return,
   strict, timeoutMs:int(max_wait_s*1000)}`. Esperar `202`; extraer `jobId`, `queuePosition`,
   `estimate`, `capacitySnapshot`. Loguear `running/queued` de `capacitySnapshot` para tuning.
3. **Poll loop**: dormir `min(estimate.avgDurationSeconds*0.6, 30)` antes del primer poll; luego
   `GET {base}/forusbot/jobs/{job_id}` con intervalo `poll_interval_s` × `poll_backoff` con tope
   `poll_max_interval_s` y jitter ±0.5s. Acumular `stage`/`state`. `succeeded` → devolver
   `ScrapeResult`. `failed`/`canceled` → `ForusBotsJobFailed` (NO reintentar el scrape; suele ser
   determinístico, p.ej. participante no existe). Exceder `max_wait_s` → `state="timeout"`,
   conservar `job_id` en logs (no hay endpoint de cancel en el contrato → no cancelar).
4. **Idempotencia / dedupe** (asumir que ForusBots NO deduplica):
   - `idem_key = sha256(participant_id + "|" + json.dumps(modules, sort_keys=True))`.
   - `self._inflight: dict[str, asyncio.Task]` — si ya hay un scrape idéntico en vuelo, **await la
     task existente** en vez de mandar un segundo job (clave para no saturar `maxConcurrency=3`).
   - `cachetools.TTLCache(maxsize=…, ttl=result_cache_ttl_s)` para reusar resultados recientes.
   - Nunca re-submitear ante timeout ambiguo del POST (un job pudo haberse creado).
5. **Semáforo**: `asyncio.Semaphore(max_inflight)` (default **2**, debajo de 3 para dejar headroom a
   otros callers y al par participant+plan). Se toma durante submit+poll.
6. **Resiliencia por llamada HTTP**: 3 reintentos con backoff exp (0.5→2s) ante
   `httpx.TransportError`/5xx/429 en CADA submit/poll individual. NUNCA reintentar el *submit* ante
   timeout ambiguo sin idempotencia.

## Definition of Done
- [ ] `forusbots_client.py` compila e importa sin tocar el resto de la app.
- [ ] `scrape_participant` / `scrape_plan` hacen submit→poll→terminal correctamente.
- [ ] Dedupe inflight: dos llamadas concurrentes idénticas comparten un solo job (verificable con
      mock que cuenta POSTs).
- [ ] Errores tipados (`ForusBotsTimeout`, `ForusBotsJobFailed`) con datos útiles.
- [ ] Tests unit con `httpx` mockeado (ver detalle en Etapa 6, pero se pueden adelantar aquí):
      submit→202→poll(queued→running→succeeded); failed→`ForusBotsJobFailed`;
      nunca-terminal→`ForusBotsTimeout`; dedupe; tope de semáforo.

## Cómo verificar
- Test unit (mock `httpx.AsyncClient`, patrón como el mock de OpenAI en `tests/test_llm_router.py`).
- (Opcional, manual) contra el **sandbox** `https://forusbots-6jyh.onrender.com/docs/sandbox/` con
  un `participantId` conocido y módulo mínimo `census`, validar 202 + poll a `succeeded`. Requiere
  `FORUSBOTS_AUTH_TOKEN` (lo agrega la Etapa 2; aquí se puede pasar por constructor).

## Notas para la siguiente etapa
La construcción del cliente (base_url, token, timeouts) se parametriza desde `settings` en la Etapa
2/5; aquí dejar el constructor con args explícitos.
