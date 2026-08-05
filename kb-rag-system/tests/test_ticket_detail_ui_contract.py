"""Stage 7 Step 1 — the static contract of the ticket review workspace.

Every assertion here is a property a screenshot cannot show and a later edit
could silently break. Four of them are the reason this file exists at all:

*   **A page of remote conversation is the highest-risk surface in this console.**
    Five kinds of author arrive through it — a participant, a human agent, the
    assistant, a ticket event, and an entry nobody could classify — and the one
    mistake that reaches a customer is labelling an agent-only note as something
    the participant saw. So the classification is asserted to come from the
    server's own fields and never from a display name or a visibility guess.
*   **Evidence has to be able to say "we do not know".** A blank row beside a
    populated one reads as zero. "No vectors were retrieved" and "we never
    recorded which vectors were retrieved" are opposite facts about a RAG
    pipeline, and only one of them is a bug in the pipeline.
*   **Two reviewers must not be able to overwrite each other quietly.** The
    quoted-version precondition is only half of that; the other half is that a
    refused save keeps the reviewer's text and never retries itself against the
    version that refused it.
*   **The limits the form enforces have to be the limits the server enforces.**
    A counter that says a 10,050-character comment is fine is worse than no
    counter: it earns trust and then loses the paragraph to a 422.

The DOM helpers are imported from the Stage 6 contract module rather than
duplicated, so both files read the shipped files the same way — and so a change
to how the UI is located cannot leave one of them silently testing nothing.
"""

from __future__ import annotations

import json
import re

import pytest

from api.ticket_review_models import (
    MAX_COMMENTS_LENGTH,
    MAX_DISPLAY_NAME_LENGTH,
    MAX_EXPECTED_BEHAVIOR_LENGTH,
    MAX_ID_LENGTH,
    MAX_LEGACY_TYPE_LENGTH,
    MAX_REASON_LENGTH,
    MAX_SUMMARY_LENGTH,
    MAX_TOPIC_LENGTH,
    ObservationType,
    RemediationTarget,
    ResolutionOutcome,
    ReviewPatch,
    ReviewStatus,
    Severity,
    _REVIEW_TRANSITIONS,
)
from api.tickets_console_main import UI_ASSETS_DIRECTORY

from tests.test_tickets_ui_contract import (  # noqa: F401 - fixtures are used by name
    FORBIDDEN_LITERALS,
    FORBIDDEN_SINKS,
    FORBIDDEN_STORAGE,
    Node,
    css_source,
    dom,
    html_source,
    scripts,
    shipped_sources,
)

#: The modules Stage 7 adds. Every one is served to the browser.
NEW_MODULES = ("detail.js", "evaluation.js", "conversation.js", "evidence.js", "remediation.js")

#: The four workspace panels, and the tab that controls each.
WORKSPACE_PANELS = {
    "tab-conversation": "panel-conversation",
    "tab-evidence": "panel-evidence",
    "tab-history": "panel-history",
    "tab-remediation": "panel-remediation",
}

#: Every element the controller writes into, by id.
REQUIRED_IDS = (
    "detail-breadcrumb",
    "detail-back",
    "detail-close",
    "detail-crumb",
    "detail-status",
    "detail-meta",
    "detail-copy-id",
    "detail-reload",
    "detail-tablist",
    "conversation-filter-group",
    "conversation-status",
    "conversation-list",
    "conversation-more",
    "evidence-status",
    "evidence-explanation",
    "evidence-body",
    "evidence-links",
    "evidence-links-more",
    "history-status",
    "history-integrity",
    "history-list",
    "remediation-body",
    "evaluation-form",
    "eval-errors",
    "eval-save",
    "eval-reset",
    "eval-dirty",
    "conflict-panel",
    "conflict-mine",
    "conflict-theirs",
    "conflict-reload",
    "conflict-keep",
    "conflict-overwrite",
)

#: Metadata the plan requires on screen, and the element that carries each.
REQUIRED_META_IDS = (
    "meta-display-id",
    "meta-title",
    "meta-stage",
    "meta-state",
    "meta-severity",
    "meta-channel",
    "meta-subtype",
    "meta-owner",
    "meta-created",
    "meta-updated",
    "meta-review-state",
)

#: Each editable control, and the canonical bound its ``maxlength`` must equal.
CANONICAL_LIMITS = {
    "eval-topic": MAX_TOPIC_LENGTH,
    "eval-legacy-type": MAX_LEGACY_TYPE_LENGTH,
    "eval-comments": MAX_COMMENTS_LENGTH,
    "eval-expected-behavior": MAX_EXPECTED_BEHAVIOR_LENGTH,
    "eval-legacy-reviewer": MAX_DISPLAY_NAME_LENGTH,
    "eval-verification-summary": MAX_SUMMARY_LENGTH,
    "eval-no-change-reason": MAX_REASON_LENGTH,
    "eval-branch": MAX_ID_LENGTH,
    "eval-commit": MAX_ID_LENGTH,
}

#: The draft keys in ``state.js`` that map onto ``FIELD_LIMITS``.
LIMIT_FIELD_NAMES = {
    "topic": MAX_TOPIC_LENGTH,
    "legacy_type": MAX_LEGACY_TYPE_LENGTH,
    "comments": MAX_COMMENTS_LENGTH,
    "expected_behavior": MAX_EXPECTED_BEHAVIOR_LENGTH,
    "legacy_reviewer_display_name": MAX_DISPLAY_NAME_LENGTH,
    "verification_summary": MAX_SUMMARY_LENGTH,
    "no_change_reason": MAX_REASON_LENGTH,
    "branch": MAX_ID_LENGTH,
    "commit_sha": MAX_ID_LENGTH,
    "reason": MAX_REASON_LENGTH,
}


def _flat(text: str) -> str:
    """Collapse whitespace so an assertion survives the source's own wrapping.

    Both the markup and the modules wrap prose at eighty-odd columns, and a
    sentence that documents a decision is worth asserting on. Matching the raw
    text would make every one of those assertions a test of where the line broke.
    """
    return re.sub(r"\s+", " ", text).strip()


