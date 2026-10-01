"""Structured logging for CloudPulse.

Two output formats are supported:

``text``
    The human readable ``timestamp | level | logger | message`` line used during
    local development. Extra ``key=value`` pairs are appended inline.

``json``
    One JSON object per line, which is what Loki, Elasticsearch and any log
    shipper expect. Every record carries a stable set of keys so a query never
    has to parse the message text.

A request id is attached to each record so the lines emitted while serving one
request can be correlated in the log aggregator. It is read from the inbound
``X-Request-ID`` header when present, which lets a gateway-provided id flow
through instead of being replaced.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from datetime import UTC, datetime
from typing import Any

RESERVED = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    }
)

# Attributes promoted to a dedicated JSON field instead of being nested under
# ``extra`` so dashboards and log queries can address them directly.
PROMOTED = ("request_id", "client_ip", "method", "path", "status", "duration_ms")

TEXT_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    fields: dict[str, Any] = {}

    for key, value in record.__dict__.items():
        if key in RESERVED or key in PROMOTED:
            continue
        if key.startswith("_"):
            continue
        fields[key] = value

    return fields


def _request_id() -> str | None:
    try:
        from flask import g, has_request_context

        if has_request_context():
            return getattr(g, "request_id", None)
    except Exception:  # pragma: no cover - defensive
        pass
    return None


class JSONFormatter(logging.Formatter):
    """Render a log record as a single line JSON object."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        """Return an ISO-8601 UTC timestamp.

        The inherited implementation is not broken, but it renders local time
        with a comma separator, which disagrees with the ISO-8601 UTC stamp
        that :meth:`format` puts in ``timestamp``. Overriding the method keeps
        the two paths consistent if this formatter is ever used through the
        normal ``format`` pipeline.
        """
        moment = datetime.fromtimestamp(record.created, tz=UTC)
        return moment.isoformat() if datefmt is None else moment.strftime(datefmt)

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        request_id = getattr(record, "request_id", None) or _request_id()
        if request_id:
            payload["request_id"] = request_id

        for key in PROMOTED:
            if key == "request_id":
                continue
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value

        fields = _extra_fields(record)
        if fields:
            payload["context"] = fields

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, default=str, sort_keys=True)


class TextFormatter(logging.Formatter):
    """Human readable formatter that still surfaces extra context."""

    def __init__(self) -> None:
        super().__init__(fmt=TEXT_FORMAT)

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)

        extras = []
        for key in PROMOTED:
            value = getattr(record, key, None)
            if value is not None:
                extras.append(f"{key}={value}")

        for key, value in _extra_fields(record).items():
            extras.append(f"{key}={value}")

        if extras:
            base = f"{base} | {' | '.join(extras)}"

        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"

        return base


class RequestIdFilter(logging.Filter):
    """Attach the active request id to every record it sees."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", None):
            record.request_id = _request_id()
        return True


def build_formatter(log_format: str) -> logging.Formatter:
    return JSONFormatter() if log_format == "json" else TextFormatter()


def new_request_id() -> str:
    return uuid.uuid4().hex


def configure(
    app,
    *,
    log_format: str | None = None,
    level: str | None = None,
    include_request_id: bool = True,
) -> logging.Handler:
    """Install a single stdout handler on the root logger.

    Container platforms collect stdout, so logs go there rather than to a file.
    The root logger's handlers are replaced so repeated app creation in the test
    suite does not stack duplicate handlers.
    """
    resolved_format = (
        log_format
        or app.config.get("LOG_FORMAT")
        or "text"
    ).lower()

    resolved_level = (
        level or app.config.get("LOG_LEVEL") or ("DEBUG" if app.config.get("DEBUG") else "INFO")
    ).upper()

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(build_formatter(resolved_format))

    if include_request_id and app.config.get("LOG_INCLUDE_REQUEST_ID", True):
        handler.addFilter(RequestIdFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)

    root.addHandler(handler)
    root.setLevel(getattr(logging, resolved_level, logging.INFO))

    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    return handler
