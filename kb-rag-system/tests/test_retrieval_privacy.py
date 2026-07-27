"""Outbound semantic-query privacy is a controlled-vocabulary boundary."""

from __future__ import annotations

import pytest

from data_pipeline.retrieval_privacy import (
    UnsafeRetrievalQuery,
    redact_retrieval_context,
    sanitize_retrieval_query,
)


@pytest.mark.parametrize(
    ("raw", "forbidden"),
    (
        (
            "Jane Doe wants a direct rollover to her new employer plan",
            ("jane", "doe"),
        ),
        (
            "Jane's 401(k) rollover is going to Fidelity",
            ("jane", "fidelity"),
        ),
        (
            "Send Jane Doe's rollover check to 742 Evergreen Terrace, Springfield",
            ("jane", "doe", "742", "evergreen", "terrace", "springfield"),
        ),
        (
            "Michael Ditton left Acme Corporation and needs a rollover",
            ("michael", "ditton", "acme", "corporation"),
        ),
    ),
)
def test_query_is_rebuilt_from_domain_concepts_not_redacted_free_text(
    raw: str, forbidden: tuple[str, ...]
) -> None:
    outbound = sanitize_retrieval_query(raw).lower()

    assert "rollover" in outbound
    for token in forbidden:
        assert token not in outbound


def test_address_or_identity_without_retirement_concept_fails_closed() -> None:
    with pytest.raises(UnsafeRetrievalQuery):
        sanitize_retrieval_query("Jane Doe lives at 742 Evergreen Terrace")


def test_retirement_semantics_survive_controlled_rebuild() -> None:
    outbound = sanitize_retrieval_query(
        "How does a terminated employee complete an indirect 401k rollover "
        "and avoid the 60-day tax penalty?"
    ).lower()

    for concept in ("termination", "indirect rollover", "401(k)", "tax", "penalty"):
        assert concept in outbound


def test_probe_text_maps_to_safe_neutral_concept() -> None:
    assert sanitize_retrieval_query("knowledge base article content") == (
        "retirement plan guidance"
    )


@pytest.mark.parametrize(
    "raw",
    (
        "ACH delivery",
        "census required data",
        "hardship",
        "Roth IRA rollover",
        "tax withholding",
        "knowledge base article content and rollover",
    ),
)
def test_controlled_query_boundary_is_idempotent(raw: str) -> None:
    once = sanitize_retrieval_query(raw)
    assert sanitize_retrieval_query(once) == once


def test_context_redaction_preserves_safe_enrichment_and_removes_pii() -> None:
    raw = (
        "enriched OUT rollover options for jane@example.com; "
        "SSN 123 45 6789; account number 1234 5678 9012; "
        "date of birth July 4, 1980"
    )

    context = redact_retrieval_context(raw)
    outbound = sanitize_retrieval_query(context)

    assert "enriched OUT rollover options" in context
    for sentinel in (
        "jane@example.com",
        "123 45 6789",
        "1234 5678 9012",
        "July 4, 1980",
    ):
        assert sentinel.lower() not in context.lower()
        assert sentinel.lower() not in outbound.lower()
    assert "rollover" in outbound.lower()