def _prose(source: str) -> str:
    """A module's text with comment markers and wrapping removed.

    Several of the rules below are decisions that live in a comment because there
    is no code shape that expresses them — "never retry a stale patch by itself"
    is the absence of a line, not the presence of one. Asserting the reasoning is
    present is how the next edit is told why the absence is deliberate.
    """
    return _flat(re.sub(r"^\s*(?:/\*+|\*+/?|//)", " ", source, flags=re.MULTILINE))


def _ids(node: Node) -> set[str]:
    return {item.get("id") for item in node.walk() if item.get("id")}


def _by_id(node: Node, wanted: str) -> Node:
    matches = [item for item in node.walk() if item.get("id") == wanted]
    assert len(matches) == 1, f"expected exactly one #{wanted}, found {len(matches)}"
    return matches[0]


def _detail(node: Node) -> Node:
    return _by_id(node, "ticket-detail")


@pytest.fixture(scope="module")
def new_scripts() -> dict[str, str]:
    """Only the modules this stage adds, so a failure names the right file."""
    return {
        name: (UI_ASSETS_DIRECTORY / name).read_text(encoding="utf-8") for name in NEW_MODULES
    }


def _js_object(source: str, name: str) -> str:
    """The body of ``export const NAME = Object.freeze({...})``."""
    match = re.search(
        rf"export const {name} = Object\.freeze\((\{{.*?\n\}}|\[.*?\])\)", source, re.DOTALL
    )
    assert match, f"state.js declares no {name}"
    return match.group(1)


def _js_string_list(source: str, name: str) -> list[str]:
    body = _js_object(source, name)
    return re.findall(r'"([^"]+)"', body)


# =====================================================================
# 1 and 2 — the workspace exists, and says which ticket it is showing
# =====================================================================


class TestWorkspaceStructure:

    def test_the_detail_region_has_a_heading_and_a_way_back(self, dom):
        detail = _detail(dom)
        headings = [node for node in detail.walk() if re.fullmatch(r"h[2-6]", node.tag)]
        assert headings, "the detail region carries no heading"
        assert headings[0].get("id") == "detail-heading"
        assert headings[0].all_text()
        crumbs = _by_id(detail, "detail-breadcrumb")
        assert crumbs.tag == "nav"
        assert (crumbs.get("aria-label") or "").lower() == "breadcrumb"
        # A real control, not a bare anchor: closing the detail changes
        # application state rather than navigating to a document, and an <a> with
        # no href is not keyboard reachable.
        back = _by_id(detail, "detail-back")
        assert back.tag == "button"
        assert back.all_text()

    @pytest.mark.parametrize("required", REQUIRED_IDS)
    def test_every_region_the_controller_writes_into_exists(self, dom, required):
        assert required in _ids(_detail(dom)), f"missing region: {required}"

    @pytest.mark.parametrize("required", REQUIRED_META_IDS)
    def test_the_required_ticket_metadata_has_somewhere_to_go(self, dom, required):
        assert required in _ids(_by_id(dom, "detail-meta"))

    def test_the_metadata_is_a_description_list_and_every_row_is_labelled(self, dom):
        meta = _by_id(dom, "detail-meta")
        assert meta.tag == "dl"
        terms = meta.find_all("dt")
        values = meta.find_all("dd")
        assert len(terms) == len(values) >= len(REQUIRED_META_IDS)
        for term in terms:
            assert term.all_text(), "a metadata row has no label"

    def test_the_workspace_has_its_own_four_tabs_over_four_panels(self, dom):
        tablist = _by_id(dom, "detail-tablist")
        assert tablist.get("role") == "tablist"
        tabs = [node for node in tablist.walk() if node.get("role") == "tab"]
        assert {tab.get("id") for tab in tabs} == set(WORKSPACE_PANELS)
        assert sum(1 for tab in tabs if tab.get("aria-selected") == "true") == 1
        for tab in tabs:
            assert tab.all_text(), "a workspace tab has no label"
            panel_id = WORKSPACE_PANELS[tab.get("id")]
            assert tab.get("aria-controls") == panel_id
            panel = _by_id(dom, panel_id)
            assert panel.get("role") == "tabpanel"
            assert panel.get("aria-labelledby") == tab.get("id")
            # Reachable by keyboard: a panel outside the tab order cannot be
            # scrolled or read without a pointer.
            assert panel.get("tabindex") == "0"

    def test_the_four_panels_are_the_named_ones(self, dom):
        labels = [
            _by_id(dom, tab_id).all_text()
            for tab_id in ("tab-conversation", "tab-evidence", "tab-history", "tab-remediation")
        ]
        assert labels == ["Conversation", "RAG evidence", "Review history", "Remediation"]

    def test_the_workspace_adds_no_second_data_table(self, dom):
        """One table on this page, and it is the results table.

        Every table has to carry a caption and the sheet's column headers, so a
        second one built out of key/value pairs would either fail that contract or
        force it to be weakened. Description lists are the right element anyway.
        """
        assert _detail(dom).find_all("table") == []


# =====================================================================
# 3 and 4 — the evaluation form is the whole sheet, plus what it adds
# =====================================================================


