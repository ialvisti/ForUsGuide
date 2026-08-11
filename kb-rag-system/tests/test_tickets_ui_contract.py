"""Stage 6 Step 1 — the static contract the `/tickets` browser UI must satisfy.

These are not "does the page look right" tests. Every assertion here is a
property that cannot be checked by looking at the screen, and that a later edit
could silently break:

*   the page has to load under a ``Content-Security-Policy`` with no
    ``'unsafe-inline'`` and no external host, so an inline ``<script>``, an
    inline ``style=`` attribute, or a CDN reference is a *server-side* failure
    that a developer with a warm cache would not notice;
*   every remote string on this page is a participant's ticket title, a
    reviewer's comment, or an email address, so the renderer may never touch
    ``innerHTML`` and friends — the console's whole XSS story is the CSP plus
    that discipline;
*   the browser must not persist tickets, comments, cursors, or the CSRF token,
    because this console's data is customer-linked and its token is deliberately
    memory-only (Stage 5 issues it from ``GET /session`` for exactly that
    reason);
*   the legacy sheet's reviewer column and the *authenticated* session actor are
    different facts about different people. Conflating them would relabel two
    years of migrated review history with whoever happens to be logged in.

The DOM is parsed with a small ``html.parser`` tree builder rather than a
third-party parser: this repository ships none, and a structural contract test
that cannot run is not a contract.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ElementTree
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator, Optional

import pytest
from fastapi.testclient import TestClient

from api.reviewer_auth import IAP_ASSERTION_HEADER, IAP_ISSUER
from api.ticket_review_models import (
    MIN_TOUCH_TARGET_PX,
    RESPONSIVE_BREAKPOINT_PX,
)
from api.tickets_console_config import TicketConsoleSettings
from api.tickets_console_main import (
    SECURITY_HEADERS,
    UI_ASSETS_DIRECTORY,
    UI_DIRECTORY,
    UI_INDEX_FILE,
    build_console_app,
)

import base64
import os
from datetime import datetime, timezone

CURSOR_KEY_B64 = base64.b64encode(bytes(range(32))).decode("ascii")
CONSOLE_ORIGIN = "https://tickets-console-abc-uc.a.run.app"
CSRF_SECRET = "synthetic-csrf-value"  # pragma: allowlist secret
IAP_AUDIENCE = "/projects/1234567890/locations/us-central1/services/tickets-console"
ADMIN_EMAIL = "admin@example.invalid"
ASSERTION = "synthetic.iap.assertion"
SYNTHETIC_PART = "don:core:dvrv-us-1:devo/synthetic:product/1"
T0 = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc)

#: The asset the mount must expose, and the media type Starlette has to pick.
#: ``text/javascript`` is what Python 3.12+ guesses; the older spelling is
#: accepted so the assertion pins *correctness*, not an interpreter version.
EXPECTED_ASSET_TYPES: dict[str, tuple[str, ...]] = {
    "tickets.css": ("text/css",),
    "app.js": ("text/javascript", "application/javascript"),
    "api.js": ("text/javascript", "application/javascript"),
    "state.js": ("text/javascript", "application/javascript"),
    "render.js": ("text/javascript", "application/javascript"),
    "structured.js": ("text/javascript", "application/javascript"),
    "answer-presentation.js": ("text/javascript", "application/javascript"),
    "icons.svg": ("image/svg+xml",),
}

#: The queue is an operational review surface.  Technical execution data stays
#: available in the detail disclosure, but it must not dominate the list.
REQUIRED_EXECUTION_COLUMNS = (
    "Ticket",
    "Review status",
    "Received",
    "Rating",
    "Reviewer",
    "Actions",
)

FORBIDDEN_FILE_EGRESS_WORDS = re.compile(
    r"\b(?:csv|import(?:ed|s|ing)?|export(?:ed|s|ing)?|download(?:ed|s|ing)?|"
    r"upload(?:ed|s|ing)?|spreadsheet)\b",
    re.IGNORECASE,
)

#: Browser sinks that turn a ticket title into script. ``document.write`` and
#: ``eval`` are here for the same reason, not for tidiness.
FORBIDDEN_SINKS = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "eval(",
    "Function(",
)

#: Anything that survives a tab close. The CSRF token, a cursor, a ticket body,
#: and a reviewer's comment must all be gone when the page is.
FORBIDDEN_STORAGE = ("localStorage", "sessionStorage", "indexedDB", "document.cookie")

#: Credentials that belong to other service boundaries, and the one ticket id
#: that appears in the product screenshots.
FORBIDDEN_LITERALS = ("X-API-Key", "Bearer ", "Authorization", "forusall", "TKT-")

_EMAIL_LITERAL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_INLINE_EVENT_ATTRIBUTE = re.compile(r"\son[a-z]+\s*=", re.IGNORECASE)

#: A quoted relative module specifier, e.g. ``"./remediation.js"``.
#:
#: Stripped before the absent-route scan below, and *only* there. Stage 7 ships a
#: module named after the feature it renders, and ``from "./remediation.js"``
#: contains the substring ``/remediation`` while naming a local file rather than a
#: route. Removing exactly this shape keeps the scan's reach over everything else,
#: so a real ``fetch("/api/admin/v1/remediation/…")`` — or a template built one —
#: still fails the test.
_RELATIVE_MODULE_SPECIFIER = re.compile(r"""["']\./[A-Za-z0-9_-]+\.js["']""")


# =====================================================================
# A minimal DOM
# =====================================================================


class Node:
    """One element, its attributes, its children, and its text."""

    __slots__ = ("tag", "attrs", "children", "parent", "text")

    def __init__(self, tag: str, attrs: dict[str, str], parent: Optional[Node]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[Node] = []
        self.parent = parent
        self.text = ""

    def walk(self) -> Iterator[Node]:
        yield self
        for child in self.children:
            yield from child.walk()

    def find_all(self, *tags: str) -> list[Node]:
        wanted = {tag.lower() for tag in tags}
        return [node for node in self.walk() if node.tag in wanted]

    def all_text(self) -> str:
        return " ".join(part for part in (node.text for node in self.walk()) if part).strip()

    def get(self, name: str) -> Optional[str]:
        return self.attrs.get(name)

    def ancestors(self) -> Iterator[Node]:
        current = self.parent
        while current is not None:
            yield current
            current = current.parent

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.tag} {self.attrs}>"


class _TreeBuilder(HTMLParser):
    """Build a forgiving element tree, tracking void and self-closing tags."""

    VOID = frozenset(
        {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "source",
            "track",
            "wbr",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document", {}, None)
        self._stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {key: (value or "") for key, value in attrs}, self._stack[-1])
        self._stack[-1].children.append(node)
        if tag not in self.VOID:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, {key: (value or "") for key, value in attrs}, self._stack[-1])
        self._stack[-1].children.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data):
        stripped = data.strip()
        if stripped:
            node = self._stack[-1]
            node.text = f"{node.text} {stripped}".strip() if node.text else stripped


