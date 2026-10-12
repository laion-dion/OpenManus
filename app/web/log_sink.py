"""Loguru sink that mirrors every OpenManus log record into the Web UI event bus.

The agentic logs panel in the browser subscribes to the ``logs`` SSE channel and
renders these records terminal-style, so users can watch each tool the Python
interpreter triggers behind the scenes (including the bash scripts being run).
"""

import re

from app.web.event_bus import event_bus

# Records emitted by our own SSE pipeline must not loop back into the bus.
_EXCLUDED_MODULE = "app.web"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _serialize(record) -> dict:
    """Turn a loguru record into a compact JSON-safe dict for the SSE stream."""
    extra = dict(record.get("extra") or {})
    # Drop non-serialisable objects (coroutines, locks, ...) coming from bind().
    clean_extra = {k: v for k, v in extra.items() if isinstance(v, (str, int, float, bool))}

    return {
        "ts": record["time"].strftime("%H:%M:%S"),
        "level": record["level"].name,
        "module": f"{record['name']}:{record['function']}:{record['line']}",
        "message": _ANSI_RE.sub("", record["message"]),
        "extra": clean_extra,
    }


class WebEventSinkFilter:
    """Callable loguru filter: keep agent logs, drop our own server noise."""

    def __init__(self, channel: str):
        self.channel = channel

    def __call__(self, record) -> bool:
        module = record["name"] or ""
        if module.startswith(_EXCLUDED_MODULE) and self.channel == "logs":
            # Still allow explicit web-server warnings through.
            return record["level"].no >= 40
        return True


def install_log_sink(channel: str = "logs") -> None:
    """Attach the event-bus sink to the global loguru logger (idempotent)."""
    from app.logger import logger

    # Idempotency guard across reloads/restarts of the web server.
    if getattr(logger, "_openmanus_web_sinks", None) is None:
        logger._openmanus_web_sinks = set()
    if channel in logger._openmanus_web_sinks:
        return

    def _sink(message):
        # ``message`` carries the fully-formatted record via its .record attr.
        record = message.record
        event = _serialize(record)
        event["channel"] = channel
        event["kind"] = "log"
        event["_web_channel"] = channel
        event_bus.publish(channel, {k: v for k, v in event.items() if k != "_web_channel"})

    # Tag the sink so install_log_sink stays idempotent across reloads.
    _sink._web_channel = channel

    logger.add(
        _sink,
        level="DEBUG",
        filter=WebEventSinkFilter(channel),
        enqueue=False,  # we are already inside the asyncio loop; publish directly
    )
    logger._openmanus_web_sinks.add(channel)