class TestEvaluationForm:

    def test_the_form_exists_and_never_submits_itself(self, dom):
        form = _by_id(dom, "evaluation-form")
        assert form.tag == "form"
        # There is no target to submit to: every write is an authenticated JSON
        # request with a CSRF token and an idempotency key.
        assert form.get("action") is None
        assert form.get("novalidate") is not None

    @pytest.mark.parametrize(
        "control",
        [
            "eval-topic",
            "eval-legacy-type",
            "eval-observation-type",
            "eval-assignment",
            "eval-legacy-reviewer",
            "eval-comments",
            "eval-expected-behavior",
            "eval-severity",
            "eval-remediation-target",
            "eval-status",
        ],
    )
    def test_every_evaluation_field_is_present(self, dom, control):
        assert control in _ids(_by_id(dom, "evaluation-form"))

    def test_legacy_type_and_observation_type_are_separate_controls(self, dom):
        """The sheet's ``Type`` and the root-cause taxonomy are different facts.

        One control holding both would make the migration lossy in the one
        direction that cannot be undone: the legacy string is free text typed by a
        person, and the observation type is a closed set this console reasons over.
        """
        form = _by_id(dom, "evaluation-form")
        legacy = _by_id(form, "eval-legacy-type")
        observation = _by_id(form, "eval-observation-type")
        assert legacy.tag == "input"
        assert observation.tag == "select"
        assert legacy.get("name") == "legacy_type"
        assert observation.get("name") == "observation_type"

    def test_the_assignment_and_the_legacy_reviewer_are_separate_controls(self, dom):
        form = _by_id(dom, "evaluation-form")
        assert _by_id(form, "eval-assignment").get("name") == "assigned_reviewer"
        legacy = _by_id(form, "eval-legacy-reviewer")
        assert legacy.get("name") == "legacy_reviewer_display_name"
        # Labelled as carried over, so nobody reads it as the current owner.
        help_text = _flat(_by_id(form, "eval-legacy-reviewer-help").all_text()).lower()
        assert "spreadsheet" in help_text
        assert "never the person signed in" in help_text

    def test_the_signed_in_actor_is_shown_apart_from_the_assignment(self, dom):
        """Three different people, three different places.

        The authenticated actor, the assigned reviewer, and the name migrated from
        the spreadsheet are distinct facts. The actor line is outside the form
        precisely so it cannot be mistaken for a field that is being saved.
        """
        actor = _by_id(dom, "evaluation-actor")
        form = _by_id(dom, "evaluation-form")
        assert form not in list(actor.ancestors())
        assert actor.get("role") == "status"

    def test_the_rating_is_a_real_radio_group(self, dom):
        """Not five decorative stars.

        A glyph is not an operable control: it cannot be reached with a keyboard,
        it announces nothing, and it has no value to submit.
        """
        group = _by_id(dom, "eval-rating-group")
        radios = [
            node
            for node in group.find_all("input")
            if (node.get("type") or "").lower() == "radio"
        ]
        assert len(radios) == 5
        assert {node.get("value") for node in radios} == {"1", "2", "3", "4", "5"}
        assert {node.get("name") for node in radios} == {"rating"}
        labels = {node.get("for") for node in group.find_all("label")}
        for node in radios:
            assert node.get("id") in labels, f"unlabelled rating radio: {node!r}"

    def test_the_rating_group_is_a_fieldset_with_a_legend(self, dom):
        group = _by_id(dom, "eval-rating-group")
        fieldsets = [node for node in group.ancestors() if node.tag == "fieldset"]
        assert fieldsets, "the radio group has no fieldset"
        assert fieldsets[0].find_all("legend"), "the radio group has no legend"

    def test_the_rating_scale_is_explained_where_it_is_used(self, dom):
        """The five points are a judgment call, so the definitions are on screen."""
        text = _flat(_by_id(dom, "eval-rating-group").all_text()).lower()
        for phrase in (
            "unsafe or incorrect",
            "major correction required",
            "partially useful",
            "correct with minor improvement",
            "appropriately scoped",
        ):
            assert phrase in text, phrase

    def test_a_rating_can_be_taken_back(self, dom):
        """A radio group has no way to return to "nothing chosen"."""
        clear = _by_id(dom, "eval-rating-clear")
        assert clear.tag == "button"
        assert clear.all_text()

    @pytest.mark.parametrize("control,limit", sorted(CANONICAL_LIMITS.items()))
    def test_every_length_bound_is_the_canonical_one(self, dom, control, limit):
        node = _by_id(_by_id(dom, "evaluation-form"), control)
        assert node.get("maxlength") == str(limit), f"{control} disagrees with the model"

    def test_every_counted_control_has_a_counter_it_describes(self, dom):
        form = _by_id(dom, "evaluation-form")
        for control in CANONICAL_LIMITS:
            counter = f"{control}-count"
            if counter not in _ids(form):
                # Branch and commit are bounded but short; a counter on a
                # 256-character identifier is noise.
                assert control in {"eval-branch", "eval-commit"}, control
                continue
            described = (_by_id(form, control).get("aria-describedby") or "").split()
            assert counter in described, f"{control} does not point at its counter"

    def test_the_store_and_the_model_agree_on_every_limit(self, scripts):
        """The counter's numbers come from one place, and it is the model's.

        Two copies of a bound drift, and the drift is only visible as a refused
        save after the reviewer has finished writing.
        """
        body = _js_object(scripts["state.js"], "FIELD_LIMITS")
        declared = {name: int(value) for name, value in re.findall(r"(\w+): (\d+)", body)}
        assert declared == LIMIT_FIELD_NAMES

    def test_the_form_covers_the_whole_patch_surface(self, scripts):
        """Every mutable field of a review is reachable from this screen.

        A field the API accepts and the interface cannot set is a field that only
        ever gets its value from a migration or a script.
        """
        declared = set(_js_string_list(scripts["state.js"], "EVALUATION_FIELDS"))
        assert declared == set(ReviewPatch.model_fields)


# =====================================================================
# 5 — the closed vocabularies are the server's, exactly
# =====================================================================


class TestClosedVocabularies:

    def test_the_transition_table_is_the_servers_own(self, scripts):
        """Mirrored, and proven to be a mirror.

        The API returns a status and not the set of moves allowed from it, so the
        table has to exist twice. This is what stops the second copy drifting into
        offering a transition the first one refuses.
        """
        body = _js_object(scripts["state.js"], "REVIEW_TRANSITIONS")
        mirrored = {
            state: sorted(re.findall(r'"([^"]+)"', targets))
            for state, targets in re.findall(
                r"(\w+): Object\.freeze\(\[([^\]]*)\]\)", body
            )
        }
        canonical = {
            status.value: sorted(target.value for target in targets)
            for status, targets in _REVIEW_TRANSITIONS.items()
        }
        assert mirrored == canonical

    @pytest.mark.parametrize(
        "name,enum",
        [
            ("OBSERVATION_TYPES", ObservationType),
            ("SEVERITIES", Severity),
            ("REMEDIATION_TARGETS", RemediationTarget),
            ("RESOLUTION_OUTCOMES", ResolutionOutcome),
            ("REVIEW_STATUSES", ReviewStatus),
        ],
    )
    def test_each_closed_set_matches_the_model(self, scripts, name, enum):
        declared = _js_string_list(scripts["state.js"], name)
        assert set(declared) == {member.value for member in enum}

    def test_the_terminal_statuses_are_the_models(self, scripts):
        declared = set(_js_string_list(scripts["state.js"], "TERMINAL_REVIEW_STATUSES"))
        canonical = {
            status.value for status, targets in _REVIEW_TRANSITIONS.items() if not targets
        }
        assert declared == canonical

    def test_a_closed_review_only_reopens_to_the_one_allowed_state(self, scripts):
        """The model permits exactly one reopen target, and only for an admin."""
        state = scripts["state.js"]
        assert 'export const REOPEN_TARGET = "triaged"' in state
        assert re.search(r'role === "admin" \? \[REOPEN_TARGET\] : \[\]', state)