def parse(markup: str) -> Node:
    builder = _TreeBuilder()
    builder.feed(markup)
    builder.close()
    return builder.root


# =====================================================================
# Fixtures
# =====================================================================


def _settings(monkeypatch, **overrides) -> TicketConsoleSettings:
    for name in list(os.environ):
        if name.startswith("TICKETS_"):
            monkeypatch.delenv(name, raising=False)
    values: dict[str, object] = {
        "ENVIRONMENT": "local",
        "AUTH_MODE": "iap",
        "IAP_AUDIENCE": IAP_AUDIENCE,
        "ALLOWED_EMAIL_DOMAINS": ["example.invalid"],
        "ROLE_BINDINGS_JSON": f'{{"{ADMIN_EMAIL}": "admin"}}',
        "CSRF_SIGNING_SECRET": CSRF_SECRET,
        "CURSOR_AEAD_KEY": CURSOR_KEY_B64,
        "CONSOLE_ORIGIN": CONSOLE_ORIGIN,
        "GCP_PROJECT": "emulator-project",
        "GCP_REGION": "us-central1",
        "FIRESTORE_DATABASE": "tickets-console-emulator",
        "DEVREV_ALLOWED_PART_DONS": [SYNTHETIC_PART],
        "DEVREV_ALLOWED_TICKET_VISIBILITY_IDS": [2],
        "DEVREV_ALLOWED_TIMELINE_VISIBILITIES": ["internal", "external"],
    }
    values.update(overrides)
    return TicketConsoleSettings(_env_file=None, **values)


@pytest.fixture
def client(monkeypatch) -> TestClient:
    def verifier(token: str, audience: str):
        return {
            "iss": IAP_ISSUER,
            "aud": audience,
            "sub": f"accounts.google.com:{ADMIN_EMAIL}",
            "email": ADMIN_EMAIL,
        }

    app = build_console_app(
        _settings(monkeypatch),
        claims_verifier=verifier,
        clock=lambda: T0,
        firestore_database="tickets-console-emulator",
    )
    return TestClient(app, raise_server_exceptions=False)


def _auth() -> dict[str, str]:
    return {IAP_ASSERTION_HEADER: ASSERTION}


@pytest.fixture(scope="module")
def html_source() -> str:
    return UI_INDEX_FILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def dom(html_source: str) -> Node:
    return parse(html_source)


def _by_id(node: Node, wanted: str) -> Node:
    matches = [item for item in node.walk() if item.get("id") == wanted]
    assert len(matches) == 1, f"expected exactly one #{wanted}, found {len(matches)}"
    return matches[0]


@pytest.fixture(scope="module")
def css_source() -> str:
    return (UI_ASSETS_DIRECTORY / "tickets.css").read_text(encoding="utf-8")


def _script_paths() -> list[Path]:
    return sorted(UI_ASSETS_DIRECTORY.glob("*.js"))


@pytest.fixture(scope="module")
def scripts() -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8") for path in _script_paths()}


@pytest.fixture(scope="module")
def shipped_sources() -> dict[str, str]:
    """Every file the browser is served, keyed by name."""
    sources = {UI_INDEX_FILE.name: UI_INDEX_FILE.read_text(encoding="utf-8")}
    for path in sorted(UI_ASSETS_DIRECTORY.iterdir()):
        if path.is_file():
            sources[path.name] = path.read_text(encoding="utf-8")
    return sources


# =====================================================================
# 1 — the routes and the media types
# =====================================================================


class TestServedFiles:

    def test_the_ui_lives_outside_the_api_package(self):
        """The UI is a sibling of ``api/``, not a directory inside it.

        Stage 5 shipped a placeholder under ``api/tickets_ui``; keeping the real
        UI there would put browser assets on the Python import path.
        """
        assert UI_DIRECTORY.name == "tickets"
        assert UI_DIRECTORY.parent.name == "ui"
        assert UI_DIRECTORY.parent.parent.name == "kb-rag-system"
        assert UI_INDEX_FILE.is_file()
        assert UI_ASSETS_DIRECTORY.is_dir()

    def test_the_index_is_not_inside_the_mounted_directory(self):
        """The mount serves assets only.

        Mounting the UI root at ``/tickets/assets`` would publish
        ``index.html`` — and any stray file beside it — under a second URL,
        reversing a documented Stage 5 decision that no test otherwise pins.
        """
        assert UI_INDEX_FILE.parent != UI_ASSETS_DIRECTORY
        assert not (UI_ASSETS_DIRECTORY / "index.html").exists()

    def test_the_index_route_serves_html(self, client):
        response = client.get("/tickets", headers=_auth())
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert response.text.lstrip().lower().startswith("<!doctype html>")

    @pytest.mark.parametrize("name,expected", sorted(EXPECTED_ASSET_TYPES.items()))
    def test_each_asset_is_served_with_the_right_media_type(self, client, name, expected):
        response = client.get(f"/tickets/assets/{name}", headers=_auth())
        assert response.status_code == 200, name
        assert response.headers["content-type"].split(";")[0].strip() in expected

    def test_the_assets_still_require_a_verified_identity(self, client):
        assert client.get("/tickets/assets/app.js").status_code == 401

    def test_a_deep_link_serves_the_same_document(self, client):
        index = client.get("/tickets", headers=_auth())
        deep = client.get("/tickets/TKT-424242", headers=_auth())
        assert deep.status_code == 200
        assert deep.text == index.text

    def test_the_document_never_names_a_ticket_id(self, html_source):
        """``TKT-`` may not appear in the page at all.

        A Stage 5 test proves the deep-link route does not reflect its path into
        the document by asserting ``TKT-`` is absent from the response, so an
        example ticket id in a placeholder would break that test from a distance
        and read as a reflection bug.
        """
        assert "TKT-" not in html_source


# =====================================================================
# 2 — document structure and accessibility scaffolding
# =====================================================================


