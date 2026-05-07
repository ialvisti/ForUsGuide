# Stage 5 — Tests

## Goal

Lock in classifier behavior with unit tests, exercise the new endpoint with integration tests, and add a stress-test accuracy suite that produces a confusion matrix against today's labeled examples.

Depends on Stages 1-4.

## Unit tests for the classifier engine

New file: [kb-rag-system/tests/test_inquiry_router.py](../kb-rag-system/tests/test_inquiry_router.py)

Test groups:

### `TestDeterministicFeatures`
For each feature, assert it fires on positive cases and stays quiet on negatives.

| Feature | Positive case | Negative case |
|---|---|---|
| `is_short_interrogative` | "how long does this take?" | "I'm wondering about my balance and how it compares to my coworker's situation across multiple plans" |
| `has_first_person_status` | "my balance is $5,000" | "what's the balance threshold?" |
| `has_eligibility_verb` | "can I qualify for hardship?" | "what's the hardship process?" |
| `hardship_signal` | "medical bills" + "withdraw" | "medical bills" alone (no withdraw intent) |
| `loan_signal` | "I want to borrow" | "loan repayment rules" (general) |
| `separation_signal` | "I left my employer" | "what happens when someone leaves?" |

### `TestFastPathRules`
- Punctual short interrogative without participant signals → `knowledge_question` with conf≥0.85.
- Eligibility verb + hardship signal → `generate_response` with conf≥0.85.
- Mixed signals (short interrogative + first-person status) → returns `None` (defers to LLM).

### `TestClassifyLLMPath` (LLM mocked)
- LLM returns `{"route": "knowledge_question", "confidence": 0.8, "reasoning": "..."}` → result reflects it; `fast_path_hit=False`.
- LLM returns conf 0.40 → coerced to `needs_more_info` (below `CONFIDENCE_FALLBACK_THRESHOLD=0.55`); reasoning notes the override.
- LLM returns malformed JSON → `_safe_parse_classifier_json` falls back to `needs_more_info` with conf 0.0.

### `TestRealInquiry` (the originally failing one)
```python
def test_failing_inquiry_routes_to_knowledge_question(engine):
    result = await engine.classify(
        inquiry="Hi there I was wondering how many business days til I can see it get approved. Thank you",
    )
    assert result.route == "knowledge_question"
    assert result.confidence >= 0.7
    assert result.fast_path_hit is True
```

## Integration tests for the endpoint

Extend [kb-rag-system/tests/test_api.py](../kb-rag-system/tests/test_api.py) following the existing fixture pattern (see `client` fixture at line 30 — it already patches `RAGEngine`; add a similar patch for `InquiryRouterEngine`).

Test cases:

- `test_route_inquiry_punctual_question` — short timeframe question → `knowledge_question`, conf > 0.7. Mock `InquiryRouterEngine.classify` to return a fixed result.
- `test_route_inquiry_hardship_with_signals` — hardship inquiry → `generate_response`. Mock the engine.
- `test_route_inquiry_ambiguous` → `needs_more_info`. Mock the engine.
- `test_route_inquiry_delegate_true` → mock `rag_engine.ask_knowledge_question`, assert `delegated_result` is populated and matches the mock's return.
- `test_route_inquiry_delegate_skipped_for_needs_more_info` → assert `delegated_result.error == "delegate_skipped"`.
- `test_route_inquiry_auth_required` — missing `X-API-Key` → 401.
- `test_route_inquiry_validation_short_inquiry` — 9-char inquiry → 422.
- `test_route_inquiry_suggested_payload_shape` — for `generate_response`, payload contains `inquiry`, `record_keeper`, `plan_type`, `topic`, `collected_data`.

## Stress / accuracy suite

In [kb-rag-system/rag-testing/test_endpoints_stress.py](../kb-rag-system/rag-testing/test_endpoints_stress.py):

### 1. Add a caller helper near line 141

```python
def call_route_inquiry(
    inquiry: str,
    record_keeper: Optional[str] = None,
    plan_type: Optional[str] = None,
    topic: Optional[str] = None,
    collected_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    body = {"inquiry": inquiry}
    for k, v in {
        "record_keeper": record_keeper,
        "plan_type": plan_type,
        "topic": topic,
        "collected_data": collected_data,
    }.items():
        if v is not None:
            body[k] = v
    resp = httpx.post(
        f"{API_BASE_URL}/api/v1/route-inquiry",
        headers={"X-API-Key": API_KEY},
        json=body,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()
```

### 2. Build the labeled set

The file already separates KQ test cases (~line 1494) from GR test cases (~line 1520). Reuse them.

```python
LABELED_INQUIRIES = (
    [(case["question"], "knowledge_question") for case in KQ_TEST_CASES]
    + [(case["inquiry"], "generate_response") for case in GR_TEST_CASES]
)
```

### 3. Run the suite

```python
def run_router_accuracy_suite():
    confusion = collections.Counter()
    rows = []
    for inquiry, expected in LABELED_INQUIRIES:
        result = call_route_inquiry(inquiry=inquiry)
        actual = result["route"]
        confusion[(expected, actual)] += 1
        rows.append({
            "inquiry": inquiry[:80],
            "expected": expected,
            "actual": actual,
            "confidence": result["confidence"],
            "fast_path": result["metadata"].get("fast_path_hit"),
            "latency_ms": result["metadata"].get("usage", {}).get("latency_ms"),
        })
    return confusion, rows
```

### 4. Report

Render the confusion matrix into the existing HTML stress report (`stress_test_report.html`). Targets:

- `>= 90%` overall accuracy on the labeled set.
- `<= 5%` of `generate_response` cases misrouted to `knowledge_question` (this is the dangerous direction — we'd lose eligibility checks).
- `p50 < 500ms` on fast-path requests, `p95 < 1.5s` on LLM-path requests.

If the misrouting threshold is exceeded, **do not promote past Stage 6 phase 1** until the prompt or fast-path rules are fixed.

## Files modified

- New: [kb-rag-system/tests/test_inquiry_router.py](../kb-rag-system/tests/test_inquiry_router.py)
- Modified: [kb-rag-system/tests/test_api.py](../kb-rag-system/tests/test_api.py)
- Modified: [kb-rag-system/rag-testing/test_endpoints_stress.py](../kb-rag-system/rag-testing/test_endpoints_stress.py)

## Done when

- `pytest kb-rag-system/tests -v` passes including the new test files.
- The accuracy suite runs against a live API and produces a confusion matrix in the HTML report.
- Latency targets met on the labeled set.
