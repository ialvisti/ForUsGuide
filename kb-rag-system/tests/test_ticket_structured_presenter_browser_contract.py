"""Browser-facing behavior of the lossless structured audit presenter.

The ticket UI intentionally ships as dependency-free browser modules.  These
tests execute those exact modules in Node with a deliberately small DOM rather
than asserting implementation strings.  The fake DOM implements only the
standard primitives the presenter uses; remote content still travels through
``textContent`` and attributes exactly as it does in a browser.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

from api.tickets_console_main import UI_ASSETS_DIRECTORY


_NODE_HARNESS = r"""
import fs from "node:fs";

class FakeClassList {
  constructor(node) { this.node = node; }
  add(...names) {
    const current = new Set(this.node.className.split(/\s+/).filter(Boolean));
    for (const name of names) current.add(name);
    this.node.className = [...current].join(" ");
  }
  contains(name) { return this.node.className.split(/\s+/).includes(name); }
}

class FakeElement {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentElement = null;
    this.className = "";
    this.classList = new FakeClassList(this);
    this.dataset = {};
    this.attributes = new Map();
    this.listeners = new Map();
    this._text = "";
    this.open = false;
    this.disabled = false;
  }
  appendChild(child) {
    child.parentElement = this;
    this.children.push(child);
    return child;
  }
  removeChild(child) {
    const index = this.children.indexOf(child);
    if (index >= 0) this.children.splice(index, 1);
    child.parentElement = null;
    return child;
  }
  get firstChild() { return this.children[0] ?? null; }
  get childNodes() { return this.children; }
  set textContent(value) {
    this._text = String(value ?? "");
    this.children = [];
  }
  get textContent() {
    return this._text + this.children.map((child) => child.textContent).join("");
  }
  setAttribute(name, value) {
    const text = String(value ?? "");
    this.attributes.set(String(name), text);
    if (name === "id") this.id = text;
    if (name === "open") this.open = true;
  }
  getAttribute(name) { return this.attributes.get(String(name)) ?? null; }
  hasAttribute(name) { return this.attributes.has(String(name)); }
  addEventListener(name, handler) {
    const handlers = this.listeners.get(name) ?? [];
    handlers.push(handler);
    this.listeners.set(name, handlers);
  }
  dispatchEvent(event) {
    for (const handler of this.listeners.get(event.type) ?? []) handler.call(this, event);
  }
}

globalThis.document = {
  documentElement: { lang: "en" },
  createElement(tag) { return new FakeElement(tag); },
};

function walk(node) {
  return [node, ...node.children.flatMap(walk)];
}

function openAll(root) {
  const opened = new Set();
  while (true) {
    const next = walk(root).find((node) => node.tagName === "DETAILS" && !opened.has(node));
    if (next === undefined) break;
    opened.add(next);
    next.open = true;
    next.setAttribute("open", "");
    next.dispatchEvent({ type: "toggle", currentTarget: next });
  }
}

function serialized(node) {
  return {
    tag: node.tagName.toLowerCase(),
    text: node.textContent,
    className: node.className,
    attrs: Object.fromEntries(node.attributes),
    dataset: { ...node.dataset },
    children: node.children.map(serialized),
  };
}

const request = JSON.parse(fs.readFileSync(0, "utf8"));
const structured = await import(request.structuredUrl);
const conversation = await import(request.conversationUrl);
const state = await import(request.stateUrl);
let root;
let extra = {};