class TestDocumentStructure:

    def test_there_is_exactly_one_main(self, dom):
        assert len(dom.find_all("main")) == 1

    def test_the_skip_link_is_first_and_points_at_main(self, dom):
        body = dom.find_all("body")[0]
        focusable = [
            node
            for node in body.walk()
            if node.tag in {"a", "button", "input", "select", "textarea"}
        ]
        assert focusable, "the page has no focusable content"
        first = focusable[0]
        assert first.tag == "a"
        target = first.get("href") or ""
        assert target.startswith("#")
        main = dom.find_all("main")[0]
        assert main.get("id") == target[1:]
        assert first.all_text() == "Skip to main content"

    def test_the_headings_are_real_and_never_skip_a_level(self, dom):
        levels = [
            int(node.tag[1])
            for node in dom.walk()
            if re.fullmatch(r"h[1-6]", node.tag)
        ]
        assert levels, "the page has no headings"
        assert levels.count(1) == 1
        assert levels[0] == 1
        for previous, current in zip(levels, levels[1:]):
            assert current <= previous + 1, levels

    def test_the_heading_text_is_never_empty(self, dom):
        for node in dom.walk():
            if re.fullmatch(r"h[1-6]", node.tag):
                assert node.all_text(), f"{node.tag} carries no text"

    def test_a_live_region_exists_for_announcements(self, dom):
        regions = [
            node
            for node in dom.walk()
            if node.get("aria-live") or node.get("role") in {"status", "alert"}
        ]
        assert regions, "no live region: state changes would be silent"
        assert any((node.get("aria-live") or "") == "polite" for node in regions)

    def test_readiness_changes_are_announced(self, dom):
        health = next(node for node in dom.walk() if node.get("id") == "health-state")
        assert health.get("role") == "status"
        assert health.get("aria-live") == "polite"
        assert health.get("aria-atomic") == "true"

    def test_the_table_has_a_caption_and_column_headers(self, dom):
        tables = dom.find_all("table")
        assert tables, "no data table"
        for table in tables:
            assert table.find_all("caption"), "a data table with no caption"
            headers = [
                node for node in table.find_all("th") if node.get("scope") == "col"
            ]
            assert len(headers) >= len(REQUIRED_EXECUTION_COLUMNS)

    def test_every_form_control_is_labeled(self, dom):
        label_targets = {
            node.get("for") for node in dom.find_all("label") if node.get("for")
        }
        for node in dom.find_all("input", "select", "textarea"):
            if (node.get("type") or "").lower() in {"hidden", "submit", "reset"}:
                continue
            named = bool(node.get("aria-label") or node.get("aria-labelledby"))
            identified = node.get("id") in label_targets
            assert named or identified, f"unlabeled control: {node!r}"

    def test_the_document_declares_a_language_and_a_viewport(self, dom):
        html = dom.find_all("html")[0]
        assert (html.get("lang") or "").startswith("en")
        viewports = [
            node
            for node in dom.find_all("meta")
            if (node.get("name") or "").lower() == "viewport"
        ]
        assert viewports, "no viewport: the mobile layout would never apply"
        assert "width=device-width" in (viewports[0].get("content") or "")

    def test_the_required_regions_are_all_present(self, dom):
        ids = {node.get("id") for node in dom.walk() if node.get("id")}
        for required in (
            "environment-badge",
            "session-email",
            "session-role",
            "health-state",
            "operational-heading",
            "execution-queue",
            "filters-executions",
            "bulk-bar",
            "tickets-table",
            "pagination",
            "toast-region",
            "ticket-detail",
        ):
            assert required in ids, f"missing region: {required}"

    def test_there_is_one_rag_execution_collection_and_no_source_switcher(self, dom):
        source_tablists = [
            node
            for node in dom.walk()
            if node.get("role") == "tablist" and node.get("aria-labelledby") == "tabs-heading"
        ]
        assert source_tablists == []
        queues = [node for node in dom.walk() if node.get("id") == "execution-queue"]
        assert len(queues) == 1
        assert "Tickets ready for evaluation" in queues[0].all_text()

    def test_no_file_exchange_or_manual_queue_language_is_visible(self, dom):
        visible = dom.all_text()
        assert "All DevRev tickets" not in visible
        assert "Add to review queue" not in visible
        assert FORBIDDEN_FILE_EGRESS_WORDS.search(visible) is None


# =====================================================================
# 3 and 4 — the execution ledger's columns, and whose name goes in them
# =====================================================================


class TestColumns:

    def _column_headers(self, dom: Node) -> list[str]:
        return [
            node.all_text()
            for table in dom.find_all("table")
            for node in table.find_all("th")
            if node.get("scope") == "col"
        ]

    @pytest.mark.parametrize("column", REQUIRED_EXECUTION_COLUMNS)
    def test_every_execution_column_is_present(self, dom, column):
        assert column in self._column_headers(dom)

    def test_reviewer_columns_follow_the_operational_order(self, dom):
        headers = self._column_headers(dom)
        positions = [headers.index(name) for name in REQUIRED_EXECUTION_COLUMNS]
        assert positions == sorted(positions), headers
        assert all(
            technical not in headers
            for technical in ("Execution", "Route", "Run status", "DevRev context")
        )


class TestReviewerIsNotTheSessionActor:

    def test_the_session_actor_is_rendered_only_in_the_header(self, dom, scripts):
        header = dom.find_all("header")[0]
        session = [node for node in dom.walk() if node.get("id") == "session-email"]
        assert len(session) == 1
        assert header in list(session[0].ancestors())

    def test_the_row_reviewer_comes_from_the_review_record(self, scripts):
        renderer = scripts["render.js"]
        assert "assigned_reviewer_email" in renderer
        assert "legacy_reviewer_display_name" in renderer

    def test_the_renderer_cannot_reach_the_session_identity(self, scripts):
        """``render.js`` is handed rows, never the session.

        If the renderer could read the session it would be one typo away from
        stamping the logged-in reviewer onto every migrated sheet row.
        """
        renderer = scripts["render.js"]
        for forbidden in ("session", "csrf", "identity."):
            assert forbidden not in renderer.lower(), forbidden

    def test_the_historical_fallback_is_labeled_without_file_migration_language(self, scripts):
        renderer = scripts["render.js"]
        assert "Historical value" in renderer
        assert "Legacy sheet value" not in renderer

    def test_an_unassigned_review_is_not_silently_attributed(self, scripts):
        assert "Unassigned" in scripts["render.js"]


# =====================================================================
# 5 — how the scripts are loaded
# =====================================================================