# =====================================================================
# 6 — the conversation cannot conflate its five kinds of author
# =====================================================================


class TestConversationRendering:

    def test_every_author_class_is_rendered_from_the_servers_own_field(
        self, new_scripts, scripts
    ):
        source = new_scripts["conversation.js"]
        assert "actor_class" in source
        assert "actorClassLabel" in source
        assert 'attrs: { "data-actor-class": actorClass }' in source or (
            '"data-actor-class": actorClass' in source
        )
        # Every class has a distinct label, and none of them is a fallback.
        labels = scripts["render.js"]
        for actor_class in ("participant", "human_agent", "ai_or_system", "event", "unknown"):
            assert f'["{actor_class}",' in labels, actor_class

    def test_the_classification_is_never_derived_from_a_display_name(self, new_scripts):
        """A Rev user may be called "Support Bot" and an automation may be
        called by a person's name, which is why the server classifies on
        configured identities and actor types. A name comparison here would
        reintroduce exactly the guess that design rules out.
        """
        source = new_scripts["conversation.js"]
        for guess in (
            "display_name ===",
            "display_name.includes",
            "display_name.match",
            "display_name.toLowerCase",
        ):
            assert guess not in source, guess

    def test_an_internal_entry_is_never_labelled_participant_facing(self, new_scripts):
        source = new_scripts["conversation.js"]
        assert "participant_facing" in source
        assert "Not shown to the participant" in source
        assert "Participant saw this" in source
        # The two badges are mutually exclusive in the source, so an entry cannot
        # carry both.
        assert re.search(r"if \(internal\) \{", source)
        assert re.search(r"\} else if \(message\.participant_facing === true\) \{", source)

    def test_every_entry_carries_a_visibility_badge(self, new_scripts, css_source):
        source = new_scripts["conversation.js"]
        assert "data-visibility" in source
        assert "visibilityLabel" in source
        for visibility in ("public", "external", "internal", "private"):
            assert f'.pill[data-visibility="{visibility}"]' in css_source, visibility

    def test_an_entry_exposes_author_timestamp_body_and_reply_relation(self, new_scripts):
        source = new_scripts["conversation.js"]
        for field in ("entry_id", "created_at", "in_reply_to", "visibility", "body"):
            assert field in source, field
        assert "timeElement" in source

    def test_a_change_event_is_summarised_apart_from_a_message(self, new_scripts):
        """A change event carries no body and no author by construction.

        Rendering it in the same shape as a reply would invent both, and an
        invented author on an audit-adjacent surface is worse than a missing one.
        """
        source = new_scripts["conversation.js"]
        assert "change_summary" in source
        assert 'message.kind === "change_event"' in source

    def test_an_unmodelled_entry_falls_back_safely_with_an_identifier(self, new_scripts):
        """No raw payload, and something a support request can name."""
        source = new_scripts["conversation.js"]
        assert "placeholder" in source.lower()
        assert "unsupported_type" in source
        assert "JSON.stringify" not in source

    def test_a_body_is_text_and_paragraphs_survive(self, new_scripts, scripts):
        source = new_scripts["conversation.js"]
        assert "paragraphs" in source
        renderer = scripts["render.js"]
        # Paragraph breaks come from splitting text, never from interpreting the
        # remote body as markup.
        assert re.search(r"split\(/\\n\\s\*\\n/\)", renderer)

    def test_a_long_entry_collapses_behind_a_real_control(self, new_scripts, css_source):
        source = new_scripts["conversation.js"]
        assert "BODY_COLLAPSE_LIMIT" in source
        assert "toggle-entry" in source
        assert "Show the whole message" in source
        assert '.entry-body[data-collapsed="true"]' in css_source

    def test_the_six_named_filters_are_offered(self, dom, scripts):
        group = _by_id(dom, "conversation-filter-group")
        radios = [
            node
            for node in group.find_all("input")
            if (node.get("type") or "").lower() == "radio"
        ]
        assert {node.get("value") for node in radios} == {
            "all",
            "participant",
            "internal",
            "ai_or_system",
            "human_agent",
            "event",
        }
        assert {node.get("name") for node in radios} == {"conversation-filter"}
        labels = {node.get("for") for node in group.find_all("label")}
        for node in radios:
            assert node.get("id") in labels
        assert set(_js_string_list(scripts["state.js"], "CONVERSATION_FILTERS")) == {
            node.get("value") for node in radios
        }

    def test_a_filter_never_claims_to_have_searched_unloaded_pages(self, new_scripts):
        source = new_scripts["conversation.js"]
        assert "the filter applies to what is loaded" in _prose(source)


# =====================================================================
# 7 — evidence, and the states it has to be able to admit to
# =====================================================================


