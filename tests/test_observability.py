"""Offline tests for tool-span attributes and session identity validation."""

from unittest.mock import Mock

import pytest
from agents import SQLiteSession
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent.auth import AuthContext
from observability import instrument
from server import app as server_app


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


@pytest.fixture
def session_client(world, tmp_path, monkeypatch):
    monkeypatch.setattr(server_app, "_SESSIONS", {})
    monkeypatch.setattr(server_app, "SESSIONS_DB", tmp_path / "sessions.db")
    # Do not enter the client's lifespan: these tests need no tracing backend.
    client = TestClient(server_app.app)
    try:
        yield client
    finally:
        client.close()


@pytest.mark.parametrize(
    "user_id, role, status",
    [(1, "admin", 400), (999999, "shopper", 404), (1, "support", 403)],
)
def test_create_session_rejects_invalid_identity(session_client, user_id, role, status):
    response = session_client.post("/sessions", json={"user_id": user_id, "role": role})

    assert response.status_code == status
    assert response.json()["detail"]
    assert server_app._SESSIONS == {}


@pytest.mark.parametrize(
    "user_id, role, store_id",
    [(1, "shopper", None), (9002, "merchant", 2), (9501, "support", None)],
)
def test_create_session_stores_database_identity(
    session_client, monkeypatch, user_id, role, store_id
):
    monkeypatch.setattr(server_app.time, "time", lambda: 1234567890)
    response = session_client.post("/sessions", json={"user_id": user_id, "role": role})

    assert response.status_code == 200
    body = response.json()
    session_id = body["session_id"]
    ctx, session = server_app._SESSIONS[session_id]
    assert ctx == AuthContext(user_id=user_id, role=role, store_id=store_id)
    assert isinstance(session, SQLiteSession)
    assert server_app.verify_token(body["token"]) == {
        "session_id": session_id,
        "user_id": user_id,
        "role": role,
        "store_id": store_id,
        "issued_at": 1234567890,
    }