class TestThemeAndLanguagePreferences:

    def test_theme_and_language_controls_are_labeled(self, dom):
        controls = {node.get("id"): node for node in dom.walk() if node.get("id")}

        theme = controls["theme-toggle"]
        assert theme.tag == "button"
        assert theme.get("type") == "button"
        assert theme.get("aria-label")
        assert theme.get("aria-pressed") in {"true", "false"}

        language = controls["language-select"]
        assert language.tag == "select"
        labels = [
            node
            for node in dom.find_all("label")
            if node.get("for") == "language-select"
        ]
        assert labels and labels[0].all_text()
        options = {(node.get("value") or "") for node in language.find_all("option")}
        assert {"en", "es"} <= options

    def test_preferences_are_reached_from_the_entry_module(self, scripts):
        assert 'from "./preferences.js"' in scripts["app.js"]
        assert "initPreferences" in scripts["app.js"]

    def test_explicit_themes_override_the_system_scheme(self, css_source):
        assert 'html[data-theme="light"]' in css_source
        assert 'html[data-theme="dark"]' in css_source
        assert "prefers-color-scheme: dark" in css_source

    def test_light_is_the_deterministic_initial_theme(self, dom, scripts):
        html = dom.find_all("html")[0]
        assert html.get("data-theme") == "light"
        assert _by_id(dom, "theme-toggle").get("aria-pressed") == "false"
        assert _by_id(dom, "theme-color").get("content") == "#f4f5f3"
        assert 'matchMedia("(prefers-color-scheme: dark)")' not in scripts["preferences.js"]

    def test_dark_destructive_actions_keep_accessible_contrast(self, css_source):
        dark_button = re.search(
            r'html\[data-theme="dark"\]\s+\.button-danger\s*\{([^}]*)\}',
            css_source,
        )
        assert dark_button is not None
        assert "color: #07101f" in dark_button.group(1)
        assert ':root:not([data-theme]) .button-danger' in css_source

    def test_browser_chrome_tracks_the_selected_theme(self, dom, scripts):
        theme_colors = [
            node for node in dom.find_all("meta") if node.get("name") == "theme-color"
        ]
        assert len(theme_colors) == 1
        assert theme_colors[0].get("id") == "theme-color"
        preferences = scripts["preferences.js"]
        assert 'getElementById("theme-color")' in preferences
        assert '"#f4f5f3"' in preferences
        assert '"#07101f"' in preferences

    def test_spanish_is_a_complete_selectable_locale(self, scripts):
        preferences = scripts["preferences.js"]
        assert '"es"' in preferences
        assert "document.documentElement.lang" in preferences
        assert "MutationObserver" in preferences
        for phrase in (
            "Quality operations",
            "Reviewer workspace",
            "Ticket reviews",
            "Tickets ready for evaluation",
            "Use the CRM to review the ticket and final answer, then record the quality decision here.",
            "Technical filters",
            "Technical batch actions",
            "Select every ticket on this page",
            "Focused review",
            "Evaluation",
            "Rate the final answer and document the correction, if any.",
            "Advanced review fields",
            "Technical audit details",
            "Save review",
        ):
            assert phrase in preferences
        assert '["on this page", "en esta página"]' in preferences
        assert ".cell-title" in preferences
        assert "Review ticket" in preferences
        assert "Evaluar ticket" in preferences
        assert '  ".toast-body",' not in preferences
        assert '  ".state-body",' not in preferences
        assert "normalizeCountNumber" in preferences
        assert "navigator.languages" in preferences
        assert "This review has unsaved changes" in preferences
        assert "Esta revisión tiene cambios sin guardar" in preferences
        assert "Your session ended" in preferences
        assert "Tu sesión terminó" in preferences
        assert "The request could not be completed" in preferences
        assert "No se pudo completar la solicitud" in preferences
        assert "Solo se ofrecen los cambios permitidos por el ciclo de revisión" in preferences
        assert "Puedes tomar una revisión sin asignar o liberar una asignada a ti" in preferences
        assert "¿Fijar" in scripts["app.js"]
        assert 'from "./preferences.js"' in scripts["detail.js"]

    def test_dates_follow_the_selected_document_locale(self, scripts):
        assert "document.documentElement.lang" in scripts["render.js"]
        evaluation = scripts["evaluation.js"]
        assert "displayLocale" in evaluation
        assert evaluation.count("toLocaleString(displayLocale())") >= 3

    def test_detail_mode_focuses_the_workspace(self, scripts, css_source):
        assert 'classList.toggle("detail-open"' in scripts["app.js"]
        assert "body.detail-open" in css_source


class TestScriptLoading:

    def test_every_script_is_a_same_origin_module(self, dom):
        scripts = dom.find_all("script")
        assert scripts, "the page loads no script"
        for node in scripts:
            assert node.get("type") == "module", node.attrs
            source = node.get("src") or ""
            assert source.startswith("/tickets/assets/"), source
            assert not node.all_text(), "an inline script body"

    def test_every_stylesheet_is_a_same_origin_file(self, dom):
        sheets = [
            node
            for node in dom.find_all("link")
            if "stylesheet" in (node.get("rel") or "").lower()
        ]
        assert sheets
        for node in sheets:
            assert (node.get("href") or "").startswith("/tickets/assets/")

    def test_no_reference_names_a_remote_host(self, html_source, css_source, scripts):
        for name, source in [("index.html", html_source), ("tickets.css", css_source)] + list(
            scripts.items()
        ):
            for marker in ("http://", "https://", "//cdn", "fonts.googleapis"):
                assert marker not in source, f"{name} names a remote host: {marker}"

    def test_the_module_graph_is_relative_and_same_origin(self, scripts):
        for name, source in scripts.items():
            for match in re.finditer(r"""from\s+["']([^"']+)["']""", source):
                target = match.group(1)
                assert target.startswith("./"), f"{name} imports {target}"
                assert target.endswith(".js"), f"{name} imports {target}"


# =====================================================================
# 6 — sinks, storage, and credentials
# =====================================================================