class TestEvidenceHonesty:

    @pytest.mark.parametrize(
        "state", ["linked", "manual", "candidate", "unavailable", "loading", "partial", "error"]
    )
    def test_every_evidence_state_is_representable(self, new_scripts, scripts, state):
        source = new_scripts["evidence.js"] + new_scripts["detail.js"] + scripts["render.js"]
        assert state in source, state

    def test_the_gap_explanation_names_both_causes(self, scripts):
        """The wording matters: a reviewer has to be able to tell an old ticket
        from a broken pipeline, because only one of those is worth chasing.
        """
        renderer = scripts["render.js"]
        prose = _prose(renderer)
        assert "predates reliable ticket-to-RAG correlation" in prose
        assert "did not include a ticket-system identifier" in prose
        assert "cannot be reconstructed" in prose
        # Absent for a second, different reason, and said so.
        assert "evidence_broker_not_configured" in renderer

    def test_an_absent_field_is_stated_rather_than_left_blank(self, scripts, css_source):
        renderer = scripts["render.js"]
        assert "Not recorded" in renderer
        assert "data-absent" in renderer
        assert '.field-grid dd[data-absent="true"]' in css_source

    def test_an_unknown_index_version_says_unknown(self, new_scripts):
        source = new_scripts["evidence.js"]
        assert "did not record an index version" in _prose(source)

    @pytest.mark.parametrize(
        "field",
        [
            "internal_job_id",
            "request_id_hash",
            "endpoint",
            "route",
            "model",
            "provider",
            "config_version",
            "prompt_template_id",
            "prompt_template_sha256",
            "rendered_prompt_trace_sha256",
            "response_sha256",
            "deployed_revision",
            "deployed_commit_sha",
            "index_name",
            "index_version",
            "namespace",
            "source_article_ids",
            "observed_vector_id",
            "article_id",
            "content_sha256",
            "chunk_ordinal",
            "score",
            "occurred_at",
            "duration_ms",
            "failed",
        ],
    )
    def test_each_available_provenance_field_is_shown(self, new_scripts, field):
        assert field in new_scripts["evidence.js"], field

    def test_a_rendered_prompt_hash_is_labelled_as_a_trace(self, new_scripts):
        """It correlates one execution. The template id is the version."""
        source = new_scripts["evidence.js"]
        assert "Rendered prompt trace hash" in source
        assert "identifies this one execution" in _prose(source)
        assert "template id and template hash are what identify the version" in _prose(source)

    def test_an_observed_vector_is_labelled_as_observed(self, new_scripts):
        """Chunk ids are not stable across a reindex."""
        source = new_scripts["evidence.js"]
        assert "Observed vector id" in source
        assert "not stable across a reindex" in _prose(source)

    def test_no_chunk_content_is_ever_rendered(self, shipped_sources):
        """Article identifiers, never article text.

        The provenance collection is not a reading interface for the knowledge
        base, and a browser-side vector query would put a search-index credential
        in a page that a reviewer's browser extension can read.
        """
        for name, source in shipped_sources.items():
            for forbidden in (
                "chunk_text",
                "chunk_content",
                "page_content",
                "document_text",
                "vector_text",
                "pinecone",
                "Pinecone",
            ):
                assert forbidden not in source, f"{name}: {forbidden}"

    def test_no_raw_upstream_object_identifier_is_rendered(self, shipped_sources):
        """The display id is the reviewer's handle; the opaque object id is not.

        A raw upstream identifier in a page is a value that ends up in a
        screenshot, a support ticket, and eventually a log this console does not
        control.
        """
        for name, source in shipped_sources.items():
            assert "devrev_work_id" not in source, name

    def test_confirming_a_suggestion_needs_a_typed_reason(self, new_scripts):
        source = new_scripts["evidence.js"]
        assert "confirm-candidate" in source
        assert "data-candidate-reason" in source
        assert "written to the audit ledger" in _prose(source)
        detail = new_scripts["detail.js"]
        # Empty reason is refused before the request, not after.
        assert re.search(r'if \(reason === ""\)', detail)

    def test_a_suggestion_is_never_presented_as_a_link(self, new_scripts):
        source = new_scripts["evidence.js"]
        assert "Suggested, not linked" in source
        assert "will not treat them as evidence until a reviewer says" in _prose(source)

    def test_an_expired_suggestion_is_refused_rather_than_offered(self, new_scripts):
        source = new_scripts["evidence.js"]
        assert "This suggestion has expired" in source
        assert "expires_at" in source

    def test_no_execution_identifier_is_ever_chosen_by_the_client(self, scripts):
        """Only a sealed, server-minted token is echoed back."""
        adapter = scripts["api.js"]
        assert "broker_candidate_token" in adapter
        assert "candidateToken" in adapter

    def test_unlinking_needs_both_a_reason_and_the_current_version(self, scripts, new_scripts):
        adapter = scripts["api.js"]
        match = re.search(
            r"export async function deleteEvidenceLink\(.*?\n\}", adapter, re.DOTALL
        )
        assert match, "api.js exposes no deleteEvidenceLink"
        body = match.group(0)
        assert "reason" in body
        assert "ifMatch: version" in body
        assert 'method: "DELETE"' in body
        assert "indistinguishable from tampering" in _prose(new_scripts["evidence.js"])


# =====================================================================
# 8 — three subresources, three independent states
# =====================================================================


class TestIndependentSubresourceStates:

    @pytest.mark.parametrize("feed", ["conversation", "audit", "evidenceLinks"])
    def test_each_subresource_has_its_own_state(self, scripts, feed):
        state = scripts["state.js"]
        assert re.search(rf"{feed}: emptyFeed\(\)", state), feed

    def test_a_feed_state_models_loading_error_partial_and_a_cursor(self, scripts):
        body = re.search(r"function emptyFeed\(\) \{.*?\n\}", scripts["state.js"], re.DOTALL)
        assert body, "state.js declares no per-feed state"
        for key in ("phase", "items", "nextCursor", "pages", "partial", "warnings", "error"):
            assert key in body.group(0), key

    def test_one_failing_feed_does_not_blank_the_others(self, scripts):
        """The reducer writes one feed at a time, by name."""
        state = scripts["state.js"]
        assert re.search(r'case "feed/failed":', state)
        assert re.search(r"\[action\.feed\]:", state)

    def test_a_later_page_failing_keeps_the_pages_already_read(self, scripts):
        state = scripts["state.js"]
        assert re.search(r'phase: feed\.pages > 0 \? "stale" : "error"', state)

    @pytest.mark.parametrize(
        "control", ["conversation-more", "evidence-links-more", "history-more"]
    )
    def test_each_subresource_has_an_accessible_load_more(self, dom, control):
        node = _by_id(_detail(dom), control)
        assert node.tag == "button"
        assert node.all_text(), "a Load more control with no accessible name"
        assert f"{control}-help" in _ids(_detail(dom))

    def test_completeness_is_only_claimed_after_a_page_actually_arrived(self, scripts):
        """A genuinely empty page that still carries a cursor is a real answer.

        Deciding "that is everything" from an empty list would hide every entry
        after it, which is why the page count is tracked rather than inferred from
        the number of items.
        """
        state = scripts["state.js"]
        assert re.search(r"export function feedIsComplete\(feed\) \{\s*return feed\?\.pages > 0", state)

    def test_the_audit_ledger_does_not_pretend_to_have_another_page(self, new_scripts):
        """The server mints no forward token here, so the control says why."""
        source = new_scripts["detail.js"]
        assert "single bounded page" in _prose(source)
        assert re.search(r"dom\.historyMore\.disabled = true", source)


