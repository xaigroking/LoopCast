"""W3C context propagation and allowlisted application diagnostics.

Only opaque correlation identifiers cross into storage/execution. Baggage,
tracestate, raw headers, payloads and exception messages are never logged.
"""
from datetime import datetime, timezone
import json
import logging
import re
import secrets

from opentelemetry.trace import NonRecordingSpan, SpanContext, get_current_span, set_span_in_context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_propagator = TraceContextTextMapPropagator()
CONTEXT_FIELDS = ("trace_id", "span_id", "parent_span_id", "request_id", "traceparent")
_LOG_FIELDS = set(CONTEXT_FIELDS) - {"traceparent"} | {
    "run_id", "attempt_id", "intent_id", "publication_attempt_id", "attempt_number",
    "method", "route", "status", "result", "duration_ms", "error_code",
    "project_id", "revision_id", "asset_id", "account_id", "session_id", "batch_id", "target_id", "job_id",
}


def configure_logging():
    logger = logging.getLogger('loopcast')
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(message)s'))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def new_context(traceparent=None, request_id=None):
    # Bound untrusted input before handing it to the standard propagator. Remote
    # baggage/tracestate are deliberately not part of our diagnostic contract.
    carrier = {"traceparent": traceparent} if isinstance(traceparent, str) and len(traceparent) <= 256 else {}
    parent = get_current_span(_propagator.extract(carrier)).get_span_context()
    trace_id = parent.trace_id if parent.is_valid else int(secrets.token_hex(16), 16) or 1
    span_id = int(secrets.token_hex(8), 16) or 1
    context = SpanContext(trace_id, span_id, False, parent.trace_flags if parent.is_valid else None)
    output = {}
    _propagator.inject(output, context=set_span_in_context(NonRecordingSpan(context)))
    return {
        "trace_id": f"{trace_id:032x}", "span_id": f"{span_id:016x}",
        "parent_span_id": f"{parent.span_id:016x}" if parent.is_valid else None,
        "request_id": request_id, "traceparent": output["traceparent"],
    }


def child_context(parent=None):
    parent = parent or {}
    carrier = parent.get("traceparent")
    # Older persisted links carry only trace_id; retain their correlation when
    # resuming queued work after an upgrade, without inventing a request ID.
    legacy = not carrier and re.fullmatch(r"(?!0{32})[a-f0-9]{32}", parent.get("trace_id") or "")
    if legacy:
        carrier = f"00-{parent['trace_id']}-{secrets.token_hex(8)}-00"
    context = new_context(carrier, parent.get("request_id"))
    if legacy:
        context['parent_span_id'] = None  # v3 did not record the originating span
    return context


def log_event(component, event, context=None, **fields):
    """Callers supply fixed event names and safe fields, never arbitrary data."""
    data = {key: value for key, value in {**(context or {}), **fields}.items() if key in _LOG_FIELDS}
    # Defense in depth for correlation fields read back from a private database.
    for key in ("trace_id", "span_id", "parent_span_id", "request_id", "run_id", "attempt_id", "intent_id", "publication_attempt_id", "project_id", "revision_id", "asset_id", "account_id", "session_id", "batch_id", "target_id", "job_id"):
        if key in data and data[key] is not None and not re.fullmatch(r"[a-f0-9]{16,32}", str(data[key])):
            data[key] = None
    logger = logging.getLogger("loopcast." + component)
    logger.setLevel(logging.INFO)
    logger.info(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(), "level": "INFO",
        "component": component, "event": event, **data,
    }, separators=(",", ":")))