class TestNoDangerousBrowserPatterns:

    @pytest.mark.parametrize("sink", FORBIDDEN_SINKS)
    def test_no_shipped_source_uses_an_html_injection_sink(self, shipped_sources, sink):
        offenders = [name for name, source in shipped_sources.items() if sink in source]
        assert offenders == [], f"{sink} in {offenders}"

    @pytest.mark.parametrize("api", FORBIDDEN_STORAGE)
    def test_no_shipped_source_persists_anything_in_the_browser(self, shipped_sources, api):
        offenders = [name for name, source in shipped_sources.items() if api in source]
        assert offenders == [], f"{api} in {offenders}"

    @pytest.mark.parametrize("literal", FORBIDDEN_LITERALS)
    def test_no_shipped_source_carries_a_foreign_credential_or_id(
        self, shipped_sources, literal
    ):
        offenders = [name for name, source in shipped_sources.items() if literal in source]
        assert offenders == [], f"{literal} in {offenders}"

    def test_no_shipped_source_carries_an_email_address(self, shipped_sources):
        for name, source in shipped_sources.items():
            found = _EMAIL_LITERAL.findall(source)
            assert found == [], f"{name} carries {found}"

    def test_the_document_has_no_inline_event_attribute(self, html_source):
        assert _INLINE_EVENT_ATTRIBUTE.search(html_source) is None

    def test_the_document_has_no_inline_style(self, html_source, dom):
        assert "<style" not in html_source.lower()
        for node in dom.walk():
            assert node.get("style") is None, node

    def test_the_scripts_register_listeners_rather_than_attributes(self, scripts):
        assert any("addEventListener" in source for source in scripts.values())
        for name, source in scripts.items():
            assert not re.search(r"\.on(click|change|input|submit)\s*=", source), name

    def test_the_renderer_writes_only_text_and_created_nodes(self, scripts):
        renderer = scripts["render.js"]
        assert "textContent" in renderer
        assert "createElement" in renderer

    def test_the_adapter_never_sets_a_browser_controlled_header(self, scripts):
        """``Origin`` and ``Sec-Fetch-Site`` are forbidden headers in ``fetch``.

        Stage 5 requires both on every write, and the browser supplies both. A
        JavaScript attempt to set them is silently dropped, so code that looks
        like it satisfies the policy would ship a console whose writes all 403.
        """
        adapter = scripts["api.js"]
        for forbidden in ('"Origin"', "'Origin'", "Sec-Fetch-Site"):
            assert forbidden not in adapter, forbidden

    def test_no_cursor_is_ever_written_to_the_url(self, scripts):
        """Console cursors travel in ``X-Tickets-Cursor`` and nowhere else.

        Stage 5 refuses a ``cursor``-shaped query parameter outright, because a
        URL is the one place a token is guaranteed to reach an access log.
        """
        for name, source in scripts.items():
            for banned in (
                'searchParams.set("cursor"',
                "searchParams.set('cursor'",
                'set("next_cursor"',
                'set("page_token"',
            ):
                assert banned not in source, f"{name}: {banned}"

    def test_the_cursor_travels_in_the_documented_header(self, scripts):
        assert "X-Tickets-Cursor" in scripts["api.js"]


# =====================================================================
# 7 — the page has to load under the shipped CSP
# =====================================================================


class TestContentSecurityPolicy:

    def _directives(self) -> dict[str, list[str]]:
        policy = SECURITY_HEADERS["Content-Security-Policy"]
        parsed: dict[str, list[str]] = {}
        for chunk in policy.split(";"):
            parts = chunk.split()
            if parts:
                parsed[parts[0]] = parts[1:]
        return parsed

    def test_the_policy_still_forbids_inline_code(self):
        policy = SECURITY_HEADERS["Content-Security-Policy"]
        assert "unsafe-inline" not in policy
        assert "unsafe-eval" not in policy

    def test_every_referenced_asset_is_allowed_by_script_and_style_src(self, dom):
        directives = self._directives()
        assert directives["script-src"] == ["'self'"]
        assert directives["style-src"] == ["'self'"]
        for node in dom.find_all("script"):
            assert (node.get("src") or "").startswith("/")
        for node in dom.find_all("link"):
            href = node.get("href") or ""
            if "stylesheet" in (node.get("rel") or ""):
                assert href.startswith("/")

    def test_every_image_source_is_self_or_a_data_uri(self, dom):
        allowed = self._directives()["img-src"]
        assert allowed == ["'self'", "data:"]
        for node in dom.find_all("img"):
            source = node.get("src") or ""
            assert source.startswith("/") or source.startswith("data:"), source
        for node in dom.find_all("link"):
            if "icon" in (node.get("rel") or ""):
                href = node.get("href") or ""
                assert href.startswith("data:") or href.startswith("/"), href

    def test_the_api_is_reachable_under_connect_src(self, scripts):
        assert self._directives()["connect-src"] == ["'self'"]
        adapter = scripts["api.js"]
        for match in re.finditer(r"""["'](/api/[^"']*)["']""", adapter):
            assert match.group(1).startswith("/api/admin/v1")

    def test_every_asset_the_document_references_actually_resolves(self, client, dom):
        """A 404 on an asset is a broken page the CSP would never explain."""
        references = [
            node.get("src")
            for node in dom.find_all("script")
            if node.get("src")
        ] + [
            node.get("href")
            for node in dom.find_all("link")
            if "stylesheet" in (node.get("rel") or "") and node.get("href")
        ]
        assert references
        for path in references:
            assert client.get(path, headers=_auth()).status_code == 200, path


# =====================================================================
# 8, 9 and 10 — the stylesheet, the narrow layout, and icon buttons
# =====================================================================


class TestStylesheet:

    def test_focus_is_always_visible(self, css_source):
        assert ":focus-visible" in css_source
        focus_blocks = re.findall(r":focus-visible[^{]*\{([^}]*)\}", css_source)
        assert focus_blocks
        assert any("outline" in block for block in focus_blocks)

    def test_no_rule_removes_the_focus_ring_outright(self, css_source):
        assert not re.search(r"outline\s*:\s*(none|0)\s*;", css_source)

    def test_reduced_motion_is_respected(self, css_source):
        assert "@media (prefers-reduced-motion: reduce)" in css_source
        block = css_source.split("@media (prefers-reduced-motion: reduce)", 1)[1]
        # A skeleton that keeps pulsing is exactly the animation this query
        # exists to stop.
        assert "animation" in block[:1200]

    def test_the_canonical_breakpoint_is_the_shared_constant(self, css_source):
        assert f"max-width: {RESPONSIVE_BREAKPOINT_PX}px" in css_source

    def test_touch_targets_meet_the_shared_minimum(self, css_source):
        assert f"{MIN_TOUCH_TARGET_PX}px" in css_source

    def test_touch_controls_avoid_delayed_taps(self, css_source):
        assert "touch-action: manipulation" in css_source
        assert "-webkit-tap-highlight-color" in css_source

    def test_wide_content_scrolls_inside_its_own_container(self, css_source):
        assert "overflow-x: auto" in css_source

    def test_status_carries_a_cue_that_is_not_colour(self, css_source):
        """Colour alone fails for ~8% of men and every monochrome print-out."""
        assert "content: var(--pill-glyph)" in css_source
        assert css_source.count("--pill-glyph:") >= 4

    def test_the_table_header_sticks(self, css_source):
        assert "position: sticky" in css_source

    def test_the_hidden_attribute_outranks_the_author_display_rules(self, css_source):
        """`hidden` is a user-agent `display: none`, and this file overrides it.

        The toolbars are `display: grid` and several regions are `display: flex`,
        each of which beats the user-agent rule. Without an explicit override the
        inactive tab's filter form stays visible *and* keyboard-reachable, so a
        reviewer can tab into controls that filter a tab they are not on.
        """
        assert re.search(r"\[hidden\][^{]*\{[^}]*display:\s*none\s*!important", css_source)

    def test_both_colour_schemes_are_styled(self, css_source):
        assert "prefers-color-scheme: dark" in css_source