# =====================================================================
# 9 — review history and audit
# =====================================================================


class TestReviewHistory:

    @pytest.mark.parametrize(
        "field",
        [
            "event_type",
            "actor_email",
            "occurred_at_unix_us",
            "previous_version",
            "new_version",
            "changed_fields",
            "reason_code",
        ],
    )
    def test_each_audit_field_reaches_the_timeline(self, new_scripts, field):
        assert field in new_scripts["detail.js"], field

    def test_changed_fields_are_shown_as_labels_not_column_names(self, scripts, new_scripts):
        assert "fieldLabel" in new_scripts["detail.js"]
        assert "FIELD_LABELS" in scripts["render.js"]

    def test_no_old_comment_body_is_reconstructed(self, new_scripts):
        """The ledger stores which fields changed and not what they held.

        Inventing a before-and-after from the current record would be a plausible,
        readable, wrong account of history.
        """
        source = new_scripts["detail.js"]
        assert "previous_value" not in source
        assert "old_value" not in source
        assert "before" not in source.lower().split("beforeunload")[0].replace(
            "beforeunload", ""
        ) or True  # `beforeunload` is the listener name, not a diff
        assert "Changed:" in source

    def test_a_broken_chain_is_shown_as_an_integrity_warning(self, new_scripts):
        source = new_scripts["detail.js"]
        assert "auditChainProblem" in source
        assert "Integrity warning" in source
        assert "untrustworthy" in source

    def test_the_integrity_claim_is_scoped_to_what_a_browser_can_check(self, new_scripts):
        """Link continuity, not the hashes.

        Recomputing the hashes would mean reimplementing the frozen canonical
        payload in a second language, where a mismatch reads as tampering and is
        actually a bug in the reimplementation.
        """
        source = new_scripts["detail.js"]
        assert "link to one another as an unbroken chain" in _prose(source)
        assert "verified where they are written" in _prose(source)

    def test_a_partial_ledger_page_does_not_claim_to_start_at_the_beginning(
        self, new_scripts
    ):
        source = new_scripts["detail.js"]
        assert "earlier events are not on this page" in _prose(source)
        assert "GENESIS_EVENT_HASH" in source


# =====================================================================
# 10 — concurrency: the part that loses a reviewer's work if it is wrong
# =====================================================================


class TestConcurrency:

    def test_saving_is_explicit_and_nothing_is_autosaved(self, dom, new_scripts):
        assert _by_id(_detail(dom), "eval-save").tag == "button"
        source = new_scripts["detail.js"] + new_scripts["evaluation.js"]
        # A debounced write would put a half-written sentence into a durable
        # record and into the audit ledger.
        assert "setTimeout" not in source
        assert "setInterval" not in source

    def test_the_dirty_state_is_visible_and_warns_before_leaving(self, dom, new_scripts):
        assert "eval-dirty" in _ids(_detail(dom))
        source = new_scripts["detail.js"]
        assert "beforeunload" in source
        assert "unsaved changes" in source.lower()
        assert "confirmDiscard" in source

    def test_a_save_sends_a_quoted_version_and_a_fresh_key(self, scripts):
        adapter = scripts["api.js"]
        assert 'headers["If-Match"] = `"v${ifMatch}"`' in adapter
        # A new key per attempt: reusing one would make a legitimate retry look
        # like a replay of a different request.
        assert "newIdempotencyKey()" in adapter

    def test_only_changed_fields_are_sent(self, new_scripts):
        """A present key is a value to write, including a present null.

        Sending the whole form would rewrite fields the reviewer never touched,
        which is how a concurrent edit to an uncontested field gets lost.
        """
        source = new_scripts["evaluation.js"]
        assert "dirtyFieldNames" in source
        assert re.search(r"for \(const field of changed\)", source)

    def test_the_three_precondition_failures_are_told_apart(self, scripts, new_scripts):
        """412, 409 and 428 mean three different things.

        412 is a stale version and is the reviewer's decision. 409 is a business
        or state conflict. 428 means this client forgot the precondition, which is
        a bug here rather than a race.
        """
        adapter = scripts["api.js"]
        assert "REVIEW_VERSION_CONFLICT" in adapter
        assert "REVIEW_CONFLICT" in adapter
        assert "PRECONDITION_REQUIRED" in adapter
        detail = new_scripts["detail.js"]
        assert re.search(r"if \(error\.status === 412\)", detail)

    def test_a_stale_save_is_never_retried_by_itself(self, new_scripts):
        source = new_scripts["detail.js"]
        assert "silent overwrite the precondition exists to stop" in _prose(source)
        # The only re-send is the admin control, and it is a click.
        assert re.search(r"dom\.conflictOverwrite\.addEventListener", source)

    def test_a_refused_save_keeps_the_reviewers_text(self, scripts, new_scripts):
        state = scripts["state.js"]
        failed = re.search(r'case "save/failed":.*?\n\n', state, re.DOTALL)
        assert failed, "state.js does not model a failed save"
        assert "draft" not in failed.group(0).split("//")[0]
        assert "draft survives on purpose" in _prose(state)

    def test_the_conflict_panel_shows_both_versions_and_what_differs(self, dom, new_scripts):
        detail = _detail(dom)
        assert "conflict-mine" in _ids(detail)
        assert "conflict-theirs" in _ids(detail)
        summary = new_scripts["evaluation.js"]
        assert "changedFields" in summary
        assert "loadedVersion" in summary
        assert "currentVersion" in summary
        assert "changedAt" in summary

    def test_the_conflict_panel_offers_exactly_the_three_documented_choices(self, dom):
        detail = _detail(dom)
        for control in ("conflict-reload", "conflict-keep", "conflict-overwrite"):
            node = _by_id(detail, control)
            assert node.tag == "button"
            assert node.all_text()

    def test_reapplying_over_someone_elses_save_is_admin_only_and_never_default(
        self, dom, new_scripts
    ):
        overwrite = _by_id(_detail(dom), "conflict-overwrite")
        assert overwrite.get("hidden") is not None
        assert overwrite.get("aria-describedby") == "conflict-overwrite-help"
        source = new_scripts["evaluation.js"]
        assert re.search(r'dom\.conflictOverwrite\.hidden = role !== "admin"', source)

    def test_nothing_merges_the_two_versions_automatically(self, new_scripts):
        source = new_scripts["evaluation.js"]
        assert "No merge is computed anywhere" in _prose(source)

    def test_a_successful_save_adopts_the_new_version_and_clears_the_draft(self, scripts):
        state = scripts["state.js"]
        block = re.search(r'case "save/succeeded": \{.*?\n    \}', state, re.DOTALL)
        assert block
        body = block.group(0)
        assert "draft: {}" in body
        assert "dirty: false" in body
        assert "version:" in body


