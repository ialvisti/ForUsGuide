# Auditoría de ejecuciones recientes en GCP

**Fecha del análisis:** 2026-08-02
**Proyecto:** `rag-kb-system` (`900340137010`)
**Región principal:** `us-central1`
**Ventana principal:** desde el despliegue actual, `2026-07-27T21:20:00Z`, hasta aproximadamente `2026-08-02T14:40:00Z`
**Modo de revisión:** solo lectura; no se modificó ningún recurso de GCP ni se reintentó ningún ticket.

## Resumen ejecutivo

El problema no es una caída general de Cloud Run, Cloud Tasks, OpenAI, Pinecone ni ForUsBots. El servicio acepta y consume correctamente las solicitudes a nivel HTTP, pero una parte importante falla después de completar el trabajo externo, al intentar guardar el resultado de `generate_response` en Firestore.

En la ventana revisada hubo **25 trabajos con estado terminal**:

| Estado de negocio | Cantidad | Porcentaje |
|---|---:|---:|
| `succeeded` | 15 | 60% |
| `failed / INTERNAL_ERROR` | 8 | 32% |
| `partial / PINECONE_TRANSIENT_FAILURE` | 2 | 8% |
| **No satisfactorios** | **10** | **40%** |

Los **8 fallos `INTERNAL_ERROR`**:

- ocurrieron exclusivamente en la ruta `generate_response`, siempre en `inquiry_index=0`;
- completaron antes sus llamadas a OpenAI, Pinecone y ForUsBots con HTTP 200;
- produjeron un log de respuesta generada con confianza y, unos cientos de milisegundos después, cayeron en el `catch` genérico del worker;
- quedaron marcados como `manual_reconciliation_required=true`, `retryable=false` y `next_action=use_legacy_or_human`;
- fueron reconocidos por el worker con HTTP 200, por lo que Cloud Tasks los consumió sin reintento.

### Causa raíz de alta confianza

El código desplegado construye dentro de `diagnostics.field_mapping.deterministic_mapped` una estructura con **arreglos directamente anidados**, por ejemplo:

```json
{
  "deterministic_mapped": {
    "termination_date": [
      ["census", "Termination Date"]
    ]
  }
}
```

La edición **Standard** de Firestore usada por este proyecto no admite que un arreglo contenga directamente otro arreglo. La ruta fue reproducida localmente con el código equivalente al desplegado: `_map_fields` crea el arreglo anidado, el modelo Pydantic lo conserva, `_entry_from_outcome` lo incluye en `result` y `record_inquiry_result` intenta persistirlo. En un resultado realista, esta fue la **única** ruta con un arreglo dentro de otro arreglo; la conversión Pydantic tardó 0.136 ms, mientras que el intervalo observado en producción es compatible con una escritura rechazada por Firestore.

La confianza estimada es **alta (~0.9)**, no absoluta, porque el `except Exception` desplegado elimina el tipo de excepción y el stack trace, y el diagnóstico que falló nunca llegó a persistirse. Los logs tampoco registran si cada uno de los ocho casos utilizó al menos un mapeo determinístico. Aun así, la estructura inválida, el punto exacto de caída, la reproducción y el patrón 8/8 hacen que ésta sea, por amplio margen, la explicación mejor sustentada.

Los **2 estados parciales etiquetados como `PINECONE_TRANSIENT_FAILURE` tampoco corresponden a una caída de Pinecone**. El código rechazó localmente la consulta mediante `UnsafeRetrievalQuery` y luego la clasificación genérica la convirtió incorrectamente en un error transitorio de Pinecone.

Por separado, el reconciliador está funcional: al corte de verificación se listaron 1,092 ejecuciones retenidas, 1,091 exitosas y una pendiente. Sin embargo, se programa cada minuto y el 88.2% de sus ejecuciones completas duró más de 60 segundos, por lo que existe solapamiento y gasto innecesario, aunque no hay evidencia de que eso haya causado los ocho fallos.