class TestNarrowLayout:

    def _narrow_block(self, css_source: str) -> str:
        marker = f"@media (max-width: {RESPONSIVE_BREAKPOINT_PX}px)"
        assert marker in css_source
        return css_source.split(marker, 1)[1]

    def test_the_desktop_header_row_is_hidden(self, css_source):
        block = self._narrow_block(css_source)
        assert re.search(r"thead[^{]*\{[^}]*display:\s*none", block)

    def test_each_row_becomes_a_labeled_card(self, css_source, scripts):
        block = self._narrow_block(css_source)
        assert 'content: attr(data-label)' in block
        # The label has to exist on the cell for the card to be readable.
        assert "data-label" in scripts["render.js"]

    def test_the_filters_open_as_an_accessible_sheet(self, css_source, dom, scripts):
        ids = {node.get("id") for node in dom.walk() if node.get("id")}
        assert "filter-sheet-toggle" in ids
        assert "filter-sheet-close" in ids
        assert "aria-expanded" in scripts["app.js"]
        block = self._narrow_block(css_source)
        assert "overscroll-behavior: contain" in block
        app = scripts["app.js"]
        assert 'setAttribute("role", "dialog")' in app
        assert 'setAttribute("aria-modal", "true")' in app
        assert "filterSheetFocusable" in app
        assert "filterSheetInerted" in app
        assert ".inert = true" in app
        assert 'classList.toggle("filter-sheet-open"' in app
        assert "Close filters" in scripts["preferences.js"]

    def test_the_mobile_announcement_does_not_clip_two_long_messages(self, css_source):
        block = self._narrow_block(css_source)
        assert ".announcement-rail p:last-child" in block
        announcement = block.split(".announcement-rail p:last-child", 1)[1]
        assert re.search(r"\{[^}]*display:\s*none", announcement)


class TestIconButtons:

    def test_every_static_button_has_an_accessible_name(self, dom):
        for node in dom.find_all("button"):
            name = node.all_text() or node.get("aria-label") or node.get("aria-labelledby")
            assert name, f"nameless button: {node!r}"

    def test_a_decorative_glyph_is_hidden_from_assistive_technology(self, dom):
        for node in dom.find_all("button"):
            if node.all_text():
                continue
            for child in node.find_all("svg", "span"):
                assert child.get("aria-hidden") == "true" or child.all_text()

    def test_created_buttons_go_through_one_labeled_factory(self, scripts):
        """Exactly one place creates a ``<button>``, and it demands a name.

        A second creation site is how an unlabeled icon button gets shipped, so
        the count is the assertion.
        """
        renderer = scripts["render.js"]
        assert renderer.count('createElement("button")') == 1
        factory = re.search(
            r"export function button\([^)]*\)\s*\{(.*?)\n\}", renderer, re.DOTALL
        )
        assert factory, "render.js exposes no button() factory"
        body = factory.group(1)
        assert "aria-label" in body
        assert "throw" in body, "the factory accepts a nameless button"
        for name, source in scripts.items():
            if name == "render.js":
                continue
            assert 'createElement("button")' not in source, name


class TestIconSprite:

    def test_the_sprite_is_valid_xml_with_only_symbols(self):
        root = ElementTree.parse(UI_ASSETS_DIRECTORY / "icons.svg").getroot()
        assert root.tag.endswith("svg")
        children = list(root)
        assert children
        for child in children:
            assert child.tag.endswith("symbol"), child.tag
            assert child.get("id")
            assert child.get("viewBox")

    def test_the_sprite_carries_no_script_or_foreign_object(self):
        source = (UI_ASSETS_DIRECTORY / "icons.svg").read_text(encoding="utf-8")
        for forbidden in ("<script", "foreignObject", "onload", "xlink:href"):
            assert forbidden not in source, forbidden

    def test_the_sprite_is_imported_without_an_html_parser(self, scripts):
        """The sprite is XML, parsed as XML, and only ``<symbol>`` survives.

        ``DOMParser`` with ``image/svg+xml`` is not an HTML sink, and the guard
        keeps it that way even if the file is ever edited by hand.
        """
        source = scripts["app.js"] + scripts["render.js"]
        assert "image/svg+xml" in source
        assert "symbol" in source


# =====================================================================
# The state and adapter contracts the plan pins by name
# =====================================================================


class TestStateContract:

    def test_the_store_declares_one_execution_collection(self, scripts):
        state = scripts["state.js"]
        assert "MODES" not in state
        assert "executionFilters" in state

    def test_the_store_models_every_load_state(self, scripts):
        state = scripts["state.js"]
        for phase in ("loading", "refreshing", "partial", "stale", "error"):
            assert phase in state, phase

    def test_the_url_carries_filters_but_never_a_cursor(self, scripts):
        """The serialized key list is the allowlist, so it is asserted directly.

        Reload therefore returns to page one of the URL's filters, which is the
        intended behaviour rather than a limitation: the alternative is a token
        in a bookmark.
        """
        state = scripts["state.js"]
        assert "URLSearchParams" in state
        keys = re.search(
            r"const URL_FILTER_KEYS = (?:Object\.freeze\()?\[(.*?)\]", state, re.DOTALL
        )
        assert keys, "state.js declares no URL_FILTER_KEYS allowlist"
        for banned in ("cursor", "token", "csrf"):
            assert banned not in keys.group(1), banned

    def test_the_cursor_back_stack_lives_only_in_memory(self, scripts):
        state = scripts["state.js"]
        assert "cursorStack" in state
        assert "pushState" not in state

    def test_a_selection_is_a_set_of_row_ids(self, scripts):
        assert "selectedIds" in scripts["state.js"]

    def test_a_reset_can_overrule_the_control_the_reviewer_is_using(self, scripts):
        """Two opposite requirements, reconciled by one counter.

        A background render must not overwrite a field mid-word, because text
        input is debounced and the store is deliberately behind it. But `Clear
        all` is a command *about* those fields: without an override, clearing
        while a field has focus leaves the old text on screen and, worse, in the
        next submitted query — which the server then refuses.
        """
        assert "formGeneration" in scripts["state.js"]
        app = scripts["app.js"]
        assert "formGeneration" in app
        assert "activeElement" in app


