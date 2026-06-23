# Task — Knowledge-question synthesis is ticket-scoped, not inquiry-scoped (cross-contamination)

> Follow-up surfaced by the F3 verification (see `kb-rag-system/rag-testing/eval_reports/f3_eval_2026-06-23.md`,
> §10 caveat and F-OBS-2). The F3 split guard is fixed; this is a **separate** downstream issue and is
> **not yet implemented** — this doc opens it as the next task.

## 1. Problem

When a single ticket yields **two inquiries that both route to `knowledge_question`** (e.g. a financial
request + a security/access blocker), both inquiries get the **same** synthesized KB question and therefore
the same answer — the second topic dominates and the other half is effectively unanswered.

**Observed (post-F3-fix, f3-07):** the ticket *"First, roll over my old 401(k)… Second, I can't log into my
account — credentials are invalid."* now splits correctly into `rollover` + `account_access` (2 inquiries),
but **both** inquiries' `synthesized_question` are about the login/credentials problem; the `rollover`
inquiry never gets a rollover answer. Same family as f3-03, where the `contribution_change` inquiry's answer
was dominated by MFA (`right_answer_wrong_reason=true`, financial_handling=58).

## 2. Root cause

`kb-rag-system/data_pipeline/ticket_orchestrator.py` → `_handle_kq(self, ext, req, classification)` builds the
synthesis input from the **whole ticket**, ignoring the specific inquiry being handled:

```python
agent_input = {"ticketData": self._build_ticket_data(req)}   # full email_subject + email_body + messages
system, user = prompts.build_kb_question_synthesis_prompt(agent_input)
...
question = (parsed or {}).get("question")
kq = await self.deps.rag_engine.ask_knowledge_question(question=question)
```

`build_kb_question_synthesis_prompt` (`data_pipeline/prompts.py:1039`) only receives `{"ticketData": {...}}` —
there is no `ext.inquiry` in the input. So for every KQ-routed inquiry of the same ticket the model sees the
identical full-ticket text and converges on the dominant topic. (The GR path does NOT have this bug: `_handle_gr`
is inquiry-driven via `get_required_data(inquiry=ext.inquiry, …)`.)

## 3. Proposed fix (recommended: orchestrator-only, no prompt/parity change)

Make the synthesis **inquiry-scoped** by feeding the specific inquiry as the focus while keeping ticket context.
Minimal change in `_handle_kq`, no prompt edit (so `test_prompt_parity.py` stays green — `kb_question_synthesis`
is a parity-locked prompt mapped to `External agents/Knowledge Question Inquiry Generator.md`):

- Build a focused ticketData whose `emailBody` is `ext.inquiry` (the clean third-person paraphrase the extractor
  already produced for THIS inquiry), e.g.:
  ```python
  focused = {**self._build_ticket_data(req), "emailBody": ext.inquiry, "emailSubject": ""}
  agent_input = {"ticketData": focused}
  ```
  This points the synthesizer at the one inquiry instead of the whole ticket. Keep the original subject only if
  it adds signal; for `_FORM_SUBMISSION_SUBJECT` it is already blanked.

### Alternative (more robust, touches prompt + parity)
Add an explicit `inquiry`/`focusInquiry` field to `agent_input` and update **both** `agent_prompts/kb_question_synthesis.md`
**and** `External agents/Knowledge Question Inquiry Generator.md` byte-identically (parity) to instruct the model to
synthesize a question for THAT inquiry using the ticket as context. Higher fidelity, but more surface area.

Start with the orchestrator-only fix; escalate to the prompt-aware version only if focusing on `ext.inquiry`
loses needed context.

## 4. Tests (add to `tests/test_ticket_orchestrator.py`)
- Multi-inquiry ticket where both inquiries route to `knowledge_question`: assert each inquiry's synthesized
  question / answer is **on its own topic** (the financial one is not about login, and vice-versa). Use `LLMStub`
  to return a per-`task_type` canned response and assert the `user` prompt passed to `kb_question_synthesis`
  contains the focused inquiry text (the stub records `user_prompts[task_type]`).
- Single-inquiry KQ: unchanged behavior (regression guard).
- f3-07 / f3-03 shaped end-to-end check (optional, live): financial inquiry answer is topical.

## 5. Verification
```
cd kb-rag-system && venv/bin/python -m pytest tests/test_ticket_orchestrator.py tests/test_prompt_parity.py -q
```
End-to-end (temp instance, NOT :8000): re-run f3-07 and f3-03; expect the financial inquiry's
`synthesized_question`/answer to be on its own topic, not the access/MFA topic.

## 6. Notes
- Scope: `_handle_kq` only. Do not touch the F3 guard.
- Severity: medium (correctness of the non-dominant inquiry in multi-KQ tickets); the split itself already works.
