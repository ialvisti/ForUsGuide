# Stage 4 — API Endpoint

## Goal

Wire `InquiryRouterEngine` into FastAPI as `POST /api/v1/route-inquiry`. After this stage the endpoint exists in production builds but n8n is **not yet calling it** — that switch happens in Stage 6.

Depends on Stages 1, 2, 3.

## What to do

### 1. Construct the engine in `lifespan`

In [kb-rag-system/api/main.py](../kb-rag-system/api/main.py), inside the `lifespan` context manager (around line 100, after `app.state.llm_router = llm_router`):

```python
from data_pipeline.inquiry_router import InquiryRouterEngine
...
app.state.inquiry_router = InquiryRouterEngine(llm_router=llm_router)
```

### 2. Add a dependency injector

Mirror `get_rag_engine` ([main.py:197](../kb-rag-system/api/main.py#L197)):

```python
def get_inquiry_router(request: Request) -> InquiryRouterEngine:
    return request.app.state.inquiry_router
```

### 3. Add the endpoint

Place after the existing `knowledge_question_endpoint` (after [main.py:569](../kb-rag-system/api/main.py#L569)):

```python
@app.post(
    "/api/v1/route-inquiry",
    response_model=RouteInquiryResponse,
    dependencies=[Depends(verify_api_key)],
    tags=["RAG Endpoints"],
)
async def route_inquiry_endpoint(
    request: RouteInquiryRequest,
    http_request: Request,
    router_engine: InquiryRouterEngine = Depends(get_inquiry_router),
    rag_engine: RAGEngine = Depends(get_rag_engine),
    exec_logger: Optional[ExecutionLogger] = Depends(get_execution_logger),
):
    """
    Endpoint 4: Classify an inquiry to choose the right downstream endpoint.

    Default returns the routing decision only. With `delegate=true`, also
    invokes the chosen endpoint in-process and returns the result inline.
    """
    start = time.monotonic()
    try:
        result = await router_engine.classify(
            inquiry=request.inquiry,
            record_keeper=request.record_keeper,
            plan_type=request.plan_type,
            topic=request.topic,
            collected_data=request.collected_data,
        )

        suggested_endpoint, suggested_payload = _build_suggested_call(request, result.route)

        delegated_result = None
        if request.delegate:
            delegated_result = await _delegate(
                route=result.route,
                payload=suggested_payload,
                rag_engine=rag_engine,
            )

        response = RouteInquiryResponse(
            route=result.route,
            confidence=result.confidence,
            reasoning=result.reasoning,
            signals=result.signals,
            suggested_endpoint=suggested_endpoint,
            suggested_payload=suggested_payload,
            delegated_result=delegated_result,
            metadata={
                **result.metadata,
                "fast_path_hit": result.fast_path_hit,
                "router_mode": settings.ROUTER_MODE,
            },
        )

        if exec_logger:
            duration_ms = (time.monotonic() - start) * 1000
            await exec_logger.log_execution(
                request_id=getattr(http_request.state, "request_id", "unknown"),
                endpoint="route_inquiry",
                duration_ms=duration_ms,
                request_data=request.model_dump(),
                response_data=response.model_dump(),
            )

        return response

    except Exception as e:
        if exec_logger:
            duration_ms = (time.monotonic() - start) * 1000
            await exec_logger.log_execution(
                request_id=getattr(http_request.state, "request_id", "unknown"),
                endpoint="route_inquiry",
                duration_ms=duration_ms,
                request_data=request.model_dump(),
                response_data={},
                error=str(e),
            )
        logger.exception("Error in route_inquiry endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while routing the inquiry.",
        )
```

### 4. Helper: build `suggested_endpoint` + `suggested_payload`

```python
def _build_suggested_call(request: RouteInquiryRequest, route: str) -> tuple[str, Dict[str, Any]]:
    if route == "knowledge_question":
        return "/api/v1/knowledge-question", {"question": request.inquiry}
    if route == "generate_response":
        return "/api/v1/generate-response", {
            "inquiry": request.inquiry,
            "record_keeper": request.record_keeper,
            "plan_type": request.plan_type,
            "topic": request.topic,
            "collected_data": request.collected_data or {},
        }
    # needs_more_info -> caller should run the existing required-data flow first
    return "/api/v1/required-data", {
        "inquiry": request.inquiry,
        "record_keeper": request.record_keeper,
        "plan_type": request.plan_type,
        "topic": request.topic,
    }
```

### 5. Helper: delegate to downstream in-process

```python
async def _delegate(route: str, payload: Dict[str, Any], rag_engine: RAGEngine) -> Dict[str, Any]:
    if route == "knowledge_question":
        result = await rag_engine.ask_knowledge_question(question=payload["question"])
        return result.__dict__  # or build a dict explicitly
    if route == "generate_response":
        # Validate that required fields are present; if not, fail the delegate
        # cleanly and let the caller handle the gap.
        if not payload.get("topic") or not payload.get("plan_type"):
            return {"error": "delegate_skipped", "reason": "missing topic or plan_type"}
        result = await rag_engine.generate_response(**payload)
        return result.__dict__
    # needs_more_info: do not auto-delegate; let n8n run the legacy flow
    return {"error": "delegate_skipped", "reason": "needs_more_info requires required-data first"}
```

### 6. Surface `ROUTER_MODE` in `/health`

In the existing `health_check` ([main.py:280](../kb-rag-system/api/main.py#L280)), add `router_mode=settings.ROUTER_MODE` to the response. Update `HealthResponse` accordingly.

## Files modified

- [kb-rag-system/api/main.py](../kb-rag-system/api/main.py) — endpoint + helpers + `/health` field

## Verification

1. `pytest kb-rag-system/tests -v` — existing suite must still pass; Stage 5 will add coverage for the new endpoint.
2. Local smoke:
   ```bash
   curl -X POST http://localhost:8000/api/v1/route-inquiry \
     -H "X-API-Key: $API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"inquiry":"Hi there I was wondering how many business days til I can see it get approved. Thank you"}'
   ```
   Expected: `route=knowledge_question`, `confidence>=0.7`, `suggested_endpoint=/api/v1/knowledge-question`.
3. With `delegate=true` on the same inquiry, `delegated_result.answer` should be a concrete timeframe pulled from the KB.
4. OpenAPI docs at `/docs` should now list four RAG endpoints, including `route-inquiry`.

## Done when

- `POST /api/v1/route-inquiry` is live behind `X-API-Key`.
- `delegate=true` works for `knowledge_question` and `generate_response`; returns `delegate_skipped` for `needs_more_info`.
- `/health` reports `router_mode`.
- All existing tests pass.
