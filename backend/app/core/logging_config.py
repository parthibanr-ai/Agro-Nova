"""Logging with a request id on every line, and JSON output for log platforms.

Every request gets an id (the caller's `X-Request-ID` if it sent one, else a fresh one) that is echoed in the
response header and stamped on every log line written while handling it, including lines from worker threads
spawned by the request. Searching a failure report's id in the logs then shows the whole story of that request.

LOG_FORMAT=json writes one JSON object per line with a `severity` field, which Cloud Logging, Datadog and most
other platforms understand without extra parsing. The default is readable text for local development.
"""

import json
import logging
from contextvars import ContextVar
from datetime import datetime, timezone

from app.core.config import get_settings

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", request_id_var.get()),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def configure_logging() -> None:
    """Install our handler once (safe to call again)."""
    s = get_settings()
    root = logging.getLogger()
    root.setLevel(s.log_level.upper())
    if any(getattr(h, "_agrin", False) for h in root.handlers):
        return
    handler = logging.StreamHandler()
    handler._agrin = True  # type: ignore[attr-defined]
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(
        JsonFormatter() if s.log_format == "json"
        else logging.Formatter("%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s")
    )
    root.addHandler(handler)