if (request.scenario === "schema-drift") {
  root = structured.renderGeneratedAnswer(request.value, request.options ?? {});
  const before = serialized(root);
  openAll(root);
  extra = { before, after: serialized(root) };
} else if (request.scenario === "structured-data") {
  root = structured.renderStructuredData(request.value, request.options ?? {});
  const before = serialized(root);
  openAll(root);
  extra = { before, after: serialized(root) };
} else if (request.scenario === "structured-data-initial") {
  root = structured.renderStructuredData(request.value, request.options ?? {});
  extra = { before: serialized(root) };
} else if (request.scenario === "conversation") {
  root = conversation.conversationEntry(request.value, request.options ?? {});
  const before = serialized(root);
  openAll(root);
  extra = { before, after: serialized(root) };
} else if (request.scenario === "conversation-filter") {
  const filtered = state.filterConversation(
    request.value,
    request.options?.filter ?? "messages",
  );
  extra = {
    entry_ids: filtered.map((entry) => entry.entry_id),
    actor_classes: filtered.map((entry) => entry.actor_class),
  };
} else if (request.scenario === "tokens") {
  const tokens = structured.tokenizeStructuredText(request.value);
  extra = { tokens };
} else if (request.scenario === "parse") {
  const parsed = structured.parseStructuredText(request.value);
  extra = {
    parsed: parsed !== null,
    length: Array.isArray(parsed) ? parsed.length : null,
  };
} else if (request.scenario === "limits") {
  extra = { limits: structured.STRUCTURED_LIMITS };
} else {
  throw new Error(`unknown scenario: ${request.scenario}`);
}

