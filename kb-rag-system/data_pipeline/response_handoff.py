"""Business handoff is independent of retrieval confidence or technical success."""

from collections.abc import Mapping
from typing import Any


def response_requires_review(response: Any, metadata: Any = None) -> bool:
    """A blocked/ambiguous draft cannot become publishable through a high score.

    A definitive, supported denial can be returned normally. Explicit escalation
    and incomplete question coverage still require an advisor, even on success.
    """
    if isinstance(metadata, Mapping) and metadata.get("human_review_required") is True:
        return True
    if not isinstance(response, Mapping):
        return False
    if response.get("outcome") in ("blocked_missing_data", "ambiguous_plan_rules"):
        return True
    escalation = response.get("escalation")
    return isinstance(escalation, Mapping) and escalation.get("needed") is True