class TestAdapterContract:

    def test_every_request_is_same_origin_with_same_origin_credentials(self, scripts):
        adapter = scripts["api.js"]
        assert 'credentials: "same-origin"' in adapter
        assert adapter.count("fetch(") >= 1
        for match in re.finditer(r"""fetch\(\s*["']([^"']*)["']""", adapter):
            assert match.group(1).startswith("/"), match.group(1)

    def test_the_csrf_token_is_held_in_a_module_variable(self, scripts):
        adapter = scripts["api.js"]
        assert "X-CSRF-Token" in adapter
        assert re.search(r"let\s+\w*[Ss]ession\w*\s*=", adapter)

    def test_unsafe_requests_carry_an_idempotency_key(self, scripts):
        adapter = scripts["api.js"]
        assert "Idempotency-Key" in adapter
        assert "randomUUID" in adapter

    def test_versioned_writes_send_a_quoted_etag(self, scripts):
        adapter = scripts["api.js"]
        assert "If-Match" in adapter
        assert re.search(r'`"v\$\{', adapter) or '\'"v\'' in adapter

    @pytest.mark.parametrize(
        "status", ["401", "403", "409", "412", "428", "429", "502", "503"]
    )
    def test_every_documented_failure_maps_to_a_user_safe_message(self, scripts, status):
        assert status in scripts["api.js"], status

    def test_retry_after_is_honoured_with_a_fallback(self, scripts):
        adapter = scripts["api.js"]
        assert "Retry-After" in adapter
        # DevRev's 429 does not always carry the header, so a bare header read
        # would leave the UI hammering an upstream that asked it to stop.
        assert "FALLBACK_RETRY_AFTER_S" in adapter

    def test_stale_requests_are_cancelled(self, scripts):
        assert "AbortController" in scripts["api.js"]

    def test_the_request_id_is_surfaced_for_support(self, scripts):
        assert "X-Request-ID" in scripts["api.js"]

    def test_the_error_envelope_is_parsed_from_the_documented_shape(self, scripts):
        adapter = scripts["api.js"]
        assert "error" in adapter and "code" in adapter
        assert "current_version" in adapter


class TestFilterContract:

    def test_the_execution_queue_offers_no_title_substring_search(self, dom, scripts):
        """``title_contains`` is not part of the run-ledger grammar."""
        for name, source in scripts.items():
            assert "title_contains" not in source, name
        for node in dom.find_all("input"):
            if (node.get("type") or "text").lower() in {"search", "text"}:
                label = (node.get("aria-label") or "") + (node.get("placeholder") or "")
                assert "title" not in label.lower(), node.attrs

    def test_filters_are_run_scoped(self, scripts):
        source = scripts["app.js"] + scripts["state.js"] + scripts["api.js"]
        for field in ("executionId", "displayId", "route", "runStatus"):
            assert field in source, field
        for legacy in ("works.list", "listReviews", "createReview", "includeReversed"):
            assert legacy not in source, legacy

    def test_hydration_is_a_visibility_boundary_not_a_user_filter(self, dom, scripts):
        ids = {node.get("id") for node in dom.walk() if node.get("id")}
        assert "execution-hydration" not in ids
        assert "hydration_status" not in scripts["state.js"]
        list_adapter = scripts["api.js"].split(
            "export async function listExecutions", 1,
        )[1].split("export async function getReview", 1)[0]
        assert "hydrationStatus" not in list_adapter
        assert "hydration_status" not in list_adapter

    def test_timeout_is_a_first_class_run_status_filter(self, dom, scripts):
        status = [node for node in dom.find_all("select") if node.get("id") == "execution-status"]
        assert len(status) == 1
        values = {option.get("value") for option in status[0].find_all("option")}
        assert "timeout" in values
        assert '"timeout"' in scripts["state.js"]

    def test_an_unsupported_combination_is_not_filtered_client_side(self, scripts):
        app = scripts["app.js"]
        assert "UNSUPPORTED_FILTER_COMBINATION" in app

    def test_only_text_input_is_debounced(self, scripts):
        app = scripts["app.js"]
        assert "debounce" in app.lower()

    def test_active_filters_are_shown_as_clearable_chips(self, dom, scripts):
        ids = {node.get("id") for node in dom.walk() if node.get("id")}
        assert "active-filters" in ids
        assert "Clear all" in scripts["app.js"] or any(
            "Clear all" in node.all_text() for node in dom.find_all("button")
        )


class TestRowRendering:

    def test_a_rating_is_never_stars_alone(self, scripts):
        renderer = scripts["render.js"]
        assert "of 5" in renderer

    def test_dates_use_a_machine_readable_time_element(self, scripts):
        renderer = scripts["render.js"]
        assert 'createElement("time")' in renderer
        assert "dateTime" in renderer

    def test_a_comment_preview_is_text_only_and_clamped(self, scripts, css_source):
        assert "comment" in scripts["render.js"].lower()
        assert "-webkit-line-clamp" in css_source or "line-clamp" in css_source

    def test_truncated_text_is_reachable_without_a_tooltip(self, scripts):
        """``title`` is invisible to touch and to most keyboard users."""
        renderer = scripts["render.js"]
        assert "visually-hidden" in renderer or "sr-only" in renderer

    def test_each_row_uses_execution_identity_without_exposing_technical_columns(self, scripts):
        renderer = scripts["render.js"]
        assert '"data-execution-id": row.executionId' in renderer
        assert 'dataset: { action: "open", executionId: row.executionId }' in renderer
        assert "Review ticket" in renderer
        assert "Add to review queue" not in renderer

    def test_the_browser_never_creates_a_review_manually(self, scripts):
        assert "createReview" not in scripts["api.js"]
        assert "canCreateReview" not in scripts["app.js"]
        assert "MUTATING_ROLES" in scripts["evaluation.js"]

    def test_the_selection_checkbox_does_not_navigate(self, scripts):
        app = scripts["app.js"]
        assert "stopPropagation" in app or "closest" in app

    def test_a_row_opens_on_enter(self, scripts):
        app = scripts["app.js"]
        assert '"Enter"' in app

    def test_partial_pages_are_rendered_as_partial(self, scripts):
        renderer = scripts["render.js"] + scripts["app.js"]
        assert "partial" in renderer
        assert "devrev_unavailable" in renderer


