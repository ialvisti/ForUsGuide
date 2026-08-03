"""Fail-closed validation for documents persisted in Firestore Standard.

The validator deliberately reports only closed reason codes and aggregate
counts.  It never retains field paths or values, because durable documents may
contain participant data and exception reporters commonly serialize errors.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any


FIRESTORE_MAX_DEPTH = 20
# Firestore's hard document limit is 1 MiB.  Keep headroom for protobuf and
# document-name overhead that this deterministic estimator does not model.
DEFAULT_MAX_ESTIMATED_SIZE_BYTES = 900 * 1024
_MAX_FIELD_NAME_BYTES = 1_500
_MAX_FIELD_PATH_BYTES = 1_500
_MAX_REFERENCE_NAME_BYTES = 6 * 1024
_MIN_INT64 = -(2**63)
_MAX_INT64 = 2**63 - 1
# Conservative protobuf envelope allowances. Firestore encodes every map
# field and array element as nested length-delimited messages; counting only
# keys and scalar bodies materially underestimates wide documents.
_MAP_ENTRY_OVERHEAD_BYTES = 16
_ARRAY_ELEMENT_OVERHEAD_BYTES = 12
_REASON_ORDER = (
    "nested_array",
    "cycle",
    "invalid_key",
    "invalid_utf8",
    "invalid_field_path",
    "non_finite_number",
    "invalid_geopoint",
    "invalid_reference",
    "integer_out_of_range",
    "unsupported_type",
    "too_deep",
    "too_large",
)


@dataclass(frozen=True)
class DurableDocumentStats:
    estimated_size_bytes: int
    max_depth: int
    invalid_nested_array_count: int
    non_serializable_count: int


class DurableDocumentValidationError(ValueError):
    """A document cannot be written safely to the durable store."""

    def __init__(
        self,
        *,
        stats: DurableDocumentStats,
        reason_codes: tuple[str, ...],
    ) -> None:
        self.stats = stats
        self.reason_codes = reason_codes
        super().__init__(
            "durable document rejected; "
            f"reasons={','.join(reason_codes)}; "
            f"estimated_size_bytes={stats.estimated_size_bytes}; "
            f"max_depth={stats.max_depth}; "
            f"invalid_nested_arrays={stats.invalid_nested_array_count}; "
            f"non_serializable={stats.non_serializable_count}"
        )


def _is_firestore_reference(value: Any) -> bool:
    """Recognize Firestore scalar wrappers without importing the SDK eagerly."""
    cls = value.__class__
    module = getattr(cls, "__module__", "")
    name = getattr(cls, "__name__", "")
    return module.startswith("google.cloud.firestore") and name in {
        "DocumentReference",
        "AsyncDocumentReference",
        "GeoPoint",
    }


def _field_path_segment_size(value: str) -> int:
    """Encoded size of a Firestore field-path segment.

    Non-simple names need backtick quoting; backticks and backslashes are
    escaped inside that representation.  The caller has already verified
    that ``value`` is valid UTF-8.
    """
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        return len(value.encode("utf-8"))
    escaped = value.replace("\\", "\\\\").replace("`", "\\`")
    return len(escaped.encode("utf-8")) + 2


def validate_durable_document(
    document: Any,
    *,
    max_depth: int = FIRESTORE_MAX_DEPTH,
    max_size_bytes: int = DEFAULT_MAX_ESTIMATED_SIZE_BYTES,
) -> DurableDocumentStats:
    """Validate Firestore shape, scalar types, depth and conservative size.

    Firestore forbids an array value directly inside another array.  Maps may
    occur inside arrays and may themselves contain arrays, so the check tracks
    the immediate parent container rather than rejecting all nested sequences.
    """

    reasons: set[str] = set()
    active_containers: set[int] = set()
    estimated_size_bytes = 0
    observed_max_depth = 0
    invalid_nested_array_count = 0
    non_serializable_count = 0

    def visit(
        value: Any,
        *,
        depth: int,
        parent_is_array: bool,
        field_path_bytes: int,
    ) -> None:
        nonlocal estimated_size_bytes
        nonlocal observed_max_depth
        nonlocal invalid_nested_array_count
        nonlocal non_serializable_count

        observed_max_depth = max(observed_max_depth, depth)
        if depth > max_depth:
            reasons.add("too_deep")
            non_serializable_count += 1
            # Stop at the first invalid depth.  Continuing recursion through
            # an attacker-controlled acyclic map can exceed Python's stack
            # before this function returns its sanitized validation error.
            return

        if isinstance(value, dict):
            object_id = id(value)
            if object_id in active_containers:
                reasons.add("cycle")
                non_serializable_count += 1
                return
            active_containers.add(object_id)
            estimated_size_bytes += 32
            try:
                for key, item in value.items():
                    estimated_size_bytes += _MAP_ENTRY_OVERHEAD_BYTES
                    if not isinstance(key, str):
                        reasons.add("invalid_key")
                        non_serializable_count += 1
                        estimated_size_bytes += 8
                    else:
                        invalid_encoding = False
                        try:
                            key_size = len(key.encode("utf-8"))
                        except UnicodeEncodeError:
                            key_size = 0
                            invalid_encoding = True
                            reasons.add("invalid_key")
                            reasons.add("invalid_utf8")
                            non_serializable_count += 1
                        estimated_size_bytes += key_size + 1
                        reserved = re.fullmatch(r"__.*__", key) is not None
                        if (
                            key_size == 0
                            or key_size > _MAX_FIELD_NAME_BYTES
                            or reserved
                        ):
                            reasons.add("invalid_key")
                            if not invalid_encoding:
                                non_serializable_count += 1
                        if key_size:
                            segment_size = _field_path_segment_size(key)
                            child_path_size = (
                                segment_size
                                if field_path_bytes == 0
                                else field_path_bytes + 1 + segment_size
                            )
                            if child_path_size > _MAX_FIELD_PATH_BYTES:
                                reasons.add("invalid_field_path")
                                non_serializable_count += 1
                        else:
                            child_path_size = field_path_bytes
                    if not isinstance(key, str):
                        child_path_size = field_path_bytes
                    visit(
                        item,
                        depth=depth + 1,
                        parent_is_array=False,
                        field_path_bytes=child_path_size,
                    )
            finally:
                active_containers.remove(object_id)
            return

        if isinstance(value, list):
            if parent_is_array:
                reasons.add("nested_array")
                invalid_nested_array_count += 1
            object_id = id(value)
            if object_id in active_containers:
                reasons.add("cycle")
                non_serializable_count += 1
                return
            active_containers.add(object_id)
            estimated_size_bytes += 16
            try:
                for item in value:
                    estimated_size_bytes += _ARRAY_ELEMENT_OVERHEAD_BYTES
                    visit(
                        item,
                        depth=depth + 1,
                        parent_is_array=True,
                        field_path_bytes=field_path_bytes,
                    )
            finally:
                active_containers.remove(object_id)
            return

        if value is None:
            estimated_size_bytes += 1
        elif isinstance(value, bool):
            estimated_size_bytes += 1
        elif isinstance(value, int):
            estimated_size_bytes += 8
            if not _MIN_INT64 <= value <= _MAX_INT64:
                reasons.add("integer_out_of_range")
                non_serializable_count += 1
        elif isinstance(value, float):
            estimated_size_bytes += 8
            if not math.isfinite(value):
                reasons.add("non_finite_number")
                non_serializable_count += 1
        elif isinstance(value, str):
            try:
                estimated_size_bytes += len(value.encode("utf-8")) + 1
            except UnicodeEncodeError:
                reasons.add("invalid_utf8")
                non_serializable_count += 1
                estimated_size_bytes += 1
        elif isinstance(value, bytes):
            estimated_size_bytes += len(value) + 1
        elif isinstance(value, datetime):
            estimated_size_bytes += 8
        elif _is_firestore_reference(value):
            scalar_name = value.__class__.__name__
            if scalar_name in {"DocumentReference", "AsyncDocumentReference"}:
                reference_path = getattr(value, "_document_path", None)
                if not isinstance(reference_path, str):
                    reasons.add("invalid_reference")
                    non_serializable_count += 1
                    estimated_size_bytes += _MAX_REFERENCE_NAME_BYTES + 1
                else:
                    try:
                        reference_size = len(reference_path.encode("utf-8"))
                    except UnicodeEncodeError:
                        reference_size = _MAX_REFERENCE_NAME_BYTES + 1
                        reasons.add("invalid_utf8")
                    estimated_size_bytes += reference_size + 1
                    if reference_size > _MAX_REFERENCE_NAME_BYTES:
                        reasons.add("invalid_reference")
                        non_serializable_count += 1
            else:
                estimated_size_bytes += 32
            if scalar_name == "GeoPoint":
                latitude = getattr(value, "latitude", None)
                longitude = getattr(value, "longitude", None)
                if (
                    isinstance(latitude, bool)
                    or not isinstance(latitude, (int, float))
                    or isinstance(longitude, bool)
                    or not isinstance(longitude, (int, float))
                ):
                    reasons.add("invalid_geopoint")
                    non_serializable_count += 1
                elif not (
                    math.isfinite(float(latitude))
                    and math.isfinite(float(longitude))
                ):
                    reasons.add("non_finite_number")
                    non_serializable_count += 1
                elif not (
                    -90 <= float(latitude) <= 90
                    and -180 <= float(longitude) <= 180
                ):
                    reasons.add("invalid_geopoint")
                    non_serializable_count += 1
        else:
            reasons.add("unsupported_type")
            non_serializable_count += 1
            estimated_size_bytes += 8

    if not isinstance(document, dict):
        reasons.add("unsupported_type")
        non_serializable_count += 1
        estimated_size_bytes += 8
        observed_max_depth = 0
    else:
        # The root document is not a nested Firestore field, so nested
        # map/array depth starts at zero and child values start at one.
        visit(
            document,
            depth=0,
            parent_is_array=False,
            field_path_bytes=0,
        )

    if estimated_size_bytes > max_size_bytes:
        reasons.add("too_large")

    stats = DurableDocumentStats(
        estimated_size_bytes=estimated_size_bytes,
        max_depth=observed_max_depth,
        invalid_nested_array_count=invalid_nested_array_count,
        non_serializable_count=non_serializable_count,
    )
    if reasons:
        ordered = tuple(reason for reason in _REASON_ORDER if reason in reasons)
        raise DurableDocumentValidationError(stats=stats, reason_codes=ordered)
    return stats
