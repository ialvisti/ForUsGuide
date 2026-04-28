import sys
from pathlib import Path


RAG_TESTING_DIR = Path(__file__).resolve().parent.parent / "rag-testing"
sys.path.insert(0, str(RAG_TESTING_DIR))

from ground_truth import validate_facts


def test_negated_gr31_hardship_auto_qualification_is_not_hard_failure():
    _warnings, failures = validate_facts(
        "GR-31",
        (
            "Do not assume that your rented house being sold automatically "
            "qualifies for a hardship withdrawal."
        ),
    )

    assert failures == []


def test_positive_gr31_hardship_auto_qualification_is_hard_failure():
    _warnings, failures = validate_facts(
        "GR-31",
        "Your rented house being sold automatically qualifies for a hardship withdrawal.",
    )

    assert failures
