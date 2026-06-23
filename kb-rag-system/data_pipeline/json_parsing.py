"""
Defensive JSON parsing for LLM outputs.

The ticket-handler agents return JSON (object or array). LLMs occasionally wrap
output in markdown fences or prepend prose despite ``response_format`` being set.
These helpers strip fences and fall back to extracting the first JSON object /
array substring. They never raise — they return ``None`` on failure so callers
can branch to a safe default (mirrors ``inquiry_router._safe_parse_classifier_json``).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MARKDOWN_FENCE_RE = re.compile(
    r"\A\s*```(?:json)?\s*(.*?)\s*```\s*\Z",
    re.IGNORECASE | re.DOTALL,
)
_FIRST_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_FIRST_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


def _strip_fence(text: str) -> str:
    match = _MARKDOWN_FENCE_RE.match(text)
    return match.group(1).strip() if match else text


def _try_loads(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def parse_json_object(content: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse ``content`` into a dict, or return ``None``."""
    if not content or not content.strip():
        return None
    text = _strip_fence(content.strip())
    parsed = _try_loads(text)
    if parsed is None:
        m = _FIRST_OBJECT_RE.search(text)
        if m:
            parsed = _try_loads(m.group(0))
    if isinstance(parsed, dict):
        return parsed
    logger.warning("Expected a JSON object; got %r", content[:300])
    return None


def parse_json_array(content: Optional[str]) -> Optional[List[Any]]:
    """Parse ``content`` into a list, or return ``None``.

    Tolerates a single object returned instead of an array by wrapping it.
    """
    if not content or not content.strip():
        return None
    text = _strip_fence(content.strip())
    parsed = _try_loads(text)
    if parsed is None:
        m = _FIRST_ARRAY_RE.search(text)
        if m:
            parsed = _try_loads(m.group(0))
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        # An agent that should emit a one-element array sometimes emits the
        # bare object; normalize rather than fail.
        return [parsed]
    logger.warning("Expected a JSON array; got %r", content[:300])
    return None