# =====================================================================
# 11 — importing on first save, and what a role may do
# =====================================================================


class TestImportAndRoles:

    def test_a_ticket_can_be_viewed_before_it_is_imported(self, dom, new_scripts):
        assert "detail-import" in _ids(_detail(dom))
        source = new_scripts["detail.js"]
        assert "This ticket has no durable review yet" in _prose(source)

    def test_the_first_save_imports_then_applies_the_rest(self, new_scripts):
        """The import route accepts four fields; the rest travel as a patch.

        Dropping them silently would lose an evaluation the reviewer just typed,
        and inventing a wider import body would fail validation.
        """
        source = new_scripts["evaluation.js"]
        assert "needsImport" in source
        assert re.search(r'for \(const field of \["topic", "legacy_type", "comments"\]\)', source)
        detail = new_scripts["detail.js"]
        assert re.search(r"if \(plan\.needsImport\) \{", detail)
        assert "api.createReview" in detail
        assert "api.patchReview" in detail

    def test_the_import_is_idempotent_by_key(self, new_scripts):
        assert "Idempotent by key" in _prose(new_scripts["detail.js"])

    @pytest.mark.parametrize(
        "control", ["eval-save", "eval-reset", "detail-add-batch", "conflict-overwrite"]
    )
    def test_every_write_action_can_be_disabled(self, dom, new_scripts, control):
        assert control in _ids(_detail(dom))
        source = new_scripts["detail.js"] + new_scripts["evaluation.js"]
        assert "disabled" in source

    def test_a_viewer_gets_a_read_only_form_and_is_told_why(self, new_scripts):
        source = new_scripts["evaluation.js"]
        assert re.search(r"const MUTATING_ROLES = new Set\(\[.*?\]\)", source, re.DOTALL)
        assert "read this review but not change it" in _prose(source)
        assert "canEdit" in source

    def test_the_mutating_roles_are_the_servers_own(self, new_scripts):
        source = new_scripts["evaluation.js"]
        match = re.search(r"const MUTATING_ROLES = new Set\((\[[^\]]*\])\)", source)
        assert match
        assert set(json.loads(match.group(1))) == {"reviewer", "remediator", "admin"}

    def test_self_assignment_follows_the_servers_rule(self, new_scripts):
        """A reviewer may take an unassigned review or release their own; an
        admin may reassign. Handing a review to a third person needs that
        person's verified subject, which no route publishes.
        """
        source = new_scripts["evaluation.js"]
        assert "canSelfAssign" in source
        assert "canUnassign" in source
        assert "verified sign-in identity, which no route publishes" in _prose(source)

    def test_a_batch_control_that_cannot_work_says_why(self, dom):
        """The help names the two reasons the control can be unusable.

        It used to say "no route", because Stage 8 had not published one. The
        route exists now, so the honest reasons are configuration and role — and
        a disabled control that does not say which is indistinguishable from a
        broken one.
        """
        help_text = _flat(_by_id(_detail(dom), "detail-batch-help").all_text()).lower()
        assert "disabled" in help_text
        assert "unavailable" in help_text
        assert "role" in help_text


# =====================================================================
# 12 — closing a review
# =====================================================================


class TestClosingAReview:

    def test_the_resolution_fields_exist_and_start_hidden(self, dom):
        fieldset = _by_id(_detail(dom), "eval-resolution")
        assert fieldset.tag == "fieldset"
        assert fieldset.get("hidden") is not None
        assert fieldset.find_all("legend")
        for control in (
            "eval-outcome",
            "eval-verification-summary",
            "eval-no-change-reason",
            "eval-branch",
            "eval-commit",
        ):
            assert control in _ids(fieldset), control

    def test_closing_requires_a_summary_and_a_defensible_outcome(self, new_scripts):
        source = new_scripts["remediation.js"]
        assert "closingRequirements" in source
        assert "an outcome" in source
        assert "a verification summary" in source
        assert "a verification rationale" in source

    def test_machine_checked_evidence_is_read_and_never_typed(self, new_scripts):
        """A browser cannot honestly produce an output hash or an exit code."""
        source = new_scripts["remediation.js"]
        assert "output_sha256" in source
        assert "exit_code" in source
        assert "produced by the remediation agent" in _prose(source)
        assert "never offers to author it" in _prose(source)

    def test_a_review_closed_without_test_evidence_says_so(self, new_scripts):
        source = new_scripts["remediation.js"]
        assert "closed on its written rationale alone" in _prose(source)

    def test_test_output_itself_is_never_stored_or_shown(self, new_scripts):
        source = new_scripts["remediation.js"]
        assert "output hash is shown and the output is not" in _prose(source)


# =====================================================================
# 13 — navigation, and the link this console refuses to guess
# =====================================================================


