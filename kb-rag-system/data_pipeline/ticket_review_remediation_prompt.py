"""Rendering of the reusable Codex remediation prompt.

Why this is its own module
--------------------------
The prompt is the one artifact that crosses from the console into a developer's
terminal, so the rules about what may enter it are worth stating in one place
rather than inline in a route:

*   it is built from a **checked-in template** that ships inside the runtime
    image, never from a string assembled at request time;
*   the only values substituted are five validated identifiers — console URL,
    environment, batch id, repository id, expected base ref. No ticket title,
    body, participant, reviewer comment, token, or key can reach it, because
    nothing else is ever passed in;
*   a placeholder value may not contain a newline or a control character. The
    rendered text is Markdown that a person will paste into an agent session and
    partly copy into a shell, so a value carrying ``\\n`` could append an
    instruction or a command that the template never authorized;
*   the size of the result is a function of the template and those five values
    alone. Two batches over completely different reviews render byte-identical
    prompts when their identifiers are the same length, which is what makes the
    response safe to serve uncached to any authorized remediator.

The template digest is recorded on the batch at creation time, so the record says
which instructions a batch was created against. When the deployed template has
moved on since then, :func:`render_remediation_prompt` says so *in the prompt
text* rather than failing: the agent's report then carries the discrepancy to the
human, which is more useful than a 409 nobody can act on.
"""

from __future__ import annotations

import hashlib
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Mapping, Optional

#: Bumped only when the *instructions* change in a way an operator must notice.
#: The SHA-256 is the precise identifier; this is the human-readable one.
PROMPT_TEMPLATE_VERSION = "1"

PROMPT_TEMPLATE_NAME = "ticket_review_remediation.md"
AGENT_PROMPTS_DIRECTORY = Path(__file__).resolve().parent / "agent_prompts"
PROMPT_TEMPLATE_PATH = AGENT_PROMPTS_DIRECTORY / PROMPT_TEMPLATE_NAME

#: Every placeholder the template is allowed to contain. Rendering fails if the
#: template grows one that is not listed here, so a new substitution cannot be
#: introduced without also deciding what validates it.
PROMPT_PLACEHOLDERS = (
    "console_url",
    "environment",
    "batch_id",
    "repo_id",
    "expected_base_ref",
)

#: A generous ceiling for one substituted identifier. Long enough for a Cloud Run
#: URL and a fully-qualified ref, short enough that no field can dominate the
#: rendered document.
MAX_PLACEHOLDER_LENGTH = 512

_DRIFT_NOTICE = (
    "\n> **Template drift.** This batch was created against remediation prompt "
    "template `{recorded}`, and the deployed template is `{current}`. The "
    "instructions above are the deployed ones. Report this difference in your "
    "final summary so a human can decide whether the batch is still valid.\n"
)


class PromptRenderError(ValueError):
    """A placeholder value, or the template itself, is not usable."""


def _control_free(value: str) -> bool:
    """True when no character is a control character or a line separator.

    ``unicodedata.category`` rather than a blocklist: ``Cc`` covers the ASCII
    controls, and ``Zl``/``Zp``/``Cf`` cover the ones a blocklist of ``\\n`` and
    ``\\r`` would miss — U+2028, U+2029, and the bidirectional overrides that can
    make a rendered command read as something other than what it runs.
    """
    return all(
        unicodedata.category(char) not in {"Cc", "Cf", "Zl", "Zp"} for char in value
    )


def validated_placeholder(name: str, value: object) -> str:
    """Return ``value`` if it may be substituted, else raise."""
    if not isinstance(value, str):
        raise PromptRenderError(f"{name} must be a string")
    candidate = value.strip()
    if not candidate:
        raise PromptRenderError(f"{name} is required to render the prompt")
    if len(candidate) > MAX_PLACEHOLDER_LENGTH:
        raise PromptRenderError(f"{name} is too long to substitute")
    if not _control_free(candidate):
        raise PromptRenderError(f"{name} may not contain a control character")
    if any(char.isspace() for char in candidate):
        # None of the five identifiers legitimately contains whitespace, and each
        # one is substituted into a shell command in the rendered instructions,
        # where a space would split one argument into two.
        raise PromptRenderError(f"{name} may not contain whitespace")
    if "{{" in candidate or "}}" in candidate:
        # A value that re-introduces placeholder syntax could otherwise be
        # substituted into a position that a second pass would expand.
        raise PromptRenderError(f"{name} may not contain template syntax")
    return candidate


@lru_cache(maxsize=1)
def template_text() -> str:
    """The template as shipped, read once.

    A missing template is a packaging failure, not a caller error: the file is a
    runtime asset and ``tests/test_container_contract.py`` exists to keep it in
    the image. The message says so, because that is the fix.
    """
    try:
        text = PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - packaging failure
        raise PromptRenderError(
            f"the remediation prompt template is missing from this build "
            f"({PROMPT_TEMPLATE_NAME}); check .dockerignore"
        ) from exc
    if not text.strip():
        raise PromptRenderError("the remediation prompt template is empty")
    return text


@lru_cache(maxsize=1)
def template_sha256() -> str:
    """SHA-256 of the template bytes exactly as they ship."""
    return hashlib.sha256(template_text().encode("utf-8")).hexdigest()


def render_remediation_prompt(
    *,
    console_url: str,
    environment: str,
    batch_id: str,
    repo_id: str,
    expected_base_ref: str,
    recorded_template_sha256: Optional[str] = None,
) -> str:
    """Substitute the five validated identifiers into the shipped template.

    ``recorded_template_sha256`` is what the batch recorded at creation. When it
    disagrees with the deployed template, a bounded notice is appended; when it
    is absent or agrees, the output is the template with its placeholders filled.
    """
    values: Mapping[str, str] = {
        name: validated_placeholder(name, value)
        for name, value in (
            ("console_url", console_url),
            ("environment", environment),
            ("batch_id", batch_id),
            ("repo_id", repo_id),
            ("expected_base_ref", expected_base_ref),
        )
    }
    text = template_text()
    for name, value in values.items():
        text = text.replace(f"{{{{{name}}}}}", value)
    if "{{" in text:
        raise PromptRenderError(
            "the remediation prompt template carries an unknown placeholder"
        )
    current = template_sha256()
    if recorded_template_sha256 and recorded_template_sha256 != current:
        text += _DRIFT_NOTICE.format(
            recorded=recorded_template_sha256[:12], current=current[:12]
        )
    return text


__all__ = [
    "AGENT_PROMPTS_DIRECTORY",
    "MAX_PLACEHOLDER_LENGTH",
    "PROMPT_PLACEHOLDERS",
    "PROMPT_TEMPLATE_NAME",
    "PROMPT_TEMPLATE_PATH",
    "PROMPT_TEMPLATE_VERSION",
    "PromptRenderError",
    "render_remediation_prompt",
    "template_sha256",
    "template_text",
    "validated_placeholder",
]
