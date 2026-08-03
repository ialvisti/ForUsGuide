# n8n, ForUsBots, and KB RAG Stability Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stop `participant_search` from polling forever, eliminate the ForUsBots Playwright PID leak, and correct the KB RAG monitoring filters that are generating false incidents.

**Architecture:** Keep the existing asynchronous job APIs and n8n polling shape. Make every poll explicitly distinguish success, failure, and pending states; run the ForUsBots container behind a real PID 1 init process; and make log-based counters count only positive reconciler values. Preserve all unrelated dirty-worktree changes and deploy ForUsBots from an isolated clean build context.

**Tech Stack:** n8n workflow JSON, Node.js 20, Playwright, Docker/Tini, Google Compute Engine MIG, Cloud Run, Cloud Logging, Cloud Monitoring, Python/pytest.

---

### Task 1: Lock the n8n polling contract

**Files:**
- Create: `tests/test_participant_search_workflow.py`
- Modify: `flows_n8n/participant_search.json`

**Step 1: Write the failing tests**

Add tests that load the workflow and require:

- all HTTP calls to ForUsBots to have a finite timeout;
- the two broken retry payloads to expose `jobId`, never `job_Id`;
- every `Get Job` node to route through a failure-state check;
- `failed`, `canceled`, and `cancelled` states to terminate in a Stop And Error node;
- success checks to be null-safe.

**Step 2: Run the tests to verify they fail**

Run:

```bash
python3 -m pytest -q tests/test_participant_search_workflow.py
```

Expected: FAIL on the current retry key, missing failure branches, missing HTTP timeouts, and unsafe success expressions.

**Step 3: Implement the minimal workflow correction**

Update the retry Code nodes to return `jobId`; add a finite timeout to the six ForUsBots HTTP nodes; add one failure check per polling loop; route all terminal failures to a shared Stop And Error node; and make all success expressions tolerate missing response fields.

**Step 4: Run the tests to verify they pass**

Run:

```bash
python3 -m pytest -q tests/test_participant_search_workflow.py
python3 -m json.tool flows_n8n/participant_search.json >/dev/null
```

Expected: PASS and valid JSON.

### Task 2: Prevent Playwright zombies in ForUsBots

**Files:**
- Create: `/Users/ivanalvis/Desktop/ForUsBots/tests/infra/docker-init.test.js`
- Modify: `/Users/ivanalvis/Desktop/ForUsBots/Dockerfile`

**Step 1: Write the failing test**

Add a Node test that requires the production image to install Tini, declare it as `ENTRYPOINT`, and launch `node src/index.js` directly instead of keeping `npm` as PID 1.

**Step 2: Run the test to verify it fails**

Run:

```bash
node --test tests/infra/docker-init.test.js
```

Expected: FAIL because the current image has no init process and uses `npm start`.

**Step 3: Implement the minimal container correction**

Install `tini` in the Playwright image, use `ENTRYPOINT ["/usr/bin/tini", "--"]`, and use `CMD ["node", "src/index.js"]`.

**Step 4: Verify locally**

Run:

```bash
node --test tests/infra/docker-init.test.js
npm run lint
docker build -t forusbots:pid1-fix .
```

Expected: tests, lint, and image build exit successfully.

### Task 3: Correct false-positive KB RAG metrics

**Files:**
- No application file changes; the deployed commits are ahead of this worktree, so the live logging metrics are the authoritative target.

**Step 1: Verify the current failure**

Read recent reconciler metric events and prove that `errors`, `fenced_leases`, and `deadline_terminalized` all have `value=0` while their log-based metrics still match the lines.

**Step 2: Test the proposed filters**

Use Cloud Logging queries with the additional positive-value expression:

```text
textPayload=~"\"value\":[1-9][0-9]*"
```

Expected: zero matches for the healthy recent executions.

**Step 3: Update the three log-based metrics**

Update:

- `ticket_production_reconciler_errors`
- `ticket_production_reconciler_fenced_leases`
- `ticket_production_deadline_terminalized`

Keep their original resource, job, metric, and reason filters and add the positive-value expression.

**Step 4: Verify monitoring state**

Describe the metrics to confirm the new filters, confirm `/health` and `/ready` for the current API and worker revisions, and verify the false incidents close without disabling the alert policies or notification channels.

### Task 4: Deploy and verify ForUsBots safely

**Files:**
- Use an isolated temporary worktree/build context containing committed `main` plus only the Docker PID 1 fix.

**Step 1: Build without unrelated dirty files**

Create an isolated worktree from `origin/main`, apply only the verified Dockerfile/test changes, and submit that context to the existing `cloudbuild.yaml`.

**Step 2: Wait for the MIG rollout**

Verify Cloud Build succeeds and `forusbots-mig` reaches its target template with one healthy instance.

**Step 3: Verify the original production symptom**

Check:

- `/forusbot/health` and `/forusbot/status` return HTTP 200;
- the container has Tini as PID 1;
- zombie count is zero after a Playwright launch/close smoke test;
- a second smoke launch also exits cleanly;
- the container stays healthy and does not restart or report an OOM.

**Step 4: Final regression verification**

Re-run the workflow tests, Docker infrastructure test, JSON validation, relevant ForUsBots tests/lint, KB API health/readiness, open Monitoring incidents, and recent error logs.
