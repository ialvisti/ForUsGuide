"""Bounded, attributed PA input; content never appears in the public binding.

The authenticated producer supplies messages, not a trusted digest. This
contract does not authenticate DevRev itself: the producer must exhaust the
external timeline using its existing credential and report incomplete reads.
The canonical digest uses a positional JSON array (UTF-8, no whitespace),
shared with PA/n8n/conversation-snapshot.js. captured_at is read metadata and
is deliberately excluded so independent reads of unchanged content compare.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator

_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")


def instant(value: str) -> datetime:
    if not _INSTANT.fullmatch(value):
        raise ValueError("conversation timestamp must be a UTC instant")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class ConversationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(pattern=r"^don:[A-Za-z0-9_.:/+-]+$", max_length=256)
    author_id: str | None = Field(default=None, pattern=r"^don:[A-Za-z0-9_.:/+-]+$", max_length=256)
    author_role: Literal["participant", "advisor", "automation", "unknown"]
    visibility: Literal["external"]
    created_at: str
    updated_at: str | None
    body: str = Field(max_length=100_000)

    @field_validator("created_at", "updated_at")
    @classmethod
    def validate_instant(cls, value: str | None) -> str | None:
        if value is not None:
            instant(value)
        return value

    @model_validator(mode="after")
    def validate_attribution(self) -> Self:
        if self.author_role != "unknown" and not self.author_id:
            raise ValueError("attributed message requires author_id")
        if self.updated_at is not None and instant(self.updated_at) < instant(self.created_at):
            raise ValueError("message update precedes creation")
        return self

    def canonical_row(self) -> list[Any]:
        return [self.id, self.author_id, self.author_role, self.visibility,
                self.created_at, self.updated_at, self.body]


class ConversationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    type: Literal["devrev_conversation_snapshot"]
    schema_version: StrictInt = Field(ge=1, le=1)
    ticket_id: str = Field(pattern=r"^TKT-[A-Z0-9-]+$", max_length=64)
    work_id: str = Field(pattern=r"^don:[A-Za-z0-9_.:/+-]+$", max_length=256)
    subject: str = Field(max_length=1000)
    captured_at: str
    complete: StrictBool
    partial: StrictBool
    truncated: StrictBool
    initial_message: ConversationMessage
    messages: list[ConversationMessage] = Field(max_length=250)

    @field_validator("captured_at")
    @classmethod
    def validate_instant(cls, value: str) -> str:
        instant(value)
        return value

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        if self.initial_message.id != self.work_id:
            raise ValueError("initial message must identify the work body")
        if self.complete == (self.partial or self.truncated):
            raise ValueError("inconsistent conversation completeness")
        messages = [self.initial_message, *self.messages]
        if len({m.id for m in messages}) != len(messages):
            raise ValueError("duplicate conversation message")
        if any(instant(m.updated_at or m.created_at) > instant(self.captured_at) for m in messages):
            raise ValueError("message is newer than the snapshot")
        if sum(len(m.body.encode("utf-8")) for m in messages) > 200_000:
            raise ValueError("conversation content exceeds byte bound")
        # Sorting by the exact UTC string would misorder differing fractional
        # precision. Date plus stable ID makes pagination order irrelevant.
        self.messages.sort(key=lambda m: (instant(m.created_at), m.id))
        return self

    def reference(self) -> dict[str, Any]:
        preimage = [self.type, self.schema_version, self.ticket_id, self.work_id,
                    self.subject, self.complete, self.partial, self.truncated,
                    self.initial_message.canonical_row(),
                    [m.canonical_row() for m in self.messages]]
        encoded = json.dumps(preimage, ensure_ascii=False, separators=(",", ":"))
        return {"type": self.type, "schema_version": self.schema_version,
                "hash_algorithm": "sha256", "digest": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                "complete": self.complete, "partial": self.partial, "truncated": self.truncated}


def reference_from_payload(payload: dict[str, Any] | None, *, ticket_id: str | None,
                           created_at: datetime | None) -> dict[str, Any] | None:
    """Derive only from the retained request; legacy/malformed records fail closed."""
    ticket = (payload or {}).get("ticket")
    if not isinstance(ticket, dict) or ticket.get("conversation_snapshot") is None:
        return None
    try:
        snapshot = ConversationSnapshot.model_validate(ticket["conversation_snapshot"])
        if (snapshot.ticket_id != ticket_id or ticket.get("ticket_id") != ticket_id
                or snapshot.subject != ticket.get("email_subject")
                or snapshot.initial_message.body != (ticket.get("email_body") or "")
                or created_at is None or instant(snapshot.captured_at) > created_at):
            return None
        return snapshot.reference()
    except (ValueError, TypeError):
        return None


def participant_statements(ticket: Any) -> str:
    """Quote grounding never promotes advisor/automation text to participant facts."""
    snapshot = getattr(ticket, "conversation_snapshot", None)
    if snapshot is None:
        return f"{getattr(ticket, 'email_subject', '') or ''} {getattr(ticket, 'email_body', '') or ''}"
    return "\n".join([snapshot.subject, *[
        m.body for m in [snapshot.initial_message, *snapshot.messages]
        if m.author_role == "participant"
    ]])
