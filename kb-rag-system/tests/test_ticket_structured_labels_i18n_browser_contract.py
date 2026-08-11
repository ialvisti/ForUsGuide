"""Behavioral i18n contract for known structured-audit field labels.

The technical audit keeps exact remote keys and values visible while presenting
an adjacent reviewer-friendly label.  This test executes the shipped presenter
and preference controller against a small DOM, switches the live tree EN -> ES
-> EN, and proves that only the interface label changes.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

from api.tickets_console_main import UI_ASSETS_DIRECTORY


_LANGUAGE_SWITCH_HARNESS = r"""
import fs from "node:fs";

class FakeText {
  constructor(value) {
    this.nodeType = 3;
    this.nodeValue = String(value ?? "");
    this.parentElement = null;
  }
  get childNodes() { return []; }
  get textContent() { return this.nodeValue; }
  set textContent(value) { this.nodeValue = String(value ?? ""); }
}

function simpleMatch(node, selector) {
  const value = selector.trim();
  if (value === "") return false;
  if (value.includes(" ") || value.includes(":not(")) return false;

  const tagAndAttribute = value.match(/^([a-z]+)(\[[^\]]+\])$/i);
  if (tagAndAttribute !== null) {
    return simpleMatch(node, tagAndAttribute[1]) && simpleMatch(node, tagAndAttribute[2]);
  }
  if (/^[a-z]+$/i.test(value)) return node.tagName === value.toUpperCase();
  if (value.startsWith("#")) return node.id === value.slice(1);
  if (value.startsWith(".")) return node.className.split(/\s+/).includes(value.slice(1));

  const attribute = value.match(/^\[([^=\]]+)(?:=['\"]?([^'\"\]]*)['\"]?)?\]$/);
  if (attribute !== null) {
    if (!node.hasAttribute(attribute[1])) return false;
    return attribute[2] === undefined || node.getAttribute(attribute[1]) === attribute[2];
  }
  return false;
}

class FakeElement {
  constructor(tag) {
    this.nodeType = 1;
    this.tagName = String(tag).toUpperCase();
    this.nodeName = this.tagName;
    this.children = [];
    this.parentElement = null;
    this.className = "";
    this.dataset = {};
    this.attributes = new Map();
    this.listeners = new Map();
    this.id = "";
    this.lang = "";
  }
  get childNodes() { return this.children; }
  get firstChild() { return this.children[0] ?? null; }
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
  get textContent() { return this.children.map((child) => child.textContent).join(""); }
  set textContent(value) {
    this.children = [];
    const text = String(value ?? "");
    if (text !== "") this.appendChild(new FakeText(text));
  }
  setAttribute(name, value) {
    const text = String(value ?? "");
    this.attributes.set(String(name), text);
    if (name === "id") this.id = text;
    if (name.startsWith("data-")) {
      const key = name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
      this.dataset[key] = text;
    }
  }
  getAttribute(name) { return this.attributes.get(String(name)) ?? null; }
  hasAttribute(name) { return this.attributes.has(String(name)); }
  addEventListener(name, handler) {
    const handlers = this.listeners.get(name) ?? [];
    handlers.push(handler);
    this.listeners.set(name, handlers);
  }
  removeEventListener(name, handler) {
    const handlers = this.listeners.get(name) ?? [];
    this.listeners.set(name, handlers.filter((item) => item !== handler));
  }
  closest(selectors) {
    for (let current = this; current !== null; current = current.parentElement) {
      if (String(selectors).split(",").some((selector) => simpleMatch(current, selector))) {
        return current;
      }
    }
    return null;
  }
  querySelector() { return null; }
}

function walk(node) {
  return [node, ...Array.from(node.childNodes ?? []).flatMap(walk)];
}

const html = new FakeElement("html");
html.lang = "en";
const view = {
  navigator: { languages: ["en"] },
  CustomEvent: class {
    constructor(type, options = {}) {
      this.type = type;
      this.detail = options.detail;
      this.bubbles = options.bubbles === true;
    }
  },
};
globalThis.document = {
  nodeType: 9,
  documentElement: html,
  defaultView: view,
  createElement(tag) { return new FakeElement(tag); },
  getElementById() { return null; },
  querySelectorAll(selector) {
    return walk(html).filter((node) => node.nodeType === 1 && simpleMatch(node, selector));
  },
  dispatchEvent() { return true; },
};

const request = JSON.parse(fs.readFileSync(0, "utf8"));
const structured = await import(request.structuredUrl);
const preferences = await import(request.preferencesUrl);
const audit = structured.renderStructuredData(request.value, {
  label: "",
  path: "$/recorded_run",
});
html.appendChild(audit);

function snapshot() {
  const rows = {};
  for (const node of walk(audit)) {
    if (node.nodeType !== 1 || !node.className.split(/\s+/).includes("data-label")) continue;
    const label = node.children.find((child) =>
      child.nodeType === 1 && !child.hasAttribute("data-field-key")
    );
    const key = node.children.find((child) =>
      child.nodeType === 1 && child.hasAttribute("data-field-key")
    );
    rows[key.textContent] = label.textContent;
  }
  const values = Object.fromEntries(
    walk(audit)
      .filter((node) => node.nodeType === 1 && node.hasAttribute("data-field-path"))
      .map((node) => [node.getAttribute("data-field-path"), node.textContent])
  );
  return { language: html.lang, rows, values };
}

const controller = preferences.initPreferences({ themeToggle: null, languageSelect: null });
const englishBefore = snapshot();
controller.setLanguage("es", { notify: false });
const spanish = snapshot();
controller.setLanguage("en", { notify: false });
const englishAfter = snapshot();
controller.destroy();

process.stdout.write(JSON.stringify({ englishBefore, spanish, englishAfter }));
"""


_VALUES = {
    "total_inquiries_in_ticket": "REMOTE total_inquiries_in_ticket",
    "generate_response": "generate_response",
    "forusbots_elapsed_s": "REMOTE 12.5 s",
    "forusbots_job_id": "REMOTE job-main",
    "forusbots_participant_elapsed_s": "REMOTE 9.25 s",
    "forusbots_participant_job_id": "REMOTE job-participant",
    "used_chunks": "REMOTE used_chunks",
}

_ENGLISH = {
    "total_inquiries_in_ticket": "Total inquiries in ticket",
    "generate_response": "Generate response",
    "forusbots_elapsed_s": "ForUsBots elapsed time (seconds)",
    "forusbots_job_id": "ForUsBots job ID",
    "forusbots_participant_elapsed_s": "ForUsBots participant elapsed time (seconds)",
    "forusbots_participant_job_id": "ForUsBots participant job ID",
    "used_chunks": "Used chunks",
}

_SPANISH = {
    "total_inquiries_in_ticket": "Total de consultas del ticket",
    "generate_response": "Generar respuesta",
    "forusbots_elapsed_s": "Tiempo de ForUsBots (s)",
    "forusbots_job_id": "ID de tarea de ForUsBots",
    "forusbots_participant_elapsed_s": "Tiempo del participante en ForUsBots (s)",
    "forusbots_participant_job_id": "ID de tarea del participante en ForUsBots",
    "used_chunks": "Fragmentos utilizados",
}


def _switch_languages() -> dict[str, Any]:
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", _LANGUAGE_SWITCH_HARNESS],
        check=True,
        capture_output=True,
        input=json.dumps(
            {
                "value": _VALUES,
                "structuredUrl": (UI_ASSETS_DIRECTORY / "structured.js").as_uri(),
                "preferencesUrl": (UI_ASSETS_DIRECTORY / "preferences.js").as_uri(),
            }
        ),
        text=True,
    )
    return json.loads(completed.stdout)


def test_known_audit_labels_switch_to_spanish_and_round_trip_to_english() -> None:
    result = _switch_languages()

    assert result["englishBefore"]["rows"] == _ENGLISH
    assert result["spanish"]["rows"] == _SPANISH
    assert result["englishAfter"]["rows"] == _ENGLISH
    assert result["englishBefore"]["language"] == "en"
    assert result["spanish"]["language"] == "es"
    assert result["englishAfter"]["language"] == "en"


def test_language_switch_never_translates_exact_remote_keys_or_values() -> None:
    result = _switch_languages()
    expected_values = {f"$/recorded_run/{key}": value for key, value in _VALUES.items()}

    for snapshot in result.values():
        assert set(snapshot["rows"]) == set(_VALUES)
        assert snapshot["values"] == expected_values
