# Stage 2 — Models, Config, and Prompts

## Goal

Land all the **inert scaffolding** for the router: Pydantic request/response models, env var settings, LLM router task type, and the classifier prompt template. None of this changes runtime behavior — the new symbols are unused until Stage 3 wires them up.

## What to do

### 1. Add Pydantic models

In [kb-rag-system/api/models.py](../kb-rag-system/api/models.py), append after line 528:

```python
class RouteDecision(str, Enum):
    KNOWLEDGE_QUESTION = "knowledge_question"
    GENERATE_RESPONSE = "generate_response"
    NEEDS_MORE_INFO = "needs_more_info"


class RouteInquiryRequest(BaseModel):
    inquiry: str = Field(..., min_length=10, max_length=1000)
    record_keeper: Optional[str] = None
    plan_type: Optional[str] = None
    topic: Optional[str] = None
    collected_data: Optional[Dict[str, Any]] = None
    delegate: bool = False  # if true, also invoke the chosen downstream
    # Validators mirror GenerateResponseRequest (models.py lines 155-176):
    # - strip + non-empty inquiry
    # - normalize record_keeper (empty string -> None)
    # - lowercase topic


class RouteInquiryResponse(BaseModel):
    route: RouteDecision
    confidence: float = Field(..., ge=0.0, le=1.0)
    reasoning: str
    signals: Dict[str, Any]                  # deterministic features that drove the decision
    suggested_endpoint: str                  # "/api/v1/knowledge-question" etc.
    suggested_payload: Dict[str, Any]        # ready-to-send body for the downstream
    delegated_result: Optional[Dict[str, Any]] = None  # populated only when delegate=True
    metadata: Dict[str, Any]
```

### 2. Add config / env vars

In [kb-rag-system/api/config.py](../kb-rag-system/api/config.py):

- Around line 48 (next to `LLM_ROUTE_KNOWLEDGE`):
  ```python
  LLM_ROUTE_CLASSIFY: str = "gpt-5.5-mini"
  ```
- New router-mode flag:
  ```python
  ROUTER_MODE: str = "disabled"  # disabled | shadow | knowledge_only | full
  ```
- Update the route-validation block (lines ~109-115) to include `LLM_ROUTE_CLASSIFY` so settings validation doesn't drop it.
- Surface `ROUTER_MODE` in the `/health` payload (Stage 4 will actually use it).

### 3. Register the new LLM task

In [kb-rag-system/data_pipeline/llm_router.py](../kb-rag-system/data_pipeline/llm_router.py):

- Add to the `route_map` (around line 422):
  ```python
  "classify_inquiry": settings.LLM_ROUTE_CLASSIFY,
  ```
- Add to `_TASK_EFFORT_OVERRIDES` (around line 404) so the classifier runs cheap:
  ```python
  "classify_inquiry": {"reasoning_effort": "minimal", "thinking_budget": 0},
  ```

### 4. Add the classifier prompt

In [kb-rag-system/data_pipeline/prompts.py](../kb-rag-system/data_pipeline/prompts.py), append after line 744:

```python
SYSTEM_PROMPT_CLASSIFY_INQUIRY = """You are an inquiry router for a 401(k) participant advisory system.
Decide which downstream pipeline should handle the inquiry.

ROUTES:
- "knowledge_question": factual/educational questions answerable from the knowledge base alone
  (timeframes, fees, limits, rule definitions). NO participant-specific eligibility needed.
- "generate_response": participant-specific eligibility/outcome questions that need collected
  participant data (hardship qualification, vested-balance, can-I-take-a-loan).
- "needs_more_info": route is ambiguous; topic, recordkeeper, or eligibility intent is unclear.
  This is the safe fallback.

You will receive deterministic signals computed before this call. Treat them as strong hints:
- hardship_signal=true AND active_participant=true -> generate_response
- separation_signal=true AND wants_funds=true -> generate_response
- short interrogative ("how many", "how long", "what is") with no participant signals -> knowledge_question

EXAMPLES:
- "how many business days til I can see it get approved" -> knowledge_question
- "what's the fee for a rollover from LT Trust?" -> knowledge_question
- "what is the 60-day rollover rule?" -> knowledge_question
- "I'm still working but need $15k for medical bills, can I take a hardship?" -> generate_response
- "I left my employer 3 months ago and want to roll over my balance" -> generate_response
- "Can my plan offer Roth contributions?" -> needs_more_info

Output valid JSON:
{"route": "knowledge_question|generate_response|needs_more_info",
 "confidence": 0.0-1.0,
 "reasoning": "one sentence"}"""

USER_PROMPT_CLASSIFY_INQUIRY_TEMPLATE = """INQUIRY: {inquiry}
RECORDKEEPER: {record_keeper}
PLAN_TYPE: {plan_type}
TOPIC: {topic}
PARTICIPANT_DATA_AVAILABLE: {participant_data_available}
DETERMINISTIC_SIGNALS: {signals_json}

Return ONLY the JSON object."""


def build_classify_inquiry_prompt(
    inquiry: str,
    record_keeper: Optional[str],
    plan_type: Optional[str],
    topic: Optional[str],
    participant_data_available: bool,
    signals: Dict[str, Any],
) -> tuple[str, str]:
    user_prompt = USER_PROMPT_CLASSIFY_INQUIRY_TEMPLATE.format(
        inquiry=inquiry,
        record_keeper=record_keeper or "unknown",
        plan_type=plan_type or "unknown",
        topic=topic or "unknown",
        participant_data_available=participant_data_available,
        signals_json=json.dumps(signals, sort_keys=True),
    )
    return SYSTEM_PROMPT_CLASSIFY_INQUIRY, user_prompt
```

Pull additional few-shots from [rag-testing/test_endpoints_stress.py](../kb-rag-system/rag-testing/test_endpoints_stress.py) — the labeled KQ block (~line 1494) and GR block (~line 1520) are pre-classified examples worth incorporating.

## Files modified

- [kb-rag-system/api/models.py](../kb-rag-system/api/models.py)
- [kb-rag-system/api/config.py](../kb-rag-system/api/config.py)
- [kb-rag-system/data_pipeline/llm_router.py](../kb-rag-system/data_pipeline/llm_router.py)
- [kb-rag-system/data_pipeline/prompts.py](../kb-rag-system/data_pipeline/prompts.py)

## Verification

1. `pytest kb-rag-system/tests -v` — all existing tests pass; new symbols are imported (compile check) but otherwise unused.
2. Spin up the API locally and hit `/health` — `ROUTER_MODE: "disabled"` should appear in the payload.
3. Confirm `LLM_ROUTE_CLASSIFY` is settable via env var and survives `validate_settings()`.

## Done when

- All new symbols (`RouteDecision`, `RouteInquiryRequest`, `RouteInquiryResponse`, `LLM_ROUTE_CLASSIFY`, `ROUTER_MODE`, `classify_inquiry` task route, `build_classify_inquiry_prompt`) exist and import cleanly.
- No new endpoint is exposed yet.
- No existing test regresses.
