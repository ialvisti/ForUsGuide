# Stage 3 — Classifier Engine

## Goal

Build the `InquiryRouterEngine` that does the actual classification. Lives in a new module [kb-rag-system/data_pipeline/inquiry_router.py](../kb-rag-system/data_pipeline/inquiry_router.py). Depends on Stage 1 (free `detect_advisory_concepts`) and Stage 2 (prompts + LLM router task).

Still no API endpoint at this stage — the engine is constructable and unit-testable, but nothing calls it from production code paths.

## Module shape

```python
# kb-rag-system/data_pipeline/inquiry_router.py

@dataclass
class ClassificationResult:
    route: str                    # "knowledge_question" | "generate_response" | "needs_more_info"
    confidence: float
    reasoning: str
    signals: Dict[str, Any]
    fast_path_hit: bool           # True when we skipped the LLM
    metadata: Dict[str, Any]      # model, provider, tokens, latency_ms


CONFIDENCE_FALLBACK_THRESHOLD = 0.55


class InquiryRouterEngine:
    def __init__(self, llm_router: LLMRouter):
        self._llm = llm_router

    async def classify(
        self,
        inquiry: str,
        record_keeper: Optional[str] = None,
        plan_type: Optional[str] = None,
        topic: Optional[str] = None,
        collected_data: Optional[Dict[str, Any]] = None,
    ) -> ClassificationResult:
        signals = compute_deterministic_features(inquiry, topic, collected_data)

        fast = apply_fast_path_rules(inquiry, signals)
        if fast is not None:
            return ClassificationResult(
                route=fast.route,
                confidence=fast.confidence,
                reasoning=fast.reasoning,
                signals=signals,
                fast_path_hit=True,
                metadata={"model": None, "provider": None, "latency_ms": fast.latency_ms},
            )

        # LLM path
        system, user = build_classify_inquiry_prompt(
            inquiry=inquiry,
            record_keeper=record_keeper,
            plan_type=plan_type,
            topic=topic,
            participant_data_available=bool(collected_data),
            signals=signals,
        )
        llm_result = await self._llm.call(
            task_type="classify_inquiry",
            system_prompt=system,
            user_prompt=user,
            max_tokens=200,
        )

        parsed = _safe_parse_classifier_json(llm_result.content)
        route = parsed.get("route", "needs_more_info")
        confidence = float(parsed.get("confidence", 0.0))
        reasoning = parsed.get("reasoning", "")

        # Confidence-based fallback
        if confidence < CONFIDENCE_FALLBACK_THRESHOLD:
            route = "needs_more_info"
            reasoning = f"Low confidence ({confidence:.2f}); falling back. Original: {reasoning}"

        return ClassificationResult(
            route=route,
            confidence=confidence,
            reasoning=reasoning,
            signals=signals,
            fast_path_hit=False,
            metadata={
                "model": llm_result.model_used,
                "provider": llm_result.provider_used,
                "usage": llm_result.usage,
            },
        )
```

## Helpers

### `compute_deterministic_features(inquiry, topic, collected_data)`

Thin wrapper around the free `detect_advisory_concepts` from Stage 1. Returns the same dict plus a few classifier-specific computed fields:

```python
def compute_deterministic_features(inquiry, topic, collected_data) -> Dict[str, Any]:
    base = detect_advisory_concepts(
        inquiry=inquiry,
        topic=topic,
        collected_data=collected_data,
        # bind helpers as Stage 1 decided (free fns or shared utils)
    )
    extras = {
        "word_count": len(inquiry.split()),
        "is_short_interrogative": _is_short_interrogative(inquiry),
        "has_first_person_status": _has_first_person_status(inquiry),
        "has_eligibility_verb": _has_eligibility_verb(inquiry),
    }
    return {**base, **extras}
```

The new boolean predicates are simple regex checks:

- `_is_short_interrogative`: word count ≤ 30 AND starts with one of `how long`, `how many`, `how much`, `what is`, `what are`, `what's`, `when`, `where`.
- `_has_first_person_status`: matches `\b(my balance|my employer|my plan|my account|i'?m \d+|i am \d+|i\b(left|quit|terminated))\b`.
- `_has_eligibility_verb`: matches `\b(eligible|qualify|vested|allowed to|can i|am i|do i qualify)\b`.

### `apply_fast_path_rules(inquiry, signals)`

Returns a `FastPathDecision` or `None`. Fast-path is **conservative** — only fires on unambiguous cases.

```python
@dataclass
class FastPathDecision:
    route: str
    confidence: float
    reasoning: str
    latency_ms: float

def apply_fast_path_rules(inquiry, signals) -> Optional[FastPathDecision]:
    start = time.monotonic()

    # Punctual question with no participant signals -> knowledge_question
    if (signals.get("is_short_interrogative")
        and not signals.get("has_first_person_status")
        and not signals.get("has_eligibility_verb")
        and not signals.get("hardship_signal")
        and not signals.get("loan_signal")
        and not signals.get("separation_signal")):
        return FastPathDecision(
            route="knowledge_question",
            confidence=0.9,
            reasoning="Short interrogative with no participant signals.",
            latency_ms=(time.monotonic() - start) * 1000,
        )

    # Strong eligibility intent -> generate_response
    if (signals.get("has_eligibility_verb")
        and (signals.get("hardship_signal")
             or signals.get("loan_signal")
             or signals.get("separation_signal"))):
        return FastPathDecision(
            route="generate_response",
            confidence=0.9,
            reasoning="Eligibility verb plus hardship/loan/separation signal.",
            latency_ms=(time.monotonic() - start) * 1000,
        )

    return None
```

### `_safe_parse_classifier_json(content)`

JSON parse with a try/except. On failure return `{"route": "needs_more_info", "confidence": 0.0, "reasoning": "Classifier output unparseable"}` and log the raw content for debugging. This protects against LLM format drift without crashing the endpoint.

## Files modified

- New: [kb-rag-system/data_pipeline/inquiry_router.py](../kb-rag-system/data_pipeline/inquiry_router.py)

## Verification

Verification is via Stage 5 unit tests. No API surface yet, so end-to-end verification waits for Stage 4.

Sanity check for now:

```python
import asyncio
from data_pipeline.inquiry_router import InquiryRouterEngine
# in a REPL or quick script with a real LLMRouter
result = asyncio.run(engine.classify(
    inquiry="Hi there I was wondering how many business days til I can see it get approved.",
))
assert result.route == "knowledge_question"
assert result.fast_path_hit is True  # short interrogative, no signals
```

## Done when

- `InquiryRouterEngine.classify` returns a `ClassificationResult` for any input.
- Fast-path fires deterministically for clear cases without LLM cost.
- Low-confidence LLM responses are coerced to `needs_more_info`.
- Module is import-clean and type-checked.