## Inventario de producción observado

### Cloud Run

| Componente | Revisión / configuración relevante |
|---|---|
| Productor | `kb-rag-system-00052-gxw`, 100% del tráfico, `APP_ROLE=producer`, `TICKET_HANDLER_MODE=full`, concurrencia 80, timeout 300 s, 1 CPU / 512 MiB, máximo 5 instancias |
| Worker | `kb-rag-ticket-worker-00004-zf8`, 100% del tráfico, ingress interno, `APP_ROLE=worker`, concurrencia 1, timeout 520 s, 1 CPU / 1 GiB, máximo 2 instancias, CPU siempre asignada |
| Imagen común | Digest `sha256:fc84c5412e5cb7908e6aded51975f905cb925451af4fe505826bf566ef7f4c0b` |
| Build desplegado | `f3b850eb-b2e6-4235-b964-345b2def90bd`, commit `83965cb0b52cf6b69f96d3bc25830395dd544678` |

El build posterior `10ee…`, commit `3543f431…`, terminó correctamente pero **no está desplegado**. Su diferencia respecto al runtime fue documental, por lo que no explica los fallos.

### Tareas, datos y reconciliación

| Recurso | Estado observado |
|---|---|
| Cloud Tasks queue | `ticket-jobs-prod`, `RUNNING`, 2 tareas concurrentes, 2/s, máximo 5 intentos, ventana de reintento de 1,800 s; 0 pendientes al consultar |
| Logs por tarea de Cloud Tasks | Desactivados (`stackdriverLoggingConfig=null`) |
| Firestore | Base `(default)`, modo Native, edición `STANDARD`, `us-central1` |
| Reconciliador | Cloud Run Job `ticket-reconciler-prod`, `--once --batch-size=25`, timeout 300 s, máximo 1 reintento |
| Scheduler | `ticket-reconciler-prod-tick`, cada minuto, UTC; todas las invocaciones examinadas devolvieron 200 |

No se encontraron Cloud Functions, VMs, MIGs, Batch jobs ni un segundo servicio de staging que expliquen las ejecuciones fallidas. La API de Cloud Functions estaba deshabilitada y no se habilitó durante la auditoría.

## Qué ocurrió en las ejecuciones recientes

Los contadores provienen de eventos estructurados deduplicados y de proyecciones seguras de Firestore, sin incluir payloads de tickets ni datos personales:

- 26 solicitudes al productor `POST /api/v1/handle-ticket`, todas HTTP 202;
- 25 entregas al worker, todas HTTP 200;
- 45 consultas de estado, todas HTTP 200;
- 25 trabajos con estado terminal;
- ningún trabajo mostró `attempt > 1`;
- duración de negocio: mínimo 9.65 s, promedio 56.64 s, máximo 148.93 s.

Los contadores HTTP y terminales se obtuvieron de fuentes y límites temporales distintos; la diferencia de una solicitud no debe interpretarse por sí sola como pérdida de trabajo.

### Los ocho `INTERNAL_ERROR`

| Job ID | Trace ID | Creado (UTC) | Duración (s) |
|---|---|---:|---:|
| `686c63d0846e4e2f90bc1439700d3d89` | `abe7da76-e8de-4554-b015-65a0286ec0ec` | 2026-07-30 23:24:11.108 | 123.02 |
| `43d41fd2f7074c9da8236246dac8b62d` | `a92939c2-bda2-4558-94ef-6e145fcf2b82` | 2026-07-30 23:29:20.849 | 94.87 |
| `c40b82db8f94428e8059def48dea9e67` | `8b67f7aa-99db-4125-8780-fb9bb3c81941` | 2026-07-31 15:49:30.025 | 116.54 |
| `b6a1b32782d146a28b75f1382a489c1e` | `f06224f4-4218-42b8-bf93-dfdbcc742314` | 2026-07-31 22:40:36.541 | 122.13 |
| `1aef5819caad4f2399f35ea71510ac52` | `d33197ff-b349-4c84-8036-36da5af86986` | 2026-08-01 01:10:42.206 | 108.04 |
| `a219e9f8b9a34c5f876755df5ec6e5c3` | `2e792286-691a-412f-83ff-782ac26a584f` | 2026-08-01 15:07:23.756 | 148.93 |
| `0dd41f7ea62a4912acf0dcc62fc52ba6` | `1b28544b-6c37-46c4-953a-73ca8036717c` | 2026-08-02 01:41:49.092 | 106.13 |
| `cba61d63bb2640f08c0ed25e3f046fbe` | `6ab8dac5-768b-429c-b29b-32e3d1ca8abe` | 2026-08-02 14:20:42.897 | 120.57 |

