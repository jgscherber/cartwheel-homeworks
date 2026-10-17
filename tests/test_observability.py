"""Offline tests for the application attributes on tool spans."""

from unittest.mock import Mock

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent.auth import AuthContext
from observability import instrument


@pytest.mark.parametrize(
    "ctx, result, denied, reason",
    [
        (AuthContext(1, "shopper"), {"ok": True}, False, None),
        (
            AuthContext(9001, "merchant", store_id=1),
            {"ok": False, "error": "permission_denied", "reason": "another store"},
            True,
            "another store",
        ),
        (AuthContext(9501, "support"), {"error": "not_found"}, False, None),
        (AuthContext(1, "shopper"), {}, False, None),
        (AuthContext(1, "shopper"), {"error": "permission_denied"}, True, ""),
    ],
)
def test_record_tool_result(ctx, result, denied, reason) -> None:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        with provider.get_tracer(__name__).start_as_current_span("tool") as span:
            span.set_attribute("gen_ai.operation.name", "execute_tool")
            instrument.record_tool_result(ctx, result)

        (recorded,) = exporter.get_finished_spans()
        expected = {
            "gen_ai.operation.name": "execute_tool",
            "cartwheel.user_role": ctx.role,
            "cartwheel.user_id": str(ctx.user_id),
            "cartwheel.permission_denied": denied,
        }
        if ctx.role == "merchant":
            expected["cartwheel.store_id"] = str(ctx.store_id)
        if reason is not None:
            expected["cartwheel.permission_denied.reason"] = reason
        assert dict(recorded.attributes) == expected
        assert recorded.attributes["cartwheel.permission_denied"] is denied
    finally:
        provider.shutdown()


def test_record_tool_result_without_recording_span(monkeypatch) -> None:
    span = Mock(spec=trace.Span)
    span.is_recording.return_value = False
    monkeypatch.setattr(instrument.trace, "get_current_span", lambda: span)

    instrument.record_tool_result(AuthContext(1, "shopper"), {"ok": True})

    span.set_attribute.assert_not_called()
