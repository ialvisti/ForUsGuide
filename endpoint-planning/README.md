# Inquiry Router Endpoint — Implementation Plan

A new endpoint, `POST /api/v1/route-inquiry`, that classifies an inbound inquiry and tells n8n which downstream endpoint should handle it (`knowledge-question`, `generate-response`, or "fall back to today's flow").

The plan is split into stages. Each stage is independently mergeable — earlier stages don't change runtime behavior, so they can ship one at a time without coordinating with n8n.

## Stages

| # | File | What ships | Behavior change? |
|---|---|---|---|
| 0 | [00-context.md](00-context.md) | Motivation, failing case, design decisions | — |
| 1 | [stage-1-refactor-advisory-helper.md](stage-1-refactor-advisory-helper.md) | Extract `_detect_advisory_concepts` into a reusable free function | None (pure refactor) |
| 2 | [stage-2-models-and-config.md](stage-2-models-and-config.md) | Pydantic models, env vars, LLM router task type, prompts | None (unused code) |
| 3 | [stage-3-classifier-engine.md](stage-3-classifier-engine.md) | `InquiryRouterEngine` (deterministic features + fast-path + LLM) | None (not wired) |
| 4 | [stage-4-api-endpoint.md](stage-4-api-endpoint.md) | `POST /api/v1/route-inquiry` wired in `main.py` | Endpoint live, but n8n isn't calling it |
| 5 | [stage-5-tests.md](stage-5-tests.md) | Unit tests + stress accuracy suite + confusion matrix | — |
| 6 | [stage-6-rollout.md](stage-6-rollout.md) | n8n integration + `ROUTER_MODE` shadow → knowledge-only → full | Production traffic shifts |

## Recommended sequence

Stages 1 → 5 can land back-to-back behind `ROUTER_MODE=disabled`. Stage 6 is the only one that touches production traffic and should be paced over ~2 weeks of observation.

## Quick reference: design decisions (locked)

- **3 routes**: `knowledge_question` | `generate_response` | `needs_more_info`.
- **Decision-only by default**, optional `delegate=true` for one-shot dispatch.
- **Hybrid classifier**: deterministic features + small LLM call, with regex fast-path.
- **Confidence < 0.55 → `needs_more_info`** (safe fallback).
- **Staged rollout** behind `ROUTER_MODE` env flag.

Full reasoning in [00-context.md](00-context.md).
