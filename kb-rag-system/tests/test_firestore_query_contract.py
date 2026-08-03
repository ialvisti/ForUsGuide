"""Firestore query construction must use the keyword filter API."""

from __future__ import annotations

from types import SimpleNamespace
from typing import AsyncIterator, cast

from google.cloud.firestore_v1.base_query import FieldFilter

from data_pipeline.ticket_job_repository import FirestoreTicketJobBackend


class _Aggregation:
    async def get(self) -> list[list[SimpleNamespace]]:
        return [[SimpleNamespace(value=0)]]


class _QueryProbe:
    def __init__(self) -> None:
        self.where_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def where(self, *args: object, **kwargs: object) -> _QueryProbe:
        self.where_calls.append((args, kwargs))
        return self

    def count(self) -> _Aggregation:
        return _Aggregation()

    def order_by(self, _field: object) -> _QueryProbe:
        return self

    def start_after(self, _cursor: object) -> _QueryProbe:
        return self

    def limit(self, _limit: object) -> _QueryProbe:
        return self

    async def stream(self) -> AsyncIterator[object]:
        if False:  # pragma: no cover - makes this an empty async generator
            yield None


class _ClientProbe:
    def __init__(self) -> None:
        self.queries: list[_QueryProbe] = []

    def collection(self, _name: object) -> _QueryProbe:
        query = _QueryProbe()
        self.queries.append(query)
        return query


async def test_firestore_queries_use_fieldfilter_keyword_without_positional_where(
) -> None:
    backend = object.__new__(FirestoreTicketJobBackend)
    client = _ClientProbe()
    # Build the thin SDK adapter without constructing credentials.  The cast
    # is isolated to the fake boundary; assertions below inspect real
    # FieldFilter objects created by production code.
    object.__setattr__(backend, "_client", cast(object, client))
    object.__setattr__(backend, "_prefix", "")

    await backend.count_jobs("ticket_jobs", "principal", ["queued"])
    await backend.scan_collection(
        "ticket_jobs", states=["queued", "running"]
    )
    await backend.active_job_stats("ticket_jobs", ["queued", "running"])

    calls = [call for query in client.queries for call in query.where_calls]
    assert len(calls) == 4
    filters: list[FieldFilter] = []
    for args, kwargs in calls:
        assert args == ()
        assert set(kwargs) == {"filter"}
        assert isinstance(kwargs["filter"], FieldFilter)
        filters.append(kwargs["filter"])

    assert [item.field_path for item in filters] == [
        "principal_id",
        "state",
        "state",
        "state",
    ]
    assert [item.op_string for item in filters] == ["==", "in", "in", "in"]
