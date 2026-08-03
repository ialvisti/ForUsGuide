"""Production logging must emit each canonical event exactly once."""

from __future__ import annotations

import io
import json
import logging


def test_staging_and_production_select_structured_logging():
    from api.main import _uses_structured_logging

    assert _uses_structured_logging("production") is True
    assert _uses_structured_logging("staging") is True
    assert _uses_structured_logging("development") is False


def test_production_logging_replaces_preexisting_stream_handlers():
    from api.main import _configure_logging

    isolated_root = logging.Logger("synthetic-production-root")
    isolated_root.addHandler(logging.StreamHandler())
    isolated_root.addHandler(logging.StreamHandler())

    def install_cloud_handler() -> None:
        isolated_root.addHandler(logging.NullHandler())

    _configure_logging(
        production=True,
        root_logger=isolated_root,
        cloud_setup=install_cloud_handler,
    )

    assert len(isolated_root.handlers) == 1
    assert isinstance(isolated_root.handlers[0], logging.NullHandler)


def test_production_logging_installs_exact_explicit_structured_handler():
    from api.main import _configure_logging

    isolated_root = logging.Logger("synthetic-explicit-structured-root")
    isolated_root.addHandler(logging.StreamHandler())
    structured = logging.NullHandler()

    _configure_logging(
        production=True,
        root_logger=isolated_root,
        handler_factory=lambda: structured,
    )

    assert isolated_root.handlers == [structured]


def test_canonical_metric_event_uses_json_payload_message_and_logger_label():
    from api.main import _configure_logging
    from google.cloud.logging_v2.handlers import StructuredLogHandler

    stream = io.StringIO()
    isolated_root = logging.Logger("synthetic-structured-root")
    _configure_logging(
        production=True,
        root_logger=isolated_root,
        handler_factory=lambda: StructuredLogHandler(stream=stream),
    )
    record = logging.LogRecord(
        "ticket_metrics",
        logging.INFO,
        __file__,
        1,
        'ticket_metric_event {"metric":"synthetic","value":1}',
        (),
        None,
    )
    isolated_root.handle(record)

    documents = [json.loads(line) for line in stream.getvalue().splitlines()]
    metric_entry = next(
        document for document in documents
        if str(document.get("message", "")).startswith("ticket_metric_event ")
    )
    assert metric_entry["logging.googleapis.com/labels"][
        "python_logger"
    ] == "ticket_metrics"