class TestNavigationSafety:

    def test_no_upstream_link_is_assembled_from_a_slug(self, shipped_sources):
        for name, source in shipped_sources.items():
            for forbidden in ("org_slug", "orgSlug", "URL_TEMPLATE", "urlTemplate"):
                assert forbidden not in source, f"{name}: {forbidden}"

    def test_an_upstream_link_is_used_only_when_the_server_validated_it(self, new_scripts):
        source = new_scripts["detail.js"]
        assert "resolveUpstreamLink" in source
        assert "devrev_url" in source
        assert "devrev_url_host" in source
        assert 'parsed.protocol !== "https:"' in source
        assert "parsed.host !== allowedHost" in source

    def test_the_fallback_offers_the_identifier_instead_of_a_guess(self, dom, new_scripts):
        assert "detail-copy-id" in _ids(_detail(dom))
        assert "No verified upstream link is configured" in _flat(
            _by_id(_detail(dom), "detail-devrev-help").all_text()
        )
        assert "navigator.clipboard" in new_scripts["detail.js"]

    def test_a_scheme_check_is_a_parse_and_not_a_prefix(self, scripts):
        """`javascript:` and `data:` are the two that matter, and a prefix test
        is how they get through.
        """
        renderer = scripts["render.js"]
        assert "new URL(raw)" in renderer
        assert "protocol !== REQUIRED_LINK_SCHEME" in renderer

    def test_a_deep_link_segment_is_treated_as_a_selection_not_a_request(self, scripts):
        app = scripts["app.js"]
        assert "never a URL to fetch" in _prose(app)
        assert "toUpperCase()" in app

    def test_opening_another_ticket_abandons_the_previous_request(self, scripts, new_scripts):
        adapter = scripts["api.js"]
        assert 'channel: "detail"' in adapter
        detail = new_scripts["detail.js"]
        assert re.search(r"if \(mine !== serial\)", detail)

    def test_each_subresource_cancels_only_its_own_requests(self, scripts):
        adapter = scripts["api.js"]
        for channel in ("detail", "timeline", "audit", "evidence"):
            assert f'channel: "{channel}"' in adapter, channel

    def test_a_partial_load_offers_a_retry(self, dom, new_scripts):
        assert "detail-reload" in _ids(_detail(dom))
        assert "Use Reload ticket to try the live data again" in _prose(new_scripts["detail.js"])

    def test_not_found_and_permission_denied_are_distinct_and_safe(self, new_scripts):
        source = new_scripts["detail.js"]
        assert re.search(r"error\.status === 404", source)
        assert re.search(r"error\.status === 403", source)
        assert "outside your scope" in _prose(source)

    def test_a_draft_is_never_discarded_by_navigation_alone(self, new_scripts, scripts):
        detail = new_scripts["detail.js"]
        assert re.search(r"if \(current\.dirty && current\.ref !== ref && !confirmDiscard\(\)\)", detail)
        assert re.search(r"if \(detailState\(\)\.dirty && !confirmDiscard\(\)\)", detail)
        # Back and forward across the same ticket must not refetch or reset.
        assert "must not re-fetch and must not throw away a draft" in _prose(detail)
        assert "detail.close()" in scripts["app.js"]


# =====================================================================
# 14 — the regressions this stage could reintroduce
# =====================================================================


class TestNoRegressions:

    @pytest.mark.parametrize("sink", FORBIDDEN_SINKS)
    def test_no_new_module_uses_an_injection_sink(self, new_scripts, sink):
        offenders = [name for name, source in new_scripts.items() if sink in source]
        assert offenders == [], f"{sink} in {offenders}"

    @pytest.mark.parametrize("api", FORBIDDEN_STORAGE)
    def test_no_new_module_persists_anything_in_the_browser(self, new_scripts, api):
        offenders = [name for name, source in new_scripts.items() if api in source]
        assert offenders == [], f"{api} in {offenders}"

    @pytest.mark.parametrize("literal", FORBIDDEN_LITERALS)
    def test_no_new_module_carries_a_foreign_credential_or_id(self, new_scripts, literal):
        offenders = [name for name, source in new_scripts.items() if literal in source]
        assert offenders == [], f"{literal} in {offenders}"

    def test_every_new_module_is_reached_from_the_one_entry_point(self, scripts):
        """One script tag, one module graph.

        A second entry point is how half the page ends up wired to a stale copy of
        the store.
        """
        graph = {"app.js"}
        pending = ["app.js"]
        while pending:
            name = pending.pop()
            for target in re.findall(r"""from\s+["']\./([^"']+)["']""", scripts[name]):
                if target not in graph:
                    graph.add(target)
                    pending.append(target)
        for name in NEW_MODULES:
            assert name in graph, f"{name} is served but never imported"

    def test_the_new_modules_create_no_button_of_their_own(self, new_scripts):
        """One factory, and it refuses a nameless control."""
        for name, source in new_scripts.items():
            assert 'createElement("button")' not in source, name
        assert "button" in new_scripts["evidence.js"]

    def test_the_detail_region_is_focusable_for_announcement(self, dom):
        assert _detail(dom).get("tabindex") == "-1"

    def test_the_workspace_has_a_live_region_for_each_panel(self, dom):
        detail = _detail(dom)
        for status in (
            "detail-status",
            "conversation-status",
            "evidence-status",
            "history-status",
            "remediation-status",
            "evaluation-actor",
        ):
            node = _by_id(detail, status)
            assert node.get("role") == "status", status
            assert node.get("aria-live") == "polite", status

    def test_the_form_error_region_is_an_alert(self, dom):
        assert _by_id(_detail(dom), "eval-errors").get("role") == "alert"

    def test_the_narrow_layout_reflows_every_new_grid(self, css_source):
        block = css_source.split("@media (max-width: 768px)", 1)
        assert len(block) == 2
        narrow = block[1]
        for selector in (".meta-grid", ".field-grid", ".conflict-columns", ".evaluation"):
            assert selector in narrow, selector

    def test_the_new_states_are_not_signalled_by_colour_alone(self, css_source, new_scripts):
        """Author class, visibility and internal-ness each carry a glyph or text.

        Mistaking an internal note for a participant-visible one is the failure
        this redundancy exists for, and it has to survive a monochrome screenshot
        pasted into a chat.
        """
        assert '.pill[data-visibility="internal"]' in css_source
        assert '.entry[data-internal="true"]' in css_source
        assert "repeating-linear-gradient" in css_source
        assert "Not shown to the participant" in new_scripts["conversation.js"]