#### Línea temporal del fallo más reciente

| Hora UTC | Evento |
|---|---|
| 14:20:42.719 | inicia el POST al productor |
| 14:20:43.206 | el trabajo queda aceptado |
| 14:20:43.392 | comienza el worker |
| 14:20:46.594 | el productor responde HTTP 202 |
| 14:22:42.919 | termina la última llamada a OpenAI con HTTP 200 |
| 14:22:42.923 | `generate_response` registra confianza `0.951…` |
| 14:22:43.164 | `ticket inquiry failed (inquiry_index=0)` |
| 14:22:43.316 | se marca fallido el paso `generate_response` |
| 14:22:43.386 | se emite la métrica de reconciliación manual |
| 14:22:43.626 | el trabajo queda terminal con `INTERNAL_ERROR` |

Los otros siete fallos tienen el mismo patrón. Hubo exactamente ocho eventos de confianza de `generate_response` y los ocho quedaron seguidos por el error genérico; no se observó ningún `generate_response` exitoso en esta muestra.

### Por qué Cloud Run y Cloud Tasks aparecen “verdes”

El worker devuelve HTTP 200 incluso cuando el resultado de negocio termina en `failed`. Esto es intencional para que Cloud Tasks no repita automáticamente una operación después de posibles efectos externos. En estos ocho casos, ForUsBots ya había aceptado y completado la operación; un reintento ciego podría duplicar efectos.

Por tanto:

- HTTP 200 significa “la entrega fue consumida”, no “el ticket terminó correctamente”;
- los dashboards basados solo en 5xx del worker no detectarán esta clase de fallo;
- el indicador correcto debe ser el evento/estado terminal del trabajo y el contador de reconciliación manual.

## Análisis de causa de los ocho fallos

### Evidencia de dependencias sanas

Para los ocho trabajos:

- ForUsBots emitió 8 `submit_success` y 8 `poll_success`; no hubo fallo ni apertura de circuito;
- las llamadas observadas a OpenAI y Pinecone devolvieron HTTP 200;
- el generador alcanzó a registrar una respuesta y su confianza;
- no se observó un error de `generate_response` antes del checkpoint.

Esto descarta razonablemente una interrupción de esas dependencias como causa inmediata.

### Ruta exacta del defecto

En el código inspeccionado, equivalente al runtime desplegado:

1. `kb-rag-system/data_pipeline/ticket_orchestrator.py`, `_map_fields`, crea `Dict[str, List[List[str]]]` y convierte cada par a `list(e)`.
2. Ese valor se coloca en `diagnostics.field_mapping.deterministic_mapped`.
3. `_handle_gr` devuelve los diagnósticos dentro de `InquiryOutcome`.
4. `outcome_to_inquiry_result` y `_entry_from_outcome` conservan la estructura dentro de `result`.
5. `kb-rag-system/api/ticket_worker.py` llama a `repo.record_inquiry_result(...)`.
6. `kb-rag-system/data_pipeline/ticket_job_repository.py` intenta agregar el resultado a `per_inquiry_status` en Firestore.
7. Firestore Standard rechaza el array directo dentro de otro array.
8. El `except Exception` en `ticket_worker.py` registra únicamente `ticket inquiry failed`, sin `exc_info`, tipo ni código gRPC, y sustituye el resultado por un `INTERNAL_ERROR` pequeño que sí puede persistirse.

