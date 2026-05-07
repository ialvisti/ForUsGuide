# Stage 1 — Refactor: Extract Advisory-Concept Detection

## Goal

Make `RAGEngine._detect_advisory_concepts()` (lines 2022-2177 in [rag_engine.py](../kb-rag-system/data_pipeline/rag_engine.py)) reusable from outside the engine. The new `inquiry_router` module needs the same deterministic signals (active vs. terminated participant, hardship_signal, loan_signal, separation_signal, wants_funds, etc.), and we don't want to instantiate a full `RAGEngine` just to call them.

This stage is a **pure refactor** — no behavior change, no new endpoints, no new env vars. It can ship on its own.

## What to do

### 1. Extract the body of `_detect_advisory_concepts` into a module-level free function

In [kb-rag-system/data_pipeline/rag_engine.py](../kb-rag-system/data_pipeline/rag_engine.py):

```python
def detect_advisory_concepts(
    inquiry: str,
    topic: Optional[str],
    collected_data: Optional[Dict[str, Any]],
    *,
    contains_any,                # callable: bound to engine helper
    contains_bounded_phrase,     # callable: bound to engine helper
    resolve_topic_filter,        # callable: bound to engine helper
    ordered_unique,              # callable: bound to engine helper
) -> Dict[str, Any]:
    """Pure-function port of RAGEngine._detect_advisory_concepts.

    Returns the same dict the engine produces today (keys:
    active_participant, wants_funds, separation_signal, hardship_signal,
    loan_signal, force_out_signal, indirect_60_day_signal, rmd_signal,
    contact_or_reference_signal, related_topics, etc.).
    """
    # ... lifted from current _detect_advisory_concepts body, unchanged
```

The four helper callables (`contains_any`, `contains_bounded_phrase`, `resolve_topic_filter`, `ordered_unique`) are private to `RAGEngine` today. Either:

- (a) Pass them in as parameters (shown above) — minimal change, but ugly.
- (b) Promote them to module-level free functions in `rag_engine.py` if they don't depend on `self`. Inspect their bodies — most are pure string utilities and should promote cleanly.

**Recommended:** (b). Promote any of the four that are pure to module level; pass the remainder as parameters. Keep `_detect_advisory_concepts` as a thin method that delegates.

### 2. Keep the existing method as a thin wrapper

```python
def _detect_advisory_concepts(self, inquiry, topic, collected_data):
    return detect_advisory_concepts(
        inquiry=inquiry,
        topic=topic,
        collected_data=collected_data,
        contains_any=self._contains_any,
        contains_bounded_phrase=self._contains_bounded_phrase,
        resolve_topic_filter=self._resolve_topic_filter,
        ordered_unique=self._ordered_unique,
    )
```

This guarantees zero behavior change for the existing `generate-response` path.

## Files modified

- [kb-rag-system/data_pipeline/rag_engine.py](../kb-rag-system/data_pipeline/rag_engine.py) — extract function, retain method as wrapper

## Verification

1. Run the existing engine test suite:
   ```bash
   pytest kb-rag-system/tests/test_rag_engine.py -v
   ```
   All tests must pass unchanged.

2. Run the existing stress suite against `generate-response` and confirm no regression on the labeled GR cases ([rag-testing/test_endpoints_stress.py](../kb-rag-system/rag-testing/test_endpoints_stress.py) line ~1520).

3. Diff `_detect_advisory_concepts` output for a handful of representative inquiries before/after refactor — should be byte-identical.

## Done when

- The free function `detect_advisory_concepts` exists and is importable.
- `RAGEngine._detect_advisory_concepts` calls it and returns the same dict.
- All existing tests pass.
- No new code paths in production yet.
