"""Offline tests for tool-span attributes and authenticated message requests."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from agents import Agent, SQLiteSession
from agents.tracing import get_trace_provider, set_trace_provider
from agents.tracing.provider import DefaultTraceProvider
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent.auth import AuthContext
from observability import instrument
from server import app as server_app
from tests.eval.fake_model import FakeModel, text_message


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


@pytest.fixture
def request_spans(monkeypatch):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(server_app, "_tracer", provider.get_tracer(__name__))
    try:
        yield exporter
    finally:
        provider.shutdown()


@pytest.mark.parametrize(
    "capture_content, scenario_id",
    [("true", "scenario-1"), ("false", ""), (None, None)],
)
def test_post_message_records_request(
    session_client, request_spans, monkeypatch, capture_content, scenario_id
):
    if capture_content is None:
        monkeypatch.delenv("TRACELOOP_TRACE_CONTENT", raising=False)
    else:
        monkeypatch.setenv("TRACELOOP_TRACE_CONTENT", capture_content)
    credentials = session_client.post(
        "/sessions", json={"user_id": 1, "role": "shopper"}
    ).json()
    session_id = credentials["session_id"]
    ctx, session = server_app._SESSIONS[session_id]
    build = Mock(return_value=object())
    monkeypatch.setattr(server_app, "build_agent", build)

    async def run(*args, **kwargs):
        assert trace.get_current_span().name == "cartwheel.session_message"
        return SimpleNamespace(final_output="Your orders.")

    runner = AsyncMock(side_effect=run)
    monkeypatch.setattr(server_app.Runner, "run", runner)
    response = session_client.post(
        f"/sessions/{session_id}/messages",
        headers={"Authorization": f"Bearer {credentials['token']}"},
        json={"message": "My orders?", "model": "gpt-4o-mini", "scenario_id": scenario_id},
    )

    assert response.status_code == 200
    version = server_app.prompt_version()
    assert response.json() == {
        "session_id": session_id, "reply": "Your orders.", "prompt_version": version,
    }
    build.assert_called_once_with(ctx, model="gpt-4o-mini")
    runner.assert_awaited_once_with(
        build.return_value, "My orders?", context=ctx, session=session,
        max_turns=server_app.MAX_TURNS,
    )
    (span,) = request_spans.get_finished_spans()
    expected = {
        "cartwheel.session_id": session_id,
        "cartwheel.user_role": "shopper",
        "cartwheel.user_id": "1",
        "cartwheel.prompt_version": version,
    }
    if scenario_id:
        expected["cartwheel.scenario_id"] = scenario_id
    if capture_content == "true":
        expected["gen_ai.input.messages"] = json.dumps([
            {"role": "user", "parts": [{"type": "text", "content": "My orders?"}]}
        ])
        expected["gen_ai.output.messages"] = json.dumps([
            {"role": "assistant", "parts": [{"type": "text", "content": "Your orders."}]}
        ])
    assert dict(span.attributes) == expected


@pytest.mark.parametrize("token_case, status", [
    ("missing", 401), ("invalid", 401), ("another_session", 403), ("unknown_session", 404),
])
def test_post_message_rejects_unauthorized_requests(
    session_client, request_spans, monkeypatch, token_case, status
):
    credentials = session_client.post(
        "/sessions", json={"user_id": 1, "role": "shopper"}
    ).json()
    session_id = credentials["session_id"]
    headers = {}
    if token_case == "invalid":
        headers["Authorization"] = "Bearer invalid"
    elif token_case == "another_session":
        other = session_client.post(
            "/sessions", json={"user_id": 1, "role": "shopper"}
        ).json()
        headers["Authorization"] = f"Bearer {other['token']}"
    elif token_case == "unknown_session":
        headers["Authorization"] = f"Bearer {credentials['token']}"
        server_app._SESSIONS.clear()
    build = Mock()
    runner = AsyncMock()
    monkeypatch.setattr(server_app, "build_agent", build)
    monkeypatch.setattr(server_app.Runner, "run", runner)

    response = session_client.post(
        f"/sessions/{session_id}/messages",
        headers=headers,
        json={"message": "My orders?"},
    )

    assert response.status_code == status
    build.assert_not_called()
    runner.assert_not_awaited()
    assert request_spans.get_finished_spans() == ()


def test_post_message_preserves_conversation_history(session_client, monkeypatch):
    model = FakeModel()
    model.set_next_output([text_message("First reply.")])
    model.set_next_output([text_message("Second reply.")])
    agent = Agent(name="offline-server", model=model)
    monkeypatch.setattr(server_app, "build_agent", lambda *args, **kwargs: agent)
    credentials = session_client.post(
        "/sessions", json={"user_id": 1, "role": "shopper"}
    ).json()
    session_id = credentials["session_id"]
    previous_provider = get_trace_provider()
    provider = DefaultTraceProvider()
    provider.set_disabled(True)
    set_trace_provider(provider)
    try:
        for message, reply in [("Hello.", "First reply."), ("Continue.", "Second reply.")]:
            response = session_client.post(
                f"/sessions/{session_id}/messages",
                headers={"Authorization": f"Bearer {credentials['token']}"},
                json={"message": message},
            )
            assert response.status_code == 200
            assert response.json()["reply"] == reply
        history = model.requests[1]["input"]
        assert [item["role"] for item in history] == ["user", "assistant", "user"]
        assert history[0]["content"] == "Hello."
        assert history[1]["content"][0]["text"] == "First reply."
        assert history[2]["content"] == "Continue."
    finally:
        set_trace_provider(previous_provider)
        provider.shutdown()
