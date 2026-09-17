"""Synthetic evidence: typed conversation, never a model-supplied digest."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from api.models import TicketInput


def snapshot():
    return {
        "type": "devrev_conversation_snapshot", "schema_version": 1,
        "ticket_id": "TKT-100001", "work_id": "don:synthetic:ticket:1",
        "subject": "Synthetic options question",
        "captured_at": "2026-09-17T01:00:00.000Z",
        "complete": True, "partial": False, "truncated": False,
        "initial_message": {
            "id": "don:synthetic:ticket:1", "author_id": "don:synthetic:rev_user:1",
            "author_role": "participant", "visibility": "external",
            "created_at": "2026-09-16T01:00:00.000Z",
            "updated_at": "2026-09-16T01:00:00.000Z",
            "body": "What are my options?",
        },
        "messages": [{
            "id": "don:synthetic:timeline_entry:1", "author_id": "don:synthetic:dev_user:1",
            "author_role": "advisor", "visibility": "external",
            "created_at": "2026-09-16T02:00:00.000Z",
            "updated_at": "2026-09-16T02:00:00.000Z",
            "body": "We will review the plan rules.",
        }],
    }


def ticket(value=None):
    return {"username": "Example Participant", "user_email": "person@example.test",
            "ticket_id": "TKT-100001", "email_subject": "Synthetic options question",
            "email_body": "What are my options?", "conversation_snapshot": value or snapshot()}


def test_typed_snapshot_survives_request_serialization():
    parsed = TicketInput.model_validate(ticket())
    assert parsed.model_dump(mode="json").get("conversation_snapshot") == snapshot()


@pytest.mark.parametrize("field,value", [
    ("ticket_id", "TKT-100002"), ("email_subject", "Different request"),
    ("email_body", "A later question"),
])
def test_snapshot_cannot_bind_different_consumed_input(field, value):
    raw = ticket(); raw[field] = value
    with pytest.raises(ValidationError):
        TicketInput.model_validate(raw)


@pytest.mark.parametrize("mutation", [
    lambda s: s.update(digest="a" * 64),
    lambda s: s.update(complete="true"),
    lambda s: s.update(partial=True),
    lambda s: s.update(messages=s["messages"] * 2),
    lambda s: s["messages"][0].update(visibility="internal"),
    lambda s: s["messages"][0].update(author_role="participant", author_id=None),
    lambda s: s["messages"][0].update(created_at="2026-09-18T01:00:00.000Z"),
    lambda s: s["messages"][0].update(updated_at="2026-09-15T01:00:00.000Z"),
    lambda s: s.update(captured_at="2026-09-17"),
    lambda s: s.update(work_id="don:synthetic:ticket:2"),
])
def test_invalid_or_model_claimed_snapshot_is_rejected(mutation):
    value = snapshot(); mutation(value)
    with pytest.raises(ValidationError):
        TicketInput.model_validate(ticket(value))


def test_partial_snapshot_remains_partial_and_unusable_as_complete_evidence():
    value = snapshot(); value.update(complete=False, partial=True, truncated=True)
    parsed = TicketInput.model_validate(ticket(value))
    ref = parsed.conversation_snapshot.reference()
    assert ref["complete"] is False and ref["partial"] is True and ref["truncated"] is True


def test_content_digest_is_stable_across_reads_but_changes_for_authorship_and_edits():
    base = TicketInput.model_validate(ticket()).conversation_snapshot.reference()
    value = snapshot(); value["captured_at"] = "2026-09-17T02:00:00.000Z"
    assert TicketInput.model_validate(ticket(value)).conversation_snapshot.reference() == base
    for key, change in [("author_role", "automation"), ("body", "Changed advice"),
                        ("updated_at", "2026-09-16T03:00:00.000Z")]:
        altered = deepcopy(value); altered["messages"][0][key] = change
        assert TicketInput.model_validate(ticket(altered)).conversation_snapshot.reference() != base


def test_orchestrator_passes_typed_conversation_without_enabling_legacy_history():
    from data_pipeline.ticket_orchestrator import TicketOrchestrator
    req = SimpleNamespace(ticket=TicketInput.model_validate(ticket()))
    data = TicketOrchestrator._build_ticket_data(None, req)
    assert data["ticket_messages"] == {}
    assert data.get("conversation_snapshot") == snapshot()


def test_legacy_request_does_not_gain_a_fabricated_snapshot():
    raw = ticket(); raw.pop("conversation_snapshot"); raw["ticket_messages"] = {"message_1": "untyped"}
    parsed = TicketInput.model_validate(raw)
    assert getattr(parsed, "conversation_snapshot", None) is None
    assert "ticket_messages" not in parsed.model_dump()


def test_question_grounding_uses_only_participant_statements_from_typed_messages():
    from data_pipeline.ticket_orchestrator import TicketOrchestrator
    value = snapshot()
    followup = deepcopy(value["messages"][0])
    followup.update(id="don:synthetic:timeline_entry:2", author_role="participant",
                    author_id="don:synthetic:rev_user:1", body="Can I keep the funds here?")
    value["messages"].append(followup)
    req = SimpleNamespace(ticket=TicketInput.model_validate(ticket(value)))
    assert TicketOrchestrator._evidenced_questions([
        followup["body"], value["messages"][0]["body"]], req) == [followup["body"]]


def test_extraction_prompt_distinguishes_roles_and_untrusted_content():
    from data_pipeline import prompts
    system, user = prompts.build_extract_inquiries_prompt({"ticketData": {"conversation_snapshot": snapshot()}})
    assert "author_role" in system and "untrusted" in system and "conversation_snapshot" in system
    assert '"author_role": "advisor"' in user


def test_initial_body_does_not_invent_edit_time_from_work_status_change():
    value = snapshot(); value["initial_message"]["updated_at"] = None
    parsed = TicketInput.model_validate(ticket(value))
    assert parsed.conversation_snapshot.initial_message.updated_at is None
    assert len(parsed.conversation_snapshot.reference()["digest"]) == 64


def test_new_optional_field_does_not_change_legacy_idempotency_fingerprint():
    from data_pipeline.ticket_job_models import fingerprint_request
    raw = {"ticket": ticket()}; raw["ticket"].pop("conversation_snapshot")
    original = fingerprint_request(raw)
    raw["ticket"]["conversation_snapshot"] = None
    assert fingerprint_request(raw) == original


def test_snapshot_read_time_is_not_a_different_logical_event():
    from data_pipeline.ticket_job_models import fingerprint_request
    raw = {"ticket": ticket()}; original = fingerprint_request(raw)
    raw["ticket"]["conversation_snapshot"]["captured_at"] = "2026-09-17T02:00:00.000Z"
    assert fingerprint_request(raw) == original
    raw["ticket"]["conversation_snapshot"]["messages"][0]["body"] = "A new condition"
    assert fingerprint_request(raw) != original


def test_python_binding_matches_shared_javascript_fixture():
    import json
    from pathlib import Path
    from data_pipeline.ticket_conversation import ConversationSnapshot
    value = json.loads((Path(__file__).parents[2] / "PA/n8n/fixtures/conversation-snapshot.fixture").read_text())
    assert ConversationSnapshot.model_validate(value).reference()["digest"] == (
        "814d1eb1b58a7e448bb538bdbd7b7ce1568c28237062c45eff8742579cee42e6"  # pragma: allowlist secret - public synthetic fixture digest
    )


@pytest.mark.parametrize("field", ["id", "author_id"])
def test_message_identifiers_use_devrev_ids_not_ambiguous_unicode_sort_order(field):
    value = snapshot(); value["messages"][0][field] = "don:synthetic:\U00010000"
    with pytest.raises(ValidationError):
        TicketInput.model_validate(ticket(value))


async def test_incomplete_history_cannot_become_a_safe_reply_after_successful_processing():
    from api.ticket_worker import run_ticket_job
    from data_pipeline.ticket_job_models import fingerprint_request, new_job_record
    from data_pipeline.ticket_job_repository import InMemoryTicketJobBackend, TicketJobRepository
    from tests.test_ticket_worker import FakeOrch, _worker_app
    value = snapshot(); value.update(complete=False, partial=True)
    payload = {"participant_id": "synthetic", "plan_id": "synthetic",
               "company_name": "Example", "company_status": "Ongoing", "ticket": ticket(value)}
    repo = TicketJobRepository(InMemoryTicketJobBackend())
    fp = fingerprint_request(payload)
    rec, _ = await repo.create_or_get(
        principal_id="default", idempotency_key=None, request_fingerprint=fp,
        candidate=new_job_record(principal_id="default", request_fingerprint=fp,
                                 mode="full", request_payload=payload, ticket_id="TKT-100001"))
    await run_ticket_job(_worker_app(repo, FakeOrch()), rec.job_id)
    result = await repo.get(rec.job_id)
    assert result.next_action.value == "human_review"
    assert result.per_inquiry_status[0]["participant_reply_safe"] is False
    assert result.per_inquiry_status[0]["human_review_required"] is True
