# Etapa 4 — TicketOrchestrator

> Prerrequisito: leer `00-context.md` + Etapas 1 (cliente ForusBots), 2 (config/modelos), 3
> (agentes LLM). Aquí se une todo en lógica pura, sin endpoints todavía.

## Objetivo
Un módulo que, dado un `HandleTicketRequest`, ejecute el flujo completo y produzca los resultados
por inquiry. Mantiene `main.py` delgado y es 100% mockeable.

## Archivo a crear
`kb-rag-system/data_pipeline/ticket_orchestrator.py`

## Diseño

```python
@dataclass
class ExtractedInquiry:
    inquiry: str
    record_keeper: Optional[str]
    plan_type: str
    topic: str
    related_inquiries: Optional[List[str]]

@dataclass
class OrchestratorDeps:
    rag_engine: RAGEngine
    inquiry_router: InquiryRouterEngine
    llm_router: LLMRouter
    forusbots: ForusBotsClient
    execution_logger: Optional[ExecutionLogger] = None

class TicketOrchestrator:
    def __init__(self, deps: OrchestratorDeps, settings): ...

    async def extract_inquiries(self, req: HandleTicketRequest) -> List[ExtractedInquiry]:
        # LLM task_type="extract_inquiries"; record_keeper viene de req.record_keeper;
        # plan_type default "401(k)"; topic inferido. Parsear array; [] -> []

    async def handle_inquiry(self, ext: ExtractedInquiry, req: HandleTicketRequest) -> InquiryResult:
        cls = await self.deps.inquiry_router.classify(ext.inquiry)
        if cls.route == "knowledge_question": return await self._handle_kq(req, ext, cls)
        if cls.route == "generate_response":  return await self._handle_gr(ext, req, cls)
        return self._needs_more_info(ext, cls)

    async def _handle_kq(self, req, ext, cls) -> InquiryResult:
        # LLM kb_question_synthesis(ticket); si insufficient_inquiry -> needs_more_info (saludo)
        # else: rag_engine.ask_knowledge_question(question) -> KnowledgeQuestionResponse

    async def _handle_gr(self, ext, req, cls) -> InquiryResult:
        rd = await rag_engine.get_required_data(ext.inquiry, ext.record_keeper, ext.plan_type, ext.topic, ext.related_inquiries)
        flat = _flatten_required_fields(rd.required_fields)          # Dict[str,List[RequiredField]] -> [ {field,...} ]
        mapping = await llm(forusbots_field_map, flat)               # {modules, _unmapped}
        try:
            scrape = await forusbots.scrape_participant(req.participant_id, mapping["modules"])
            scrape_status = "ok"
        except (ForusBotsTimeout, ForusBotsJobFailed) as e:
            scrape, scrape_status = None, ("timeout" if isinstance(e, ForusBotsTimeout) else "failed")
        case_data = _build_case_data(req)
        body = await llm(gr_body_build, ppt_modules=scrape.result if scrape else {}, case_data=case_data, ...)
        gr = await rag_engine.generate_response(ext.inquiry, ext.record_keeper, ext.plan_type, ext.topic,
                                                body["collected_data"], req.max_response_tokens, total_inquiries)
        return InquiryResult(route=generate_response, generate_response=..., scrape_status=scrape_status,
                             diagnostics={mapped_modules, _unmapped, forusbots_job_id, classifier_signals, step_timings})

    async def run_ticket(self, req: HandleTicketRequest) -> List[InquiryResult]:
        extracted = await self.extract_inquiries(req)
        if not extracted: return []                                  # -> needs_more_info en el endpoint
        results = []
        # fan-out acotado por semáforo (ForusBots maxConcurrency global=3): primaria + hasta MAX_RELATED
        for ext in extracted[:1 + settings.TICKET_MAX_RELATED]:
            results.append(await asyncio.wait_for(self.handle_inquiry(ext, req), settings.TICKET_INQUIRY_BUDGET_S))
        return results
```

### Reglas clave
- **degraded-proceed**: si el scrape falla/timeout, igual llamar `generate_response` con
  `collected_data` parcial (vacío si no hubo scrape). El engine ya emite `blocked_missing_data` +
  `questions_to_ask` (rag_engine.py L1075-1098). Etiquetar `scrape_status`.
- **needs_more_info** (router o KQ insufficient): devolver `InquiryResult` con
  `needs_more_info_message` (usar `cls.user_message` o saludo default).
- **Form-Submission**: como defensa extra, si `email_subject == "Participant Advisory - Form
  Submission"`, pre-recortar a solo `email_body` antes de mandar al LLM (cinturón + tirantes sobre
  la regla del prompt).
- **`total_inquiries_in_ticket`** = len(extracted); `related_inquiries` se preservan recíprocos.
- **Acotar latencia**: `MAX_RELATED` y `asyncio.wait_for` por inquiry. El semáforo del cliente
  ForusBots (Etapa 1) acota la concurrencia global de scrapes.
- **Acumular diagnostics** por paso (extract, classify, required_data, scrape jobId/elapsed/queue,
  gr_body_build, generate_response) para que la Etapa 5 los loguee.

## Definition of Done
- [ ] `run_ticket(req)` corre el flujo completo con engines y cliente reales o mockeados.
- [ ] Branching correcto: KQ (incl. insufficient→NMI), GR (incl. scrape fallido→degraded), NMI.
- [ ] Multi-inquiry: array de >1 produce primaria + relacionadas, con linkage recíproco.
- [ ] No lanza 500 ante fallo de ForusBots: degrada y devuelve `InquiryResult` con `scrape_status`.

## Cómo verificar
Tests con `rag_engine`, `inquiry_router`, `forusbots` y `llm_router.call` mockeados (ver Etapa 6):
afirmar la ruta tomada, que GR llamó scrape+generate_response, que el fallo de scrape degrada, y que
diagnostics se pobló. Aún sin endpoint — se prueba el orquestador directo.
