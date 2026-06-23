# Etapa 2 — Config y modelos Pydantic

> Prerrequisito: leer `00-context.md`. Etapa **independiente** y foundational (sin lógica).

## Objetivo
Agregar todos los settings nuevos y los modelos de request/response del endpoint. No hay flujo
todavía; solo el esqueleto de datos y configuración que el resto de etapas consume.

## Archivos a modificar
- `kb-rag-system/api/config.py`
- `kb-rag-system/api/models.py`

## 1) `config.py` — nuevos `Settings`
Agregar al bloque de `Settings` (cerca de `LLM_ROUTE_*` en L44-49 y `ROUTER_MODE` en L51-59):

```python
# ForusBots
FORUSBOTS_BASE_URL: str = "https://forusbots-6jyh.onrender.com"
FORUSBOTS_AUTH_TOKEN: str = ""               # header x-auth-token
FORUSBOTS_POLL_INTERVAL_S: float = 3.0
FORUSBOTS_POLL_BACKOFF: float = 1.3
FORUSBOTS_POLL_MAX_INTERVAL_S: float = 10.0
FORUSBOTS_MAX_WAIT_S: float = 200.0
FORUSBOTS_HTTP_READ_TIMEOUT_S: float = 15.0
FORUSBOTS_MAX_INFLIGHT: int = 2
FORUSBOTS_RESULT_CACHE_TTL_S: int = 180

# Nuevos routes LLM (LLM-first: 4 agentes internos)
LLM_ROUTE_EXTRACT_INQUIRIES: str = "gpt-5.5"
LLM_ROUTE_KB_QUESTION_SYNTHESIS: str = "gpt-5.5"
LLM_ROUTE_FORUSBOTS_FIELD_MAP: str = "gpt-5.5"
LLM_ROUTE_GR_BODY_BUILD: str = "gpt-5.5"

# Orquestador de tickets
TICKET_HANDLER_MODE: str = "disabled"        # disabled|shadow|knowledge_only|full
TICKET_INQUIRY_BUDGET_S: float = 300.0
TICKET_TOTAL_BUDGET_S: float = 480.0
TICKET_JOB_TTL_S: int = 1800
TICKET_MAX_RELATED: int = 3
RATE_LIMIT_HANDLE_TICKET: int = 20
```

En `validate_settings` (L98-151):
- Agregar los 4 nuevos `LLM_ROUTE_*` al dict `route_models` (validación de prefijo de provider).
- Agregar check: si `TICKET_HANDLER_MODE != "disabled"` y `not FORUSBOTS_AUTH_TOKEN` → error/warn
  (sin token no se puede scrapear). Mantenerlo como warning para que los otros endpoints booteen.
- Validar `TICKET_HANDLER_MODE` ∈ {disabled, shadow, knowledge_only, full}.

## 2) `models.py` — nuevos modelos (agregar al final, ~L619)

```python
class TicketInput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    username: str
    user_email: str
    email_subject: str
    email_body: Optional[str] = None
    # forward-compat opcionales (la lógica LLM-first NO depende de ellos hoy):
    ticket_messages: Optional[Dict[str, str]] = None
    tag: Optional[str] = None
    ticket_id: Optional[str] = None
    first_contact: Optional[bool] = None

class HandleTicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    participant_id: str
    plan_id: str
    company_name: str
    company_status: str
    company_status_detail: Optional[str] = None
    ticket: TicketInput
    record_keeper: Optional[str] = None
    max_response_tokens: int = Field(default=5500, ge=500, le=5500)
    ticket_handler_mode: Optional[Literal["disabled","shadow","knowledge_only","full"]] = None
    idempotency_key: Optional[str] = None

class RouteTaken(str, Enum):
    KNOWLEDGE_QUESTION = "knowledge_question"
    GENERATE_RESPONSE = "generate_response"
    NEEDS_MORE_INFO = "needs_more_info"

class InquiryResult(BaseModel):
    inquiry: str
    topic: str
    record_keeper: Optional[str] = None
    plan_type: str
    route: RouteTaken
    scrape_status: Optional[str] = None      # ok|partial|failed|timeout (solo GR)
    knowledge_answer: Optional[KnowledgeQuestionResponse] = None
    generate_response: Optional[GenerateResponseResult] = None
    needs_more_info_message: Optional[str] = None
    diagnostics: Dict[str, Any] = Field(default_factory=dict)

class TicketHandleResponse(BaseModel):     # respuesta inline (rutas rápidas)
    route_taken: RouteTaken
    primary: InquiryResult
    related: List[InquiryResult] = Field(default_factory=list)
    total_inquiries_in_ticket: int
    metadata: Dict[str, Any] = Field(default_factory=dict)

class TicketJobHandle(BaseModel):          # respuesta 202 (ruta lenta)
    ticket_job_id: str
    state: str                              # queued|running
    poll_url: str
    estimate: Dict[str, Any] = Field(default_factory=dict)

class TicketStatusResponse(BaseModel):     # GET /tickets/{id}
    ticket_job_id: str
    state: str                              # running|succeeded|partial|failed|timeout
    route_taken: Optional[RouteTaken] = None
    primary: Optional[InquiryResult] = None
    related: List[InquiryResult] = Field(default_factory=list)
    forusbots_job_ids: List[str] = Field(default_factory=list)
    elapsed_s: Optional[float] = None
    error: Optional[str] = None
```

Reusar imports/clases existentes: `KnowledgeQuestionResponse` (L503), `GenerateResponseResult`
(L320). Imitar el `field_validator` de `record_keeper` de `RequiredDataRequest` (L84-91) para
strip/empty→None.

## Definition of Done
- [ ] `from api.config import settings` expone los nuevos campos con defaults.
- [ ] `validate_settings()` valida los nuevos routes y `TICKET_HANDLER_MODE`.
- [ ] Los modelos importan y validan (probar instanciación de `HandleTicketRequest` con un payload
      de ejemplo subject+body).
- [ ] `extra="forbid"` en `HandleTicketRequest` rechaza campos desconocidos; `TicketInput` con
      `extra="ignore"` tolera campos extra.

## Cómo verificar
`python -c "from api.models import HandleTicketRequest; HandleTicketRequest(participant_id='1',
plan_id='2', company_name='X', company_status='Ongoing', ticket={'username':'A','user_email':'a@b.c',
'email_subject':'401k','email_body':'quiero retirar'})"` desde `kb-rag-system/`.
