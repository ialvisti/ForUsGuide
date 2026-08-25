# n8n ticket workflow remediation

These files are clipboard-style node/connection exports, not complete n8n
workflow exports. They do not contain workflow identity, activation state,
settings, error-workflow configuration, or execution-retention policy. Treat
them as a reviewed patch source for an **inactive clone**, not as an artifact to
activate directly.

## Changes applied to `main.json`

- Validate the webhook ticket lookup identifier before calling DevRev.
- Establish the final ticket identity from the authenticated `works.get`
  response and fail closed on missing, invalid, or mismatched identity.
- Give the KQ LLM only the content fields needed to synthesize a question.
  Ticket authority never enters or leaves the model.
- Rebuild the `/api/v1/knowledge-question` body from the parsed `question` plus
  the canonical workflow-owned `ticket_id`.
- Derive the direct KQ `Idempotency-Key` from the canonical ticket plus parsed
  question, and retry the HTTP node with that same key.
- Keep `/api/v1/handle-ticket` identity in `ticket.ticket_id`, which is the
  endpoint's supported contract.
- Derive `Idempotency-Key` from a deterministic fingerprint of the canonical
  handle-ticket input so node retries and identical redeliveries reuse a job.
- Start polling after 15 seconds, continue for both `queued` and `running`, and
  publish only `succeeded + send_participant_reply + !fallback` results.
- Route inline KQ/NMI and multi-inquiry results through the route-aware
  formatter instead of the single-primary KQ formatter.
- Keep the superseded single-primary KQ formatter disabled for visual rollback
  context; it is not part of the active graph.
- Remove pinned execution data, n8n instance metadata, unused KQ OAuth binding,
  and literal DevRev authorization headers from the local exports.

## Required before production activation

1. Create a complete inactive clone of the active parent and participant
   workflows, then apply/review this graph there.
2. Confirm the `RAG KB System` HTTP Header Auth credential emits `X-API-Key`;
   keep the Google ID token in `Authorization` for Cloud Run IAM.
3. Revoke and rotate the DevRev bearer exposed in the original snapshot. Bind
   update/comment nodes only to the managed `DevRev Production` credential.
4. Authenticate the public webhook (signed delivery or header/HMAC auth) and
   verify that an event is durably accepted before returning 2xx.
5. Confirm the deployed API contains the direct-KQ idempotency receipt/replay
   contract before enabling this export's KQ retries.
6. Remediate or explicitly isolate the participant child workflow P1s: missing
   terminal outputs, unbounded polls, LLM-owned email lookup, unreachable match
   branch, silent AI-error fallthrough, and participant data over plain HTTP.
7. Run the inactive-clone smoke matrix: wrong LLM ID, missing/invalid identity,
   KQ, GR, NMI, multi-inquiry, queued/running, every terminal state, duplicate
   delivery, invalid auth, and forced immediate-publish recovery.
8. Activate only in the coordinated backend/workflow rollout described in the
   incident plan. The repository changes alone do not fix production.