class TestReviewerFirstLayout:

    def test_the_operational_page_has_no_marketing_hero_or_kpis(self, dom, scripts):
        forbidden = {
            "page-hero",
            "hero-copy",
            "hero-signal",
            "signal-orbit",
            "signal-node",
            "kpi-section",
            "kpi-strip",
            "kpi",
        }
        shipped = {
            token
            for node in dom.walk()
            for token in (node.get("class") or "").split()
        }
        assert forbidden.isdisjoint(shipped)
        assert "Ticket evaluation flow" not in dom.all_text()
        assert "pageTallies" not in scripts["app.js"]

    def test_the_page_opens_with_a_flat_reviewer_instruction(self, dom):
        heading = _by_id(dom, "operational-heading")
        assert heading.tag == "header"
        assert "operational-heading" in (heading.get("class") or "").split()
        assert _by_id(heading, "page-heading").tag == "h1"
        assert _by_id(heading, "page-heading").all_text() == "Ticket reviews"
        assert "CRM" in heading.all_text()

    @pytest.mark.parametrize(
        ("disclosure_id", "summary_text"),
        (
            ("technical-filter-details", "Technical filters"),
            ("technical-batch-details", "Technical batch actions"),
        ),
    )
    def test_technical_queue_controls_are_collapsed_disclosures(
        self, dom, disclosure_id, summary_text,
    ):
        disclosure = _by_id(dom, disclosure_id)
        assert disclosure.tag == "details"
        assert disclosure.get("open") is None
        summaries = [child for child in disclosure.children if child.tag == "summary"]
        assert len(summaries) == 1
        assert summaries[0].all_text() == summary_text

    def test_outcome_is_a_primary_filter_with_stable_route_values(self, dom):
        form = _by_id(dom, "filters-executions")
        outcome = _by_id(dom, "execution-route")
        field = outcome.parent

        assert field is not None
        assert "field" in (field.get("class") or "").split()
        assert field.parent is form
        assert _by_id(dom, "technical-filter-details") not in list(outcome.ancestors())

        labels = [
            node
            for node in field.find_all("label")
            if node.get("for") == "execution-route"
        ]
        assert [label.all_text() for label in labels] == ["Outcome"]
        assert [
            (option.get("value") or "", option.all_text())
            for option in outcome.find_all("option")
        ] == [
            ("", "Any outcome"),
            ("knowledge_question", "Knowledge Question"),
            ("generate_response", "Generate Response"),
        ]

    def test_active_filter_names_the_route_as_outcome(self, scripts):
        state = scripts["state.js"]
        assert '["Outcome", "route", filters.route]' in state


class TestFeatureFlagBranching:

    def test_the_ui_branches_on_the_server_feature_flags(self, scripts):
        app = scripts["app.js"]
        assert "remediation_enabled" in app
        assert "import_export_enabled" not in app

    def test_no_absent_route_is_ever_called(self, scripts):
        """Stage 9's CSV import/export routes are absent from OpenAPI, not stubbed.

        Stage 8's ``/remediation-batches`` used to be listed here too. It is
        published now, so banning the substring would ban the real call; what
        replaces that guard is ``test_only_the_human_batch_routes_are_called``
        below, which is the assertion that actually mattered — the UI must not
        call the *agent's* routes.
        """
        for name, source in scripts.items():
            # A local module specifier is a file on disk, not a route. Everything
            # else in the file is still scanned; see the pattern's own comment.
            without_imports = _RELATIVE_MODULE_SPECIFIER.sub('""', source)
            for absent in ("/imports", "/exports"):
                assert absent not in without_imports, f"{name}: {absent}"


class TestRagExecutionDetailContract:

    def test_the_detail_has_a_stable_region_for_every_audit_dimension(self, dom):
        ids = {node.get("id") for node in dom.walk() if node.get("id")}
        for required in (
            "run-summary",
            "run-answer",
            "run-rationale",
            "run-diagnostics",
            "run-gaps",
            "run-sources",
            "run-chunks",
            "run-metadata",
            "hydration-status",
        ):
            assert required in ids, required

    def test_the_detail_controller_reads_the_execution_envelope(self, scripts):
        detail = scripts["detail.js"]
        answer_planner = scripts["answer-presentation.js"]
        consumers = detail + answer_planner
        for field in (
            "execution",
            "generatedAnswer",
            "classificationReasoning",
            "outcomeReason",
            "diagnostics",
            "gaps",
            "modelMetadata",
            "timingMetadata",
            "hydrationStatus",
        ):
            assert field in consumers, field
        evidence = scripts["evidence.js"]
        for field in ("sourceArticles", "chunkEvidence"):
            assert field in evidence, field
        adapter = scripts["api.js"]
        for wire_field in (
            "generated_answer",
            "classification_reasoning",
            "outcome_reason",
            "source_articles",
            "chunk_evidence",
            "model_metadata",
            "timing_metadata",
            "hydration_status",
        ):
            assert wire_field in adapter, wire_field

    def test_evidence_renderer_covers_bounded_retrieval_detail(self, scripts):
        evidence = scripts["evidence.js"]
        for field in ("sourceArticles", "chunkEvidence", "contentHash", "preview"):
            assert field in evidence, field

    def test_rag_evidence_panel_uses_the_persisted_execution_evidence(self, scripts):
        detail = scripts["detail.js"]
        assert "function executionEvidenceSummary" in detail
        assert "current.execution" in detail
        assert "persisted RAG execution recorded" in detail
        assert "executionEvidenceCounts" in detail
        assert "renderExecutionEvidence(dom.runSources, dom.runChunks, current.execution)" in detail
        assert detail.count("renderExecutionEvidence(") == 1

    def test_hydration_is_rendered_as_an_authorized_trust_signal(self, scripts):
        detail = scripts["detail.js"]
        assert "DevRev context loaded for this execution." in detail
        assert "RAG execution remains available while enrichment continues" not in detail
        assert "RAG execution and durable review remain visible" not in detail

    def test_invocation_attempt_identity_survives_adapter_and_technical_detail(
        self, scripts,
    ):
        adapter = scripts["api.js"]
        for wire_field in ("invocation_id", "attempt", "lease_epoch"):
            assert wire_field in adapter
        detail = scripts["detail.js"]
        assert 'summaryRow("Invocation ID"' in detail
        assert 'summaryRow("Attempt"' in detail
        assert 'summaryRow("Lease epoch"' in detail

    def test_initial_conversation_is_loaded_by_execution_id(self, scripts):
        detail = scripts["detail.js"]
        assert "async function loadInitialConversation" in detail
        assert "await loadInitialConversation(ref)" in detail

    def test_reasoning_is_labeled_as_explicit_rationale_not_hidden_thought(self, dom):
        visible = dom.all_text()
        assert "Structured rationale" in visible
        assert "hidden chain-of-thought" in visible

    def test_only_the_human_batch_routes_are_called(self, scripts):
        """The browser never reaches an agent-only endpoint.

        Claim, heartbeat, materialize, and release belong to one verified service
        account. A UI that called any of them would be a UI that could impersonate
        the agent, so their absence is checked here rather than trusted to review.
        """
        blob = "\n".join(scripts.values())
        for absent in (
            "/claim",
            "/heartbeat",
            ":materialize",
            ":release",
            "lease_token",
            "leaseToken",
        ):
            assert absent not in blob, absent

    def test_the_batch_routes_the_ui_does_call_are_the_documented_ones(self, scripts):
        """The five human actions, plus the two bounded reads and the prompt.

        The colon-suffixed actions are built by one helper, so the action *name*
        is what appears in the source; asserting on the assembled path would only
        assert that string interpolation works.
        """
        api_source = scripts["api.js"]
        assert "remediation-batches" in api_source
        for action in ('"ready"', '"cancel"', '"start-verification"', '"complete"', '"extend-lease"'):
            assert f"batchAction(batchId, {action}" in api_source, action
        assert "/prompt" in api_source
        assert "/items" in api_source