process.stdout.write(JSON.stringify(extra));
"""


def _run(scenario: str, *, value: Any = None, options: dict[str, Any] | None = None):
    request = {
        "scenario": scenario,
        "value": value,
        "options": options or {},
        "structuredUrl": (UI_ASSETS_DIRECTORY / "structured.js").as_uri(),
        "conversationUrl": (UI_ASSETS_DIRECTORY / "conversation.js").as_uri(),
        "stateUrl": (UI_ASSETS_DIRECTORY / "state.js").as_uri(),
    }
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", _NODE_HARNESS],
        check=True,
        capture_output=True,
        input=json.dumps(request),
        text=True,
    )
    return json.loads(completed.stdout)


def _walk(node: dict[str, Any]):
    yield node
    for child in node.get("children", []):
        yield from _walk(child)


class TestLosslessGeneratedAnswer:

    def test_only_recorded_prose_is_protected_from_interface_translation(self):
        absent = _run("schema-drift", value=None)["after"]
        absent_paragraph = next(
            node
            for node in _walk(absent)
            if "answer-opening" in node["className"].split()
        )
        assert absent_paragraph["text"] == "No generated answer was recorded."
        assert "data-audit-value" not in absent_paragraph["attrs"]
        assert "translate" not in absent_paragraph["attrs"]

        recorded = _run("schema-drift", value="Remote participant prose")["after"]
        recorded_paragraph = next(
            node
            for node in _walk(recorded)
            if "answer-opening" in node["className"].split()
        )
        assert recorded_paragraph["attrs"].get("data-audit-value") == ""
        assert recorded_paragraph["attrs"].get("translate") == "no"

    def test_schema_drift_never_drops_known_or_unknown_values(self):
        result = _run(
            "schema-drift",
            value={
                "opening": {"lead": "opening-object-value"},
                "key_points": {"primary": "points-object-value"},
                "steps": "steps-string-value",
                "warnings": {"code": "warnings-object-value"},
                "new_envelope_field": {"leaf": "unknown-field-value"},
            },
        )["after"]
        text = result["text"]
        for value in (
            "opening-object-value",
            "points-object-value",
            "steps-string-value",
            "warnings-object-value",
            "unknown-field-value",
        ):
            assert value in text
        assert "[object Object]" not in text

    def test_nested_answer_wrapper_and_every_sibling_remain_discoverable(self):
        result = _run(
            "schema-drift",
            value={
                "response_to_participant": {
                    "opening": "participant-answer",
                    "warnings": [],
                    "tone": "warm-sibling",
                },
                "outcome": "outer-sibling",
                "answer": {"archived": "second-wrapper-sibling"},
            },
        )["after"]
        text = result["text"]
        for value in (
            "response_to_participant",
            "participant-answer",
            "warm-sibling",
            "outer-sibling",
            "second-wrapper-sibling",
        ):
            assert value in text

    def test_magic_object_keys_survive_wrapper_reconstruction_as_data(self):
        result = _run(
            "schema-drift",
            value={
                "response_to_participant": {
                    "opening": "guided-answer",
                    "__proto__": {"inner_leaf": "inner-proto-value"},
                    "constructor": {"inner_leaf": "inner-constructor-value"},
                },
                "__proto__": {"outer_leaf": "outer-proto-value"},
                "constructor": {"outer_leaf": "outer-constructor-value"},
            },
        )["after"]
        for value in (
            "inner-proto-value",
            "inner-constructor-value",
            "outer-proto-value",
            "outer-constructor-value",
        ):
            assert value in result["text"]

    def test_recorded_step_number_is_visible_exact_and_headings_are_h4(self):
        result = _run(
            "schema-drift",
            value={
                "opening": "Start here",
                "steps": [
                    {
                        "step_number": 17,
                        "action": "Exact action",
                        "detail": "Exact detail",
                    }
                ],
            },
        )["after"]
        nodes = list(_walk(result))
        step_nodes = [
            node
            for node in nodes
            if node["attrs"].get("data-field-path") == "$/answer/steps/0/step_number"
        ]
        assert step_nodes and step_nodes[0]["text"] == "17"
        assert "visually-hidden" not in step_nodes[0]["className"]
        step_card = next(
            node for node in nodes if "answer-step" in node["className"].split()
        )
        assert step_card["attrs"].get("data-has-recorded-step-number") == "true"
        assert any(node["tag"] == "h4" and "Recommended steps" in node["text"] for node in nodes)
        assert not any(node["tag"] == "h5" for node in nodes)

    def test_mixed_prose_and_valid_json_fences_render_in_order_without_raw_syntax(self):
        result = _run(
            "schema-drift",
            value=(
                "Before the record.\n\n"
                "```json\n{\"decision\":{\"safe\":true}}\n```\n\n"
                "After the record."
            ),
            options={"path": "$/generated_answer"},
        )["after"]
        text = result["text"]
        assert text.index("Before the record.") < text.index("Safe") < text.index("After the record.")
        assert "```" not in text
        assert '{"decision"' not in text
        assert any(
            node["attrs"].get("data-field-path") == "$/generated_answer/segments/1/decision/safe"
            for node in _walk(result)
        )

    def test_null_and_empty_answers_have_distinct_states_and_use_the_requested_path(self):
        absent = _run(
            "schema-drift",
            value=None,
            options={"path": "$/generated_answer"},
        )["after"]
        empty = _run(
            "schema-drift",
            value="",
            options={"path": "$/generated_answer"},
        )["after"]
        absent_value = next(node for node in _walk(absent) if "answer-opening" in node["className"])
        empty_value = next(node for node in _walk(empty) if "answer-opening" in node["className"])
        assert absent_value["text"] == "No generated answer was recorded."
        assert empty_value["text"] == "An empty generated answer was recorded."
        assert absent_value["attrs"]["data-field-path"] == "$/generated_answer"
        assert empty_value["attrs"]["data-field-path"] == "$/generated_answer"
        assert absent_value["attrs"].get("data-absent") == "true"
        assert empty_value["attrs"].get("data-empty-recorded") == "true"


class TestStructuredTextParsing:

    def test_embedded_complete_json_fence_becomes_one_structured_token(self):
        body = (
            "Before <strong>stays text</strong>.\n\n"
            "```json\n{\"decision\":{\"safe\":true}}\n```\n\n"
            "After **also stays text**."
        )
        tokens = _run("tokens", value=body)["tokens"]
        assert [token["kind"] for token in tokens] == ["text", "structured", "text"]
        assert tokens[0]["value"].startswith("Before <strong>")
        assert tokens[1]["value"] == {"decision": {"safe": True}}
        assert tokens[2]["value"].strip() == "After **also stays text**."

    def test_invalid_or_non_json_fence_remains_literal_text(self):
        body = "Before\n```json\n{not-json}\n```\nAfter"
        tokens = _run("tokens", value=body)["tokens"]
        assert tokens == [{"kind": "text", "value": body}]

    def test_production_unclosed_leading_fence_with_complete_json_is_recovered(self):
        prefix = '```{"responseSource":"knowledge-base","diagnosticPadding":"'
        suffix = (
            '","inquiries":[{"detectedInquiry":"How do I continue?","response":'
            '{"opening":"Start here","steps":[{"action":"Open settings"}]}}]}'
        )
        padding = "x" * (4_524 - len(prefix) - len(suffix))
        body = f"{prefix}{padding}{suffix}"
        assert len(body) == 4_524
        assert body.startswith('```{"responseSource"')
        assert body.endswith("}]}")

        tokens = _run("tokens", value=body)["tokens"]

        assert tokens == [
            {
                "kind": "structured",
                "value": {
                    "responseSource": "knowledge-base",
                    "diagnosticPadding": padding,
                    "inquiries": [
                        {
                            "detectedInquiry": "How do I continue?",
                            "response": {
                                "opening": "Start here",
                                "steps": [{"action": "Open settings"}],
                            },
                        }
                    ],
                },
            }
        ]

    def test_unclosed_json_fence_marker_variants_are_recovered(self):
        expected = {"responseSource": "knowledge-base", "records": [1, 2]}
        encoded = json.dumps(expected, separators=(",", ":"))

        for prefix in ("```json", "```json\n", "```JSON\r\n", "```", "```\n"):
            tokens = _run("tokens", value=f"{prefix}{encoded}")["tokens"]
            assert tokens == [{"kind": "structured", "value": expected}], prefix

    def test_invalid_or_incomplete_unclosed_fences_remain_exact_literal_text(self):
        bodies = (
            '```{"responseSource":[}',
            '```json\nnot-json',
            '```{"responseSource":"knowledge-base"} trailing prose',
            '```javascript\n{"responseSource":"knowledge-base"}',
        )

        for body in bodies:
            tokens = _run("tokens", value=body)["tokens"]
            assert tokens == [{"kind": "text", "value": body}], body

    def test_unclosed_fenced_json_over_the_parse_bound_remains_literal(self):
        limit = _run("limits")["limits"]["maxParseBytes"]
        body = '```{"payload":"' + ("x" * limit) + '"}'

        tokens = _run("tokens", value=body)["tokens"]

        assert tokens == [{"kind": "text", "value": body}]

    def test_double_encoded_and_array_nested_json_strings_render_without_json_syntax(self):
        result = _run(
            "structured-data",
            value={
                "double": json.dumps(json.dumps({"deep": "double-value"})),
                "records": [
                    json.dumps({"first": "array-json-one"}),
                    json.dumps({"second": "array-json-two"}),
                ],
            },
            options={"label": "Nested records", "open": True},
        )["after"]
        assert "double-value" in result["text"]
        assert "array-json-one" in result["text"]
        assert "array-json-two" in result["text"]
        assert "{\"first\"" not in result["text"]

    def test_tiny_numbers_and_precise_timestamps_keep_an_exact_visible_value(self):
        precise_time = "2026-08-10T22:21:22.873324+05:30"
        result = _run(
            "structured-data",
            value={"tiny_score": 1e-21, "recorded_at": precise_time},
            options={"label": "Exact values", "open": True},
        )["after"]
        nodes = list(_walk(result))
        exact = [
            node["text"]
            for node in nodes
            if "data-exact-value" in node["className"].split()
        ]
        assert "1e-21" in exact
        assert precise_time in exact


class TestBoundedProgressivePresentation:

    def test_presenter_publishes_finite_parse_and_render_limits(self):
        limits = _run("limits")["limits"]
        assert 409_600 <= limits["maxParseBytes"] <= 1_000_000
        assert 4 <= limits["maxParseDepth"] <= 128
        assert 205_000 <= limits["maxParseNodes"] <= 300_000
        assert 3 <= limits["maxRenderDepth"] <= 20
        assert 100 <= limits["maxInitialNodes"] <= 5_000
        assert 5 <= limits["childrenPerChunk"] <= 100

    def test_large_arrays_are_lazy_chunked_and_every_item_can_be_opened(self):
        records = [{"ordinal": index, "value": f"record-{index:03d}"} for index in range(137)]
        result = _run(
            "structured-data",
            value={"records": records},
            options={"label": "Large record", "open": True},
        )
        before = result["before"]
        after = result["after"]
        lazy_before = [
            node for node in _walk(before) if node["attrs"].get("data-lazy-structured") == "true"
        ]
        assert len(lazy_before) >= 1
        assert "record-136" not in before["text"]
        assert "record-000" in after["text"]
        assert "record-136" in after["text"]
        assert "[object Object]" not in after["text"]

    def test_closed_nested_groups_do_not_eagerly_materialize_their_leaf_nodes(self):
        value = {
            f"group_{group}": {
                f"leaf_{leaf}": f"value-{group:02d}-{leaf:02d}"
                for leaf in range(24)
            }
            for group in range(24)
        }
        result = _run(
            "structured-data",
            value=value,
            options={"label": "Wide nested record", "open": True},
        )
        before_nodes = list(_walk(result["before"]))
        assert len(before_nodes) <= 800
        assert "value-23-23" not in result["before"]["text"]
        assert "value-23-23" in result["after"]["text"]

    def test_large_flat_collections_page_the_chunk_disclosures_themselves(self):
        result = _run(
            "structured-data-initial",
            value=[0] * 60_000,
            options={"label": "Large flat record", "open": True},
        )["before"]
        nodes = list(_walk(result))
        assert len(nodes) <= 800
        assert any(
            "structured-group--page" in node["className"].split()
            for node in nodes
        )

    def test_maximum_density_json_within_the_event_bound_is_still_structured(self):
        dense = "[" + ",".join("0" for _ in range(100_001)) + "]"
        result = _run("parse", value=dense)
        assert result == {"parsed": True, "length": 100_001}

    def test_deep_objects_pause_at_a_disclosure_without_losing_the_leaf(self):
        value: dict[str, Any] = {"leaf": "deepest-value"}
        for depth in range(40, 0, -1):
            value = {f"level_{depth}": value}
        result = _run(
            "structured-data",
            value=value,
            options={"label": "Deep record", "open": True},
        )
        assert any(
            node["attrs"].get("data-depth-boundary") == "true"
            for node in _walk(result["after"])
        )
        assert "deepest-value" in result["after"]["text"]

    def test_each_disclosure_name_includes_its_record_context(self):
        result = _run(
            "structured-data",
            value={
                "field_mapping": {"source": "title", "target": "summary"},
                "retrieval": {"matches": 3},
                "one": 1,
                "two": 2,
                "three": 3,
            },
            options={"label": "Pipeline diagnostics", "open": True},
        )["before"]
        summaries = [node for node in _walk(result) if node["tag"] == "summary"]
        names = [node["attrs"].get("aria-label", node["text"]) for node in summaries]
        assert any("Pipeline diagnostics" in name for name in names)
        assert any("Field mapping" in name for name in names)
        assert any("Retrieval" in name for name in names)
        assert all(name.strip() not in {"Show 1 field", "Show 2 fields", "Show 3 fields"} for name in names)


class TestConversationDisclosureAccessibility:

    def test_messages_filter_keeps_comments_and_excludes_ticket_activity(self):
        comments = [
            {
                "entry_id": f"message-{index}",
                "kind": "comment",
                "actor_class": actor_class,
            }
            for index, actor_class in enumerate(
                ["participant", "ai_or_system", "ai_or_system", "human_agent", "unknown"]
            )
        ]
        events = [
            {
                "entry_id": f"event-{index}",
                "kind": "change_event",
                "actor_class": "event",
            }
            for index in range(13)
        ]
        unsupported = {
            "entry_id": "unsupported-external",
            "kind": "unsupported",
            "actor_class": "unknown",
        }

        messages = _run(
            "conversation-filter",
            value=[*comments, *events, unsupported],
            options={"filter": "messages"},
        )
        activity = _run(
            "conversation-filter",
            value=[*comments, *events, unsupported],
            options={"filter": "event"},
        )
        unclassified = _run(
            "conversation-filter",
            value=[*comments, *events, unsupported],
            options={"filter": "unclassified"},
        )

        assert messages["entry_ids"] == [entry["entry_id"] for entry in comments]
        assert activity["entry_ids"] == [entry["entry_id"] for entry in events]
        assert unclassified["entry_ids"] == ["message-4", "unsupported-external"]

    def test_participant_message_is_an_outgoing_chat_bubble(self):
        result = _run(
            "conversation",
            value={
                "entry_id": "participant-message",
                "kind": "comment",
                "actor_class": "participant",
                "visibility": "external",
                "participant_facing": True,
                "rendering": "text",
                "body": "Synthetic participant question.",
                "actor": {"display_name": "Synthetic Participant"},
                "created_at": "2026-08-10T14:12:07Z",
            },
        )["before"]
        nodes = list(_walk(result))

        assert "chat-message" in result["className"].split()
        assert result["attrs"].get("data-side") == "outgoing"
        assert any("chat-avatar" in node["className"].split() for node in nodes)
        assert any("chat-bubble" in node["className"].split() for node in nodes)
        role = next(node for node in nodes if "message-role" in node["className"].split())
        assert role["attrs"].get("data-actor-class") == "participant"
        role_glyph = next(
            node for node in nodes if "message-role-glyph" in node["className"].split()
        )
        assert role_glyph["attrs"].get("aria-hidden") == "true"
        assert role["text"] == "Participant"
        audience_glyph = next(
            node for node in nodes if "message-audience-glyph" in node["className"].split()
        )
        assert audience_glyph["attrs"].get("aria-hidden") == "true"
        assert "Synthetic participant question." in result["text"]

    def test_ai_and_human_agents_are_distinct_incoming_chat_roles(self):
        def rendered(actor_class: str, author: str):
            return _run(
                "conversation",
                value={
                    "entry_id": f"{actor_class}-message",
                    "kind": "comment",
                    "actor_class": actor_class,
                    "visibility": "internal",
                    "internal": True,
                    "rendering": "text",
                    "body": "Synthetic response.",
                    "actor": {"display_name": author},
                },
            )["before"]

        ai = rendered("ai_or_system", "N8N Workflow")
        agent = rendered("human_agent", "Synthetic Agent")

        assert ai["attrs"].get("data-side") == "incoming"
        assert agent["attrs"].get("data-side") == "incoming"
        ai_role = next(
            node for node in _walk(ai) if "message-role" in node["className"].split()
        )
        agent_role = next(
            node for node in _walk(agent) if "message-role" in node["className"].split()
        )
        assert ai_role["attrs"].get("data-actor-class") == "ai_or_system"
        assert agent_role["attrs"].get("data-actor-class") == "human_agent"
        assert ai_role["text"] == "AI or system"
        assert agent_role["text"] == "Human agent"

    def test_ticket_event_is_a_compact_activity_row_not_a_message_bubble(self):
        result = _run(
            "conversation",
            value={
                "entry_id": "event-recorded",
                "kind": "change_event",
                "actor_class": "event",
                "visibility": "internal",
                "change_summary": "stage: queued -> in_progress",
                "created_at": "2026-08-10T14:13:00Z",
            },
        )["before"]

        assert "chat-event" in result["className"].split()
        assert any(
            "activity-row" in node["className"].split() for node in _walk(result)
        )
        assert not any(
            "chat-bubble" in node["className"].split() for node in _walk(result)
        )
        assert "stage: queued -> in_progress" in result["text"]

    def test_an_external_event_is_not_falsely_labelled_internal(self):
        result = _run(
            "conversation",
            value={
                "entry_id": "external-event",
                "kind": "change_event",
                "actor_class": "event",
                "visibility": "external",
                "internal": False,
                "participant_facing": True,
                "change_summary": "Synthetic external activity.",
            },
        )["before"]

        assert "Participant-visible" in result["text"]
        assert "not shown to participant" not in result["text"]

    def test_an_ordinary_message_does_not_print_its_upstream_entry_id(self):
        result = _run(
            "conversation",
            value={
                "entry_id": "upstream-entry-must-not-be-visible",
                "kind": "comment",
                "actor_class": "human_agent",
                "visibility": "external",
                "participant_facing": True,
                "rendering": "text",
                "body": "Synthetic agent reply.",
                "actor": {"display_name": "Synthetic Agent"},
            },
        )["before"]

        assert "upstream-entry-must-not-be-visible" not in result["text"]
        assert not any("entry-meta" in node["className"].split() for node in _walk(result))

    def test_a_reply_keeps_its_relation_without_printing_the_upstream_id(self):
        reply_id = "upstream-reply-id-must-not-be-visible"
        result = _run(
            "conversation",
            value={
                "entry_id": "reply-message",
                "in_reply_to": reply_id,
                "actor_class": "ai_or_system",
                "visibility": "external",
                "participant_facing": True,
                "rendering": "text",
                "body": "Synthetic reply.",
                "actor": {"display_name": "N8N Workflow"},
            },
        )["before"]

        assert reply_id not in result["text"]
        assert "Replying to an earlier message" in result["text"]
        assert result["attrs"].get("data-reply-entry-id") == reply_id

    def test_valid_unclosed_fenced_json_is_structured_without_markup_or_raw_fence(self):
        body = (
            '```{"responseSource":"<img src=x onerror=alert(1)>",'
            '"inquiries":[{"answer":"Safe recorded answer"}]}'
        )
        result = _run(
            "conversation",
            value={
                "entry_id": "production-shaped-entry",
                "actor_class": "ai_or_system",
                "visibility": "private",
                "rendering": "text",
                "body": body,
            },
        )["after"]
        nodes = list(_walk(result))
        rendered_body = next(
            node for node in nodes if "entry-body" in node["className"].split()
        )

        assert "entry-body--structured" in rendered_body["className"].split()
        assert "```" not in rendered_body["text"]
        assert '{"responseSource"' not in rendered_body["text"]
        assert "<img src=x onerror=alert(1)>" in rendered_body["text"]
        assert "Safe recorded answer" in rendered_body["text"]
        assert not any(node["tag"] == "img" for node in nodes)

    def test_only_recorded_author_names_are_protected_from_translation(self):
        recorded = _run(
            "conversation",
            value={
                "entry_id": "recorded-author",
                "actor_class": "human_agent",
                "visibility": "private",
                "rendering": "text",
                "body": "message",
                "actor": {"display_name": "Ready"},
            },
        )["before"]
        recorded_author = next(
            node for node in _walk(recorded) if "entry-author" in node["className"].split()
        )
        assert recorded_author["text"] == "Ready"
        assert recorded_author["attrs"].get("data-user-content") == ""
        assert recorded_author["attrs"].get("translate") == "no"

        fallback = _run(
            "conversation",
            value={
                "entry_id": "missing-author",
                "actor_class": "unknown",
                "visibility": "private",
                "rendering": "text",
                "body": "message",
                "actor": None,
            },
        )["before"]
        fallback_author = next(
            node for node in _walk(fallback) if "entry-author" in node["className"].split()
        )
        assert fallback_author["text"] == "Author not recorded"
        assert "data-user-content" not in fallback_author["attrs"]
        assert "translate" not in fallback_author["attrs"]

    def test_only_recorded_change_summaries_are_protected_as_remote_content(self):
        recorded = _run(
            "conversation",
            value={
                "entry_id": "event-recorded",
                "actor_class": "event",
                "kind": "change_event",
                "visibility": "private",
                "change_summary": "Ready",
            },
        )["before"]
        recorded_body = next(
            node
            for node in _walk(recorded)
            if "entry-body" in node["className"].split()
        )
        assert recorded_body["text"] == "Ready"
        assert recorded_body["attrs"].get("data-user-content") == ""
        assert recorded_body["attrs"].get("translate") == "no"

        fallback = _run(
            "conversation",
            value={
                "entry_id": "event-fallback",
                "actor_class": "event",
                "kind": "change_event",
                "visibility": "private",
                "change_summary": "",
            },
        )["before"]
        fallback_body = next(
            node
            for node in _walk(fallback)
            if "entry-body" in node["className"].split()
        )
        assert fallback_body["text"] == "A change was recorded with no summary."
        assert "data-user-content" not in fallback_body["attrs"]
        assert "translate" not in fallback_body["attrs"]

    def test_remote_metadata_tokens_do_not_protect_their_interface_prefixes(self):
        result = _run(
            "conversation",
            value={
                "entry_id": "entry-Ready",
                "in_reply_to": "reply-Ready",
                "actor_class": "unknown",
                "kind": "unsupported",
                "visibility": "private",
                "rendering": "placeholder",
                "placeholder_reason": "unsupported_entry_type",
                "unsupported_type": "type-Ready",
            },
        )["before"]
        protected = [
            node
            for node in _walk(result)
            if node["attrs"].get("data-user-content") == ""
        ]
        assert {node["text"] for node in protected} == {
            "type-Ready",
            "entry-Ready",
        }
        assert all(node["attrs"].get("translate") == "no" for node in protected)
        meta = [node for node in _walk(result) if "entry-meta" in node["className"].split()]
        assert result["attrs"].get("data-reply-entry-id") == "reply-Ready"
        assert any("reply-context" in node["className"].split() for node in _walk(result))
        assert any(
            node["children"] and node["children"][0]["text"] == "Upstream entry type: "
            for node in meta
        )
        assert any(
            node["children"] and node["children"][0]["text"] == "Entry "
            for node in meta
        )

    def test_structured_or_mixed_messages_are_never_line_clamped(self):
        body = (
            "An explanatory prefix.\n\n```json\n"
            + json.dumps({"records": [{"value": f"item-{index}"} for index in range(40)]})
            + "\n```\n\nA safe suffix."
        )
        result = _run(
            "conversation",
            value={
                "entry_id": "mixed-entry",
                "actor_class": "ai_or_system",
                "visibility": "private",
                "rendering": "text",
                "body": body,
            },
        )["before"]
        bodies = [node for node in _walk(result) if "entry-body" in node["className"].split()]
        assert len(bodies) == 1
        assert bodies[0]["attrs"].get("data-collapsed") != "true"
        assert "An explanatory prefix." in bodies[0]["text"]
        assert "A safe suffix." in bodies[0]["text"]
        assert any(node["tag"] == "details" for node in _walk(bodies[0]))

    def test_long_plain_text_control_exposes_state_and_controlled_body(self):
        result = _run(
            "conversation",
            value={
                "entry_id": "entry with unsafe/id",
                "actor_class": "participant",
                "visibility": "public",
                "participant_facing": True,
                "rendering": "text",
                "body": "plain-text " * 500,
            },
            options={"expanded": False},
        )["before"]
        bodies = [node for node in _walk(result) if "entry-body" in node["className"].split()]
        controls = [
            node
            for node in _walk(result)
            if node["dataset"].get("action") == "toggle-entry"
        ]
        assert len(bodies) == len(controls) == 1
        body_id = bodies[0]["attrs"].get("id")
        assert body_id and "/" not in body_id and " " not in body_id
        assert bodies[0]["attrs"].get("data-collapsed") == "true"
        assert controls[0]["attrs"].get("aria-expanded") == "false"
        assert controls[0]["attrs"].get("aria-controls") == body_id

        expanded = _run(
            "conversation",
            value={
                "entry_id": "entry with unsafe/id",
                "actor_class": "participant",
                "visibility": "public",
                "participant_facing": True,
                "rendering": "text",
                "body": "plain-text " * 500,
            },
            options={"expanded": True},
        )["before"]
        expanded_control = next(
            node
            for node in _walk(expanded)
            if node["dataset"].get("action") == "toggle-entry"
        )
        assert expanded_control["attrs"].get("aria-expanded") == "true"
