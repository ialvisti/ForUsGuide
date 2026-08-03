"""Firestore Standard durable-document validation contracts."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import _helpers
from google.cloud.firestore_v1 import Client, GeoPoint
from google.cloud.firestore_v1.types import Document


def test_rejects_array_directly_nested_inside_array_without_echoing_values():
    from data_pipeline.durable_document import (
        DurableDocumentValidationError,
        validate_durable_document,
    )

    sentinel = "private-value-must-not-appear"
    document = {
        "diagnostics": {
            "field_mapping": {
                "deterministic_mapped": {
                    "termination_date": [["census", sentinel]]
                }
            }
        }
    }

    with pytest.raises(DurableDocumentValidationError) as exc_info:
        validate_durable_document(document)

    assert exc_info.value.stats.invalid_nested_array_count == 1
    assert exc_info.value.reason_codes == ("nested_array",)
    assert sentinel not in str(exc_info.value)


def test_accepts_firestore_safe_document_and_reports_bounded_stats():
    from data_pipeline.durable_document import validate_durable_document

    document = {
        "job_id": "synthetic-job",
        "updated_at": datetime(2026, 8, 2, tzinfo=timezone.utc),
        "diagnostics": {
            "field_mapping": {
                "deterministic_mapped": {
                    "account_balance": [
                        {"module": "savings_rate", "field": "Account Balance"}
                    ]
                }
            }
        },
    }

    stats = validate_durable_document(document)

    assert stats.invalid_nested_array_count == 0
    assert stats.non_serializable_count == 0
    assert 0 < stats.estimated_size_bytes < 1024 * 1024
    assert stats.max_depth <= 20


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        ({"value": float("nan")}, "non_finite_number"),
        ({"value": 2**63}, "integer_out_of_range"),
        ({1: "not-a-string-key"}, "invalid_key"),
        ({"value": object()}, "unsupported_type"),
    ],
)
def test_rejects_non_firestore_values_without_echoing_them(document, reason):
    from data_pipeline.durable_document import (
        DurableDocumentValidationError,
        validate_durable_document,
    )

    with pytest.raises(DurableDocumentValidationError) as exc_info:
        validate_durable_document(document)

    assert reason in exc_info.value.reason_codes
    assert exc_info.value.stats.non_serializable_count >= 1


def test_rejects_cycles_and_excessive_depth():
    from data_pipeline.durable_document import (
        DurableDocumentValidationError,
        validate_durable_document,
    )

    cyclic = {}
    cyclic["self"] = cyclic
    with pytest.raises(DurableDocumentValidationError) as cycle_error:
        validate_durable_document(cyclic)
    assert cycle_error.value.reason_codes == ("cycle",)

    too_deep = {}
    cursor = too_deep
    for _ in range(21):
        cursor["next"] = {}
        cursor = cursor["next"]
    with pytest.raises(DurableDocumentValidationError) as depth_error:
        validate_durable_document(too_deep)
    assert "too_deep" in depth_error.value.reason_codes


def test_extreme_depth_returns_sanitized_error_instead_of_recursion_error():
    from data_pipeline.durable_document import (
        DurableDocumentValidationError,
        validate_durable_document,
    )

    too_deep = {}
    cursor = too_deep
    for _ in range(1_500):
        cursor["next"] = {}
        cursor = cursor["next"]

    with pytest.raises(DurableDocumentValidationError) as exc_info:
        validate_durable_document(too_deep)

    assert exc_info.value.reason_codes == ("too_deep",)


def test_rejects_document_above_conservative_size_limit():
    from data_pipeline.durable_document import (
        DurableDocumentValidationError,
        validate_durable_document,
    )

    with pytest.raises(DurableDocumentValidationError) as exc_info:
        validate_durable_document({"blob": "x" * 128}, max_size_bytes=64)

    assert exc_info.value.reason_codes == ("too_large",)


def test_rejects_bytearray_that_firestore_sdk_cannot_encode():
    from data_pipeline.durable_document import (
        DurableDocumentValidationError,
        validate_durable_document,
    )

    with pytest.raises(DurableDocumentValidationError) as exc_info:
        validate_durable_document({"binary": bytearray(b"synthetic")})

    assert exc_info.value.reason_codes == ("unsupported_type",)


def test_size_estimate_is_conservative_against_firestore_protobuf():
    from data_pipeline.durable_document import validate_durable_document

    document = {f"f{index:05d}": "" for index in range(1_000)}
    stats = validate_durable_document(
        document,
        max_size_bytes=10 * 1024 * 1024,
    )
    encoded_size = Document(
        fields=_helpers.encode_dict(document)
    )._pb.ByteSize()

    assert stats.estimated_size_bytes >= encoded_size


def test_reference_names_are_counted_in_conservative_size_estimate():
    from data_pipeline.durable_document import (
        DurableDocumentValidationError,
        validate_durable_document,
    )

    client = Client(
        project="synthetic-project", credentials=AnonymousCredentials()
    )
    segment = "x" * 1_200
    reference = client.document(
        f"c/{segment}/d/{segment}/e/{segment}/f/{segment}"
    )
    document = {f"ref{index}": reference for index in range(220)}
    encoded_size = Document(
        fields=_helpers.encode_dict(document)
    )._pb.ByteSize()
    assert encoded_size > 1024 * 1024

    with pytest.raises(DurableDocumentValidationError) as exc_info:
        validate_durable_document(document)

    assert "too_large" in exc_info.value.reason_codes


def test_wide_document_over_firestore_limit_is_rejected_prewrite():
    from data_pipeline.durable_document import (
        DurableDocumentValidationError,
        validate_durable_document,
    )

    document = {f"f{index:05d}": "" for index in range(70_000)}
    encoded_size = Document(
        fields=_helpers.encode_dict(document)
    )._pb.ByteSize()
    assert encoded_size > 1024 * 1024

    with pytest.raises(DurableDocumentValidationError) as exc_info:
        validate_durable_document(document)

    assert "too_large" in exc_info.value.reason_codes


@pytest.mark.parametrize("field_name", ["", "__reserved__", "\ud800"])
def test_rejects_invalid_firestore_field_names(field_name):
    from data_pipeline.durable_document import (
        DurableDocumentValidationError,
        validate_durable_document,
    )

    with pytest.raises(DurableDocumentValidationError) as exc_info:
        validate_durable_document({field_name: "synthetic"})

    assert "invalid_key" in exc_info.value.reason_codes


def test_rejects_invalid_utf8_string_without_raising_encoder_error():
    from data_pipeline.durable_document import (
        DurableDocumentValidationError,
        validate_durable_document,
    )

    with pytest.raises(DurableDocumentValidationError) as exc_info:
        validate_durable_document({"value": "\ud800"})

    assert "invalid_utf8" in exc_info.value.reason_codes


def test_rejects_field_path_over_firestore_limit():
    from data_pipeline.durable_document import (
        DurableDocumentValidationError,
        validate_durable_document,
    )

    document = {"a" * 800: {"b" * 800: "synthetic"}}
    with pytest.raises(DurableDocumentValidationError) as exc_info:
        validate_durable_document(document)

    assert "invalid_field_path" in exc_info.value.reason_codes


@pytest.mark.parametrize(
    "point",
    [
        GeoPoint(float("nan"), 0),
        GeoPoint(91, 0),
        GeoPoint(0, 181),
    ],
)
def test_rejects_invalid_geopoints(point):
    from data_pipeline.durable_document import (
        DurableDocumentValidationError,
        validate_durable_document,
    )

    with pytest.raises(DurableDocumentValidationError) as exc_info:
        validate_durable_document({"point": point})

    assert set(exc_info.value.reason_codes) & {
        "non_finite_number", "invalid_geopoint"
    }
