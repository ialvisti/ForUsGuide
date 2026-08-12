"""Browser regression for the verified identity used by review self-assignment.

The HTTP route already proves that a complete ``ReviewerIdentity`` can take an
unassigned review.  This test covers the missing browser seam: the exact session
normalizer that reads ``GET /session`` must preserve the IAP subject consumed by
the exact save-body builder.
"""

from __future__ import annotations

import json
import subprocess

from api.tickets_console_main import UI_ASSETS_DIRECTORY


_NODE_HARNESS = r"""
import fs from "node:fs";

const request = JSON.parse(fs.readFileSync(0, "utf8"));

globalThis.fetch = async (url, options = {}) => {
  if (String(url) !== "/api/admin/v1/session" || (options.method ?? "GET") !== "GET") {
    throw new Error(`unexpected request: ${String(url)}`);
  }
  return new Response(JSON.stringify({
    identity: {
      subject: "accounts.google.com:synthetic-reviewer",
      email: "reviewer@example.invalid",
      display_name: "Synthetic Reviewer",
    },
    role: "reviewer",
    csrf_token: "synthetic-csrf-token",
    csrf_expires_at: "2099-01-01T00:00:00Z",
    feature_flags: {},
  }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
};

// Imported render helpers refer to `document` only when a DOM-producing
// function is called.  The save-body path needs only the locale marker.
globalThis.document = { documentElement: { lang: "en" } };

const api = await import(request.apiUrl);
const evaluation = await import(request.evaluationUrl);
const session = await api.loadSession();
const plan = evaluation.buildSave({
  review: { assigned_reviewer: null },
  draft: { assigned_reviewer: "self" },
  session,
});

process.stdout.write(JSON.stringify({
  session_subject: session.subject,
  assigned_reviewer: plan.patch.assigned_reviewer,
}));
"""

_STATUS_HARNESS = r"""
import fs from "node:fs";

const request = JSON.parse(fs.readFileSync(0, "utf8"));

class FakeNode {
  constructor(tag = "div") {
    this.tag = tag;
    this.children = [];
    this.disabled = false;
    this.hidden = false;
    this.textContent = "";
  }
  get firstChild() {
    return this.children[0] ?? null;
  }
  appendChild(child) {
    this.children.push(child);
    return child;
  }
  removeChild(child) {
    this.children.splice(this.children.indexOf(child), 1);
  }
  setAttribute(name, value) {
    this[name] = value;
  }
}

globalThis.document = {
  documentElement: { lang: "en" },
  createElement: (tag) => new FakeNode(tag),
};

const evaluation = await import(request.evaluationUrl);

function renderFor(role) {
  const dom = {
    statusField: new FakeNode(),
    status: new FakeNode("select"),
    statusHelp: new FakeNode("p"),
  };
  const allowed = evaluation.populateStatuses(dom, {
    review: { status: "reviewed" },
    role,
  });
  return {
    allowed,
    hidden: dom.statusField.hidden,
    disabled: dom.status.disabled,
    help: dom.statusHelp.textContent,
  };
}

process.stdout.write(JSON.stringify({
  reviewer: renderFor("reviewer"),
  admin: renderFor("admin"),
}));
"""

_REMEDIATION_HARNESS = r"""
import fs from "node:fs";

const request = JSON.parse(fs.readFileSync(0, "utf8"));
const summary = { value: "Adjusted retrieval and verified the response." };
globalThis.document = {
  documentElement: { lang: "en" },
  getElementById: (id) => id === "eval-remediation-summary" ? summary : null,
};

const state = await import(request.stateUrl);
const evaluation = await import(request.evaluationUrl);
const checked = [
  { checked: true, value: "rag_code" },
  { checked: true, value: "devrev_prompt" },
];
const dom = {
  ratingGroup: { querySelector: () => null },
  modifiedSurfaces: { querySelectorAll: () => checked },
  remediationRecord: { hidden: true },
};
const form = evaluation.readForm(dom);
const plan = evaluation.buildSave({
  review: { remediation_summary: null, modified_surfaces: [] },
  draft: form,
  session: null,
});

const hiddenAtFive = evaluation.syncRemediationVisibility(dom, {
  review: { remediation_summary: null, modified_surfaces: [] },
  draft: { rating: "5" },
});
const visibleAtFour = evaluation.syncRemediationVisibility(dom, {
  review: { remediation_summary: null, modified_surfaces: [] },
  draft: { rating: "4" },
});
const storedStaysVisible = evaluation.syncRemediationVisibility(dom, {
  review: { remediation_summary: "Recorded fix", modified_surfaces: [] },
  draft: { rating: "5" },
});

process.stdout.write(JSON.stringify({
  form,
  patch: plan.patch,
  saved: state.savedValue(
    { modified_surfaces: ["rag_code", "devrev_prompt"] },
    "modified_surfaces"
  ),
  hiddenAtFive,
  visibleAtFour,
  storedStaysVisible,
}));
"""


def test_verified_session_subject_reaches_the_self_assignment_patch() -> None:
    request = {
        "apiUrl": (UI_ASSETS_DIRECTORY / "api.js").as_uri(),
        "evaluationUrl": (UI_ASSETS_DIRECTORY / "evaluation.js").as_uri(),
    }
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", _NODE_HARNESS],
        check=True,
        capture_output=True,
        input=json.dumps(request),
        text=True,
    )
    result = json.loads(completed.stdout)

    assert result["session_subject"] == "accounts.google.com:synthetic-reviewer"
    assert result["assigned_reviewer"] == {
        "subject": "accounts.google.com:synthetic-reviewer",
        "email": "reviewer@example.invalid",
        "display_name": "Synthetic Reviewer",
    }


def test_only_an_admin_is_offered_the_status_control() -> None:
    request = {
        "evaluationUrl": (UI_ASSETS_DIRECTORY / "evaluation.js").as_uri(),
    }
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", _STATUS_HARNESS],
        check=True,
        capture_output=True,
        input=json.dumps(request),
        text=True,
    )
    result = json.loads(completed.stdout)

    assert result["reviewer"] == {
        "allowed": [],
        "hidden": True,
        "disabled": True,
        "help": "",
    }
    assert result["admin"]["hidden"] is False
    assert result["admin"]["disabled"] is False
    assert "resolved" in result["admin"]["allowed"]


def test_remediation_record_uses_sorted_draft_values_and_rating_visibility() -> None:
    request = {
        "evaluationUrl": (UI_ASSETS_DIRECTORY / "evaluation.js").as_uri(),
        "stateUrl": (UI_ASSETS_DIRECTORY / "state.js").as_uri(),
    }
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", _REMEDIATION_HARNESS],
        check=True,
        capture_output=True,
        input=json.dumps(request),
        text=True,
    )
    result = json.loads(completed.stdout)

    assert result["form"]["modified_surfaces"] == "devrev_prompt,rag_code"
    assert result["patch"] == {
        "remediation_summary": "Adjusted retrieval and verified the response.",
        "modified_surfaces": ["devrev_prompt", "rag_code"],
    }
    assert result["saved"] == "devrev_prompt,rag_code"
    assert result["hiddenAtFive"] is False
    assert result["visibleAtFour"] is True
    assert result["storedStaysVisible"] is True