La restricción de tipos está documentada en [tipos de datos de Cloud Firestore](https://docs.cloud.google.com/firestore/native/docs/concepts/data-types) y en la definición REST de [ArrayValue](https://docs.cloud.google.com/firestore/docs/reference/rest/Shared.Types/ArrayValue).

### Reproducción local

Con el mapeador real se obtuvo:

```python
{
    "termination_date": [["census", "Termination Date"]],
    "account_balance": [["savings_rate", "Account Balance"]],
}
```

Al construir un resultado realista, con 10 artículos fuente y 21 chunks usados:

- la conversión Pydantic finalizó en 0.136 ms;
- la única ruta con un array directamente dentro de otro fue `result.diagnostics.field_mapping.deterministic_mapped.<slug>[0]`;
- el documento minimizado ocupó aproximadamente 10.8 KiB, por lo que un límite de tamaño es poco probable;
- el repositorio en memoria lo aceptó, como era esperable;
- el SDK local pudo serializarlo a protobuf sin validarlo, por lo que el rechazo se produce en el servidor Firestore.

También se ejecutaron 137 pruebas del ref inspeccionado: 134 pasaron y 3 pruebas de autenticación fallaron porque al entorno local le falta la dependencia `cachecontrol`, no por la ruta estudiada. Esto no equivale a una validación E2E contra Firestore: precisamente falta en la suite una prueba con el documento GR completo y un backend que aplique las restricciones reales del servidor.

El historial del código muestra además que la estructura anidada existía desde junio y que la persistencia durable se incorporó después, dejando una incompatibilidad latente que los tests con backend en memoria no detectaron.

### Limitación probatoria

No puede afirmarse al 100% que los ocho documentos contenían un mapeo determinístico porque:

- esa parte del diagnóstico no se loguea;
- la escritura fallida nunca quedó almacenada;
- la entrada de error posterior reemplazó el resultado;
- ForUsBots confirma que había módulos, pero no si provinieron del catálogo determinístico o del mapeo del LLM.

La alternativa residual es otro error de validación o escritura de Firestore en el mismo checkpoint. El arreglo anidado es, sin embargo, un defecto concreto, reproducible y suficiente para explicar todo el patrón.

## Los dos falsos `PINECONE_TRANSIENT_FAILURE`

| Job ID | Trace ID | Creado (UTC) | Duración (s) |
|---|---|---:|---:|
| `f4e30e1e36c540cd829a9981cd70eb2b` | `e929bf1d-62fd-413b-aa65-d056a296b33a` | 2026-07-30 01:27:51.915 | 28.63 |
| `19074ee4c334410faba544e0503cace5` | `e8cae6fe-2f70-4ef5-b4a8-d954d78d7e5c` | 2026-08-01 01:26:50.678 | 9.65 |

El warning real fue:

```text
Coverage retrieval failed (UnsafeRetrievalQuery); returning failed pack.
```

La cadena causal es:

1. `sanitize_retrieval_query` aplica la barrera de privacidad/seguridad.
2. Si la consulta no contiene un concepto de retiro revisado, lanza `UnsafeRetrievalQuery` localmente.
3. Un `catch` genérico convierte cualquier excepción en `CoveragePack.failed`.
4. `outcome_is_degraded` interpreta todo `retrieval_status=failed` como `PINECONE_TRANSIENT_FAILURE`.

No hubo evidencia de timeout, 429, 5xx, fallo de autenticación ni circuit breaker de Pinecone en esos dos casos. La etiqueta actual induce a un reintento y a un diagnóstico equivocados: es una denegación determinística y no reintentable, no una indisponibilidad del proveedor.

## Estado del reconciliador

En el conjunto retenido de ejecuciones del Cloud Run Job, al corte aproximado `2026-08-02T14:56:00Z`:

- 1,092 ejecuciones listadas;
- 1,091 exitosas;
- 0 fallidas o canceladas;
- 1 pendiente al momento de la consulta;
- duración de las 1,091 completadas, redondeada al segundo: mínimo 32 s, promedio 96.92 s, p50 97 s, p90 135 s, p95 144 s, p99 159 s y máximo 191 s;
- 962 de 1,091, es decir 88.2%, superaron el intervalo de un minuto.

Una ejecución representativa hizo menos de un segundo de trabajo de aplicación, pero pasó alrededor de 100 segundos esperando aprovisionamiento o scheduling. El scheduler recibe 200 y crea las ejecuciones, por lo que no hay un fallo del reconciliador; hay una frecuencia que favorece solapamientos, ruido y costo.

Además, cada ejecución emite la advertencia deprecada de Firestore:

```text
Detected filter using positional arguments. Prefer using filter keyword argument instead.
```

Debe corregirse, aunque no es la causa del incidente.

## Hallazgos de observabilidad

### 1. La excepción útil se pierde

El worker captura `Exception` y no registra stack, clase, fase de persistencia ni estado gRPC. Esto impide distinguir desde producción entre:

- conversión del modelo;
- validación del documento;
- límite de tamaño/profundidad;
- escritura/contención;
- rechazo de Firestore;
- otro fallo interno.

La corrección debe registrar metadatos sanitizados, nunca el payload ni el mensaje crudo si puede contener PII.

### 2. Duplicación de eventos de aplicación

Los mismos eventos aparecen como una línea cruda de `stderr` y como una entrada estructurada con `labels.python_logger`. Los filtros de varias métricas basadas en logs usan solamente `textPayload:"ticket_metric_event"` y no restringen la entrada canónica. Son susceptibles de contar dos veces terminales, tokens, costos, llamadas externas y reconciliaciones manuales.

Debe conservarse un solo handler o restringir todas las métricas a una única representación, y después validar la razón evento/contador en Monitoring.

### 3. Métricas del reconciliador: producción y Terraform no coinciden

Las métricas activas de `errors`, `fenced_leases` y `deadline_terminalized` ya contienen en producción el filtro positivo:

```text
textPayload=~"\"value\":[1-9][0-9]*"
```

Esto evita alertar por eventos con `value:0`. Sin embargo, el `monitoring.tf` inspeccionado no conserva esa condición; un `apply` desde esa fuente podría reintroducir alertas falsas. Es deriva de infraestructura que debe importarse o corregirse antes del siguiente despliegue.

### 4. Faltan logs de Cloud Tasks

El logging por tarea está desactivado. Los logs del worker permiten reconstruir bastante, pero no la historia completa de creación, despacho y reintento de cada tarea desde Cloud Tasks. Conviene habilitar un muestreo útil y controlar su costo.

### 5. Las alertas HTTP no bastan

La política de 5xx del worker no detecta fallos de negocio reconocidos con 200. La alerta debe usar estados terminales, tasa de `INTERNAL_ERROR`, `manual_reconciliation_required` y ausencia anormal de terminalización.

## Hallazgos de configuración, Git y despliegue

### Checkout local desalineado

El checkout desde el que se solicitó el análisis está en:

- rama `handle-ticket-hardening`;
- commit `3d48415`, del 2026-07-13;
- 47 commits por detrás del `main` remoto observado;
- sin el objeto exacto del commit desplegado;
- con cambios previos del usuario y archivos no rastreados.

El código del runtime se contrastó en el worktree `ForUsGuide-handle-ticket-finalization`, ref `04fe252`, cuyos archivos relevantes coinciden con el código del despliegue `83965cb`.

**No debe ejecutarse un despliegue desde el checkout activo.** Su `cloudbuild.yaml` y documentación son anteriores, emplea referencias mutables como `latest` y podría desplegar código obsoleto o revertir correcciones de infraestructura. La solución debe prepararse desde un worktree limpio basado en el commit realmente desplegado o en el `main` actual, verificando primero el diff efectivo.

### Drift manual de infraestructura

Queue, scheduler, identidades y bindings se crearon o ajustaron manualmente según la documentación de entrega. La configuración live debe reconciliarse con Terraform antes de confiar en un plan/apply. En particular, hay drift confirmado en filtros de métricas.

### Dependencia ForUsBots

El runtime usa el origen legacy `http://35.224.156.104:10000`.

- Respondió correctamente en los ocho casos, por lo que no causó estos fallos.
- Al usar HTTP e IP fija, sigue siendo un riesgo de disponibilidad, confidencialidad, rotación y observabilidad.
- El checkpoint terminal no conserva de forma durable el ID externo retornado, lo que complica reconciliar efectos ya ejecutados.

### Workflow n8n

El workflow local no rastreado contiene mejoras, pero no hay evidencia de que esté importado o activo en producción. Mantiene un ciclo de polling sin límite global:

```text
Get Job -> If Done(false) -> Wait -> Replicate Data -> Get Job
```

Los timeouts por request no evitan que el workflow completo consulte indefinidamente si el upstream queda en `pending`. Esto no explica los ocho fallos —ForUsBots terminó—, pero debe corregirse.

## Errores históricos que no explican el incidente actual

Antes del build desplegado hubo varios fallos el 2026-07-27. Todos quedaron superados por builds y revisiones posteriores:

| Build (prefijo) | Causa |
|---|---|
| `192e…` | sustitución inválida `${DIGEST}` |
| `0eb…` | imagen distroless de Syft sin `sh` |
| `adef…` | vulnerabilidades críticas detectadas por el escáner |
| `5b08…` | `CVE-2023-45853` |
| `1514…` | 403 en `storage.objects.list` al generar/copiar SBOM |
| `5a0e…` | smoke test con `ModuleNotFoundError: fastapi` |
| `34ad…` | 403 en `storage.objects.get` |

También hubo fallos de arranque y readiness en revisiones antiguas del worker/productor. Las revisiones actuales las reemplazaron y no muestran HTTP 4xx/5xx de servicio en la ventana relevante, salvo pruebas manuales antiguas de endpoints de health.

## Plan de solución priorizado

### P0 — Contener y corregir antes de nuevos `generate_response`

#### 1. Evitar más efectos externos sin checkpoint durable

Mientras se construye y valida el fix, desviar temporalmente la ruta `generate_response` a una alternativa segura y conocida —por ejemplo `knowledge_only`, shadow o legacy, según las opciones soportadas por el commit exacto— mediante el mecanismo aprobado de Terraform/workflow. No aplicar un cambio manual sin registrar su rollback.

**Criterio:** ningún ticket nuevo debe ejecutar ForUsBots si el sistema no puede persistir de forma segura el resultado y el identificador externo.

#### 2. No reintentar automáticamente los ocho jobs

ForUsBots tuvo submit y poll exitosos en los ocho. Antes de cualquier replay:

1. correlacionar cada Job ID, Trace ID y timestamp con los registros/auditoría de ForUsBots;
2. determinar si ya hubo modificación externa;
3. recuperar el ID externo si existe;
4. cerrar manualmente o reanudar desde un checkpoint seguro;
5. reintentar únicamente con aprobación y garantía de idempotencia.

Los payloads tienen retención limitada; conservar solo la evidencia mínima necesaria y tratarla como sensible.

#### 3. Corregir la representación Firestore

Reemplazar los pares anidados por una estructura admitida:

```python
det_by_slug: dict[str, list[dict[str, str]]] = {}
# ...
det_by_slug[str(item.get("field"))] = [
    {"module": module_name, "field": field_name}
    for module_name, field_name in entries
]
```

También es válido retirar ese diagnóstico durable si no tiene consumidor. No convertirlo en una cadena ambigua sin revisar sus lectores.

#### 4. Mover la seguridad durable antes del efecto externo

- validar antes de llamar ForUsBots toda estructura de diagnóstico ya construida que después se destinará a Firestore;
- volver a validar el resultado durable completo inmediatamente antes de persistirlo;
- conservar y verificar el checkpoint de intención existente (`forusbots_submit_intent`) antes del submit, sin que el resultado posterior lo sobrescriba;
- guardar inmediatamente el ID externo después del submit;
- no sobrescribir ese ID al guardar el resultado final;
- soportar reconciliación explícita del estado incierto.

#### 5. Instrumentar el punto de fallo

Separar y medir las fases:

- `handle_inquiry`;
- `convert_outcome`;
- `validate_durable_document`;
- `persist_inquiry_result`;
- `mark_terminal`.

En errores, registrar solo: clase sanitizada, fase, código gRPC/HTTP permitido, fingerprint, tamaño estimado, profundidad y contador de arrays anidados. Usar stack trace en el backend restringido, sin serializar ticket, prompt, respuesta ni texto de excepción potencialmente sensible.

#### 6. Añadir pruebas que reproduzcan producción

Pruebas mínimas obligatorias:

- unit test de `_map_fields` que garantice una estructura Firestore-safe;
- unit test del validador recursivo que rechace arrays directamente anidados;
- test completo de `_entry_from_outcome` con mapeos determinísticos, artículos y chunks realistas;
- integración con Firestore Emulator del `record_inquiry_result` completo;
- test del worker que demuestre que un GR válido no cae a `INTERNAL_ERROR`;
- test de replay/idempotencia después de submit externo y antes del checkpoint;
- prueba sintética E2E en una revisión sin tráfico.

### P1 — Corregir clasificación, alertas y operación

#### 7. Separar `UnsafeRetrievalQuery` de fallos de Pinecone

Crear un código público específico, estable y no reintentable, o enrutar a NMI/legacy/humano según el contrato. Reservar `PINECONE_TRANSIENT_FAILURE` para timeout, transporte, 429, 5xx y circuit breaker atribuibles al proveedor.

Añadir tests para ambos caminos y verificar que un rechazo de privacidad nunca incremente la métrica de disponibilidad de Pinecone.

#### 8. Alertar por resultado de negocio

- tasa de `failed` y `partial` sobre terminales;
- conteo de `INTERNAL_ERROR` por ruta;
- `manual_reconciliation_required`;
- trabajos no terminales fuera de SLA;
- razón entre aceptados y terminales;
- 5xx del worker como señal complementaria, no principal.

#### 9. Eliminar doble logging y doble conteo

Elegir una salida estructurada canónica. Actualizar las métricas para filtrar esa representación, desplegar y comparar durante una ventana controlada:

```text
eventos canónicos observados == puntos de métrica creados
```

#### 10. Habilitar trazabilidad de Cloud Tasks

Activar logging con un sampling/costo acordado y enlazar task name, Job ID y Trace ID. No registrar payloads.

#### 11. Acotar el polling de n8n

Definir deadline global, número máximo de intentos, backoff y ramas terminales explícitas para `failed`, `partial`, `expired` y `manual_reconciliation_required`.

### P2 — Eliminar drift y deuda operativa

#### 12. Reconciliar Terraform con producción

- importar o codificar queue, scheduler, service accounts y bindings manuales;
- conservar el filtro `value > 0` de las métricas del reconciliador;
- fijar imágenes por digest;
- revisar que un plan no revierta configuración live válida;
- documentar quién es dueño de cada recurso y su rollback.

#### 13. Ajustar el reconciliador

- cambiar la consulta Firestore a `filter=` para eliminar la advertencia;
- medir tiempo de aplicación separado de provisioning;
- reducir frecuencia, aplicar single-flight o migrar a un disparador por evento si el SLA lo permite;
- asegurar que los lotes no se solapen de forma útilmente evitable.

#### 14. Alinear el repositorio y la procedencia del despliegue

- crear un worktree limpio desde el `main` actual;
- verificar el diff contra `83965cb`;
- portar el fix y sus pruebas sin mezclar cambios locales del usuario;
- construir una imagen reproducible y registrar commit, build ID y digest;
- desplegar primero una revisión sin tráfico y luego canary;
- prohibir despliegues desde la rama local obsoleta.

#### 15. Fortalecer ForUsBots

- endpoint HTTPS o privado con identidad de servicio;
- claves de idempotencia;
- contrato de consulta/reconciliación por ID externo;
- deadline y estados terminales definidos;
- health/SLI y alertas propias.

## Secuencia recomendada de despliegue

1. Crear rama/worktree limpio desde la fuente vigente.
2. Implementar estructura Firestore-safe, validador e instrumentación.
3. Ejecutar unit tests e integración con Firestore Emulator.
4. Construir por Cloud Build y fijar el digest resultante.
5. Desplegar una revisión del worker sin tráfico.
6. Ejecutar un ticket sintético de `generate_response` y verificar el documento durable.
7. Habilitar canary pequeño, observando estados terminales y no solo HTTP.
8. Ampliar tráfico tras una ventana sin `INTERNAL_ERROR`.
9. Reconciliar manualmente los ocho casos históricos, sin replay automático.
10. Corregir luego la clasificación de retrieval, logging, métricas, n8n y reconciliador.

## Criterios de aceptación

El incidente puede considerarse resuelto cuando:

- un resultado GR con mapeo determinístico se persiste en Firestore Emulator y en una revisión sintética;
- no existe ningún array directamente dentro de otro en documentos durables;
- el ID/estado de ForUsBots queda persistido antes de cualquier punto en el que un retry pueda duplicar efectos;
- al menos 20 ejecuciones GR consecutivas terminan sin `INTERNAL_ERROR` y con correlación completa;
- `UnsafeRetrievalQuery` deja de contabilizarse como indisponibilidad de Pinecone;
- el número de eventos terminales canónicos coincide con los puntos de las métricas;
- las alertas detectan un fallo de negocio aunque el worker responda 200;
- los ocho casos existentes tienen resolución documentada individual y no fueron reintentados a ciegas;
- Terraform reproduce la configuración live sin revertir filtros ni recursos manuales.

## Consultas de verificación sugeridas

Estos comandos son de lectura; deben ejecutarse con el proyecto correcto y filtros temporales explícitos:

```bash
gcloud config get-value project

gcloud run services describe kb-rag-system \
  --region us-central1 \
  --project rag-kb-system \
  --format=json

gcloud run services describe kb-rag-ticket-worker \
  --region us-central1 \
  --project rag-kb-system \
  --format=json

gcloud run jobs executions list \
  --job ticket-reconciler-prod \
  --region us-central1 \
  --project rag-kb-system \
  --format=json

gcloud tasks queues describe ticket-jobs-prod \
  --location us-central1 \
  --project rag-kb-system \
  --format=json

gcloud scheduler jobs describe ticket-reconciler-prod-tick \
  --location us-central1 \
  --project rag-kb-system \
  --format=json
```

Para correlacionar un caso, usar Job ID y Trace ID en Cloud Logging y limitar la salida a metadatos sanitizados. No exportar `request`, `response`, prompts ni payloads completos a un issue o documento compartido.

## Conclusión

El sistema de transporte está operativo, pero la ruta `generate_response` está funcionalmente rota en la muestra observada: **0 de 8 intentos terminó bien**. El defecto más probable es una estructura de diagnóstico incompatible con Firestore que falla después de producir el efecto externo. La primera prioridad no es aumentar reintentos, sino impedir duplicados, corregir el documento durable, conservar los IDs externos y recuperar observabilidad en el punto exacto de persistencia.

Los dos estados parciales requieren una corrección distinta: reclasificar `UnsafeRetrievalQuery` para no culpar a Pinecone. El reconciliador, el drift de Terraform, la duplicación de logs, el polling infinito de n8n y el endpoint legacy de ForUsBots son riesgos reales, pero secundarios respecto al defecto de persistencia.
