"""Exercise the CLI with the real SDK and an offline model."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import langfuse
import pytest
from agents import Agent, Runner, function_tool
from agents.tracing import get_trace_provider, set_trace_provider, trace
from agents.tracing.processors import BackendSpanExporter, BatchTraceProcessor
from agents.tracing.provider import DefaultTraceProvider
from opentelemetry.instrumentation.openai_agents import OpenAIAgentsInstrumentor
from opentelemetry.trace import NoOpTracerProvider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent import cli, hw1_cli
from agent.auth import AuthContext
from observability import instrument
from tests.eval.fake_model import FakeModel, text_message, tool_call


@pytest.fixture
def hosted_exports(monkeypatch):
    """Isolate SDK tracing and capture hosted exports without network calls."""
    monkeypatch.setattr(instrument, "load_env", lambda: None)
    monkeypatch.setattr(instrument, "_genai_instrumented", False)
    monkeypatch.setattr(instrument, "_openai_tracing_enabled", False)
    requests = []

    def reject_export(request):
        requests.append(request)
        return httpx.Response(403, text="simulated ZDR tracing rejection")

    exporter = BackendSpanExporter(api_key="offline-placeholder")
    exporter._client.close()
    exporter._client = httpx.Client(transport=httpx.MockTransport(reject_export))
    hosted_processor = BatchTraceProcessor(exporter)
    monkeypatch.setattr(instrument, "default_processor", lambda: hosted_processor)
    previous_provider = get_trace_provider()
    provider = DefaultTraceProvider()
    provider.set_disabled(False)
    provider.register_processor(hosted_processor)
    set_trace_provider(provider)
    try:
        yield hosted_processor, requests
    finally:
        instrumentor = OpenAIAgentsInstrumentor()
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()
        set_trace_provider(previous_provider)
        provider.shutdown()
        hosted_processor.shutdown()
        exporter._client.close()


@pytest.mark.parametrize("debug", [False, True])
@pytest.mark.parametrize("tracing", ["plain", "missing_public", "missing_secret", "configured", "openai"])
def test_cli_tool_results(debug, tracing, tmp_path, monkeypatch, capsys, caplog, hosted_exports) -> None:
    executed = []

    @function_tool
    def lookup(order_id: int) -> dict:
        executed.append(order_id)
        return {"eligible": order_id == 4127}

    model = FakeModel()
    model.set_next_output([
        tool_call("lookup", {"order_id": 4127}),
        tool_call("lookup", {"order_id": 3980}),
    ])
    model.set_next_output([text_message("Done.")])
    agent = Agent(name="offline-cli", model=model, tools=[lookup])
    ctx = AuthContext(user_id=1, role="shopper")
    messages = iter(["Check both orders", "quit"])
    argv = ["agent.cli"] + (["--debug"] if debug else [])
    if tracing == "openai":
        monkeypatch.setenv("OPENAI_API_KEY", "offline-placeholder")
        argv.append("--trace-openai")
    elif tracing != "plain":
        argv.append("--trace")
    monkeypatch.setattr("sys.argv", argv)
    monkeypatch.setattr("builtins.input", lambda _: next(messages))
    monkeypatch.setattr(cli, "build_agent", lambda *args, **kwargs: agent)
    monkeypatch.setattr(cli, "resolve_auth", lambda *args: ctx)
    monkeypatch.setattr(cli, "load_env", lambda: None)
    monkeypatch.setattr(cli, "SESSIONS_DB", tmp_path / "sessions.db")

    # Use the real setup and processor replacement with a local OTel sink.
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "offline-public")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "offline-secret")
    if tracing == "missing_public":
        monkeypatch.delenv("LANGFUSE_PUBLIC_KEY")
    elif tracing == "missing_secret":
        monkeypatch.delenv("LANGFUSE_SECRET_KEY")
    otel_provider = TracerProvider()
    otel_exporter = InMemorySpanExporter()
    otel_provider.add_span_processor(SimpleSpanProcessor(otel_exporter))
    setup_calls = []
    monkeypatch.setattr(langfuse, "get_client", lambda: setup_calls.append(True))
    selected_provider = NoOpTracerProvider() if tracing == "missing_secret" else otel_provider
    monkeypatch.setattr(instrument.trace, "get_tracer_provider", lambda: selected_provider)

    hosted_processor, requests = hosted_exports
    try:
        cli.main()
        assert sorted(executed) == [3980, 4127]
        assert len(model.requests) == 2
        output = capsys.readouterr().out
        if debug:
            assert (
                "  [tool] lookup({'order_id': 4127})\n"
                "    -> {'eligible': True}\n"
                "  [tool] lookup({'order_id': 3980})\n"
                "    -> {'eligible': False}\n"
            ) in output
        else:
            assert "[tool]" not in output
        assert "agent> Done." in output
        hosted_processor.force_flush()
        assert len(requests) == int(tracing == "openai")
        spans = otel_exporter.get_finished_spans()
        assert setup_calls == ([True] if tracing in {"configured", "missing_secret"} else [])
        if tracing == "configured":
            assert sum(span.name == "lookup.tool" for span in spans) == 2
        else:
            assert spans == ()
        if tracing == "missing_public":
            assert "tracing is off" in caplog.text
        # Disabling tracing for CLI runs must not disable unrelated SDK traces.
        with trace("unrelated") as unrelated:
            assert unrelated.export() is not None
        hosted_processor.force_flush()
        expected = 0 if tracing in {"configured", "missing_secret"} else 1
        assert len(requests) == expected + int(tracing == "openai")
    finally:
        otel_provider.shutdown()


def test_trace_destinations_are_mutually_exclusive(monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["agent.cli", "--trace", "--trace-openai"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


def test_server_missing_secret_still_replaces_hosted_exporter(monkeypatch, hosted_exports) -> None:
    from server import app as server

    monkeypatch.setattr(server, "load_env", lambda: None)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "offline-public-server")
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    # Use real Langfuse initialization: absent secret creates a no-op client.
    hosted_processor, requests = hosted_exports

    async def run() -> None:
        async with server.lifespan(server.app):
            with trace("student-completed-server-run"):
                pass

    asyncio.run(run())
    hosted_processor.force_flush()
    assert requests == []


def test_instrumentation_failure_does_not_report_success(monkeypatch) -> None:
    monkeypatch.setattr(instrument, "_genai_instrumented", False)
    monkeypatch.setattr(
        OpenAIAgentsInstrumentor, "_check_dependency_conflicts",
        lambda self: "simulated dependency conflict",
    )
    with pytest.raises(RuntimeError, match="failed to install"):
        instrument.instrument_genai(NoOpTracerProvider())
    assert instrument._genai_instrumented is False


@pytest.mark.parametrize("has_key", [False, True])
@pytest.mark.parametrize("model_name", ["ollama_chat/local-model", "claude-opus-4-6"])
def test_non_openai_direct_run_does_not_export(
    has_key, model_name, monkeypatch, hosted_exports, caplog,
) -> None:
    from agent import agent as support
    from agents.extensions.models.litellm_model import LitellmModel

    if has_key:
        monkeypatch.setenv("OPENAI_API_KEY", "offline-placeholder")
    else:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Exercise real provider selection and agent construction, replacing only
    # the remote model call with an offline implementation.
    agent = support.build_agent(AuthContext(user_id=1, role="shopper"), model=model_name)
    assert isinstance(agent.model, LitellmModel)
    model = FakeModel()
    model.set_next_output([text_message("Local response")])
    agent.model = model
    result = asyncio.run(Runner.run(agent, "Hello"))
    assert result.final_output == "Local response"
    processor, requests = hosted_exports
    processor.force_flush()
    assert requests == []
    assert "skipping trace export" not in caplog.text


def test_non_openai_direct_run_preserves_langfuse(monkeypatch, hosted_exports) -> None:
    from agent import agent as support

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        # First disable implicit export, then explicitly enable local OTel.
        support.build_agent(AuthContext(user_id=1, role="shopper"), model="ollama_chat/local")
        instrument.instrument_genai(provider)
        agent = support.build_agent(AuthContext(user_id=1, role="shopper"), model="ollama_chat/local")
        model = FakeModel()
        model.set_next_output([text_message("Local response")])
        agent.model = model
        result = asyncio.run(Runner.run(agent, "Hello"))
        assert result.final_output == "Local response"
        assert exporter.get_finished_spans()
        processor, requests = hosted_exports
        processor.force_flush()
        assert requests == []
    finally:
        provider.shutdown()


def test_non_openai_explicit_hosted_export(monkeypatch, hosted_exports) -> None:
    from agent import agent as support

    # An explicit choice can restore hosted export after a local-only run.
    ctx = AuthContext(user_id=1, role="shopper")
    support.build_agent(ctx, model="ollama_chat/local")
    monkeypatch.setenv("OPENAI_API_KEY", "offline-placeholder")
    instrument.setup_openai_tracing()
    agent = support.build_agent(ctx, model="ollama_chat/local")
    model = FakeModel()
    model.set_next_output([text_message("Local response")])
    agent.model = model
    assert asyncio.run(Runner.run(agent, "Hello")).final_output == "Local response"
    processor, requests = hosted_exports
    processor.force_flush()
    assert len(requests) == 1


def test_explicit_hosted_export_requires_key(monkeypatch, hosted_exports, capsys) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(cli, "load_env", lambda: None)
    monkeypatch.setattr("sys.argv", ["agent.cli", "--trace-openai"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert "--trace-openai requires OPENAI_API_KEY" in capsys.readouterr().err
    processor, requests = hosted_exports
    processor.force_flush()
    assert requests == []


def test_prompt_version_tracks_template_not_identity(
    tmp_path, monkeypatch, capsys, hosted_exports,
) -> None:
    from agent import agent as support

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(cli, "_tracer", provider.get_tracer(__name__))
    monkeypatch.setattr(cli, "SESSIONS_DB", tmp_path / "sessions.db")
    contexts = [
        AuthContext(user_id=1, role="shopper"),
        AuthContext(user_id=2, role="shopper"),
        AuthContext(user_id=9002, role="merchant", store_id=2),
        AuthContext(user_id=9002, role="merchant", store_id=3),
        AuthContext(user_id=9501, role="support"),
    ]
    original = support.prompt_version()
    edited = support.SYSTEM_PROMPT_TEMPLATE + "\nKeep answers brief."
    assert support.prompt_version(edited) != original
    try:
        for index, ctx in enumerate([*contexts, contexts[0]]):
            if index == len(contexts):
                monkeypatch.setattr(support, "SYSTEM_PROMPT_TEMPLATE", edited)
            model = FakeModel()
            model.set_next_output([text_message("Hello.")])
            monkeypatch.setattr(support, "resolve_model", lambda _: model)
            messages = iter(["Hello", "quit"])
            monkeypatch.setattr("builtins.input", lambda _: next(messages))
            asyncio.run(cli.chat(ctx, model=None))
            expected = original if index < len(contexts) else support.prompt_version(edited)
            assert f"prompt_version={expected}" in capsys.readouterr().out
            span = exporter.get_finished_spans()[-1]
            assert span.attributes["cartwheel.prompt_version"] == expected
            assert span.attributes["cartwheel.user_id"] == str(ctx.user_id)
            assert model.requests[0]["system_instructions"] == support.render_system_prompt(ctx)
    finally:
        provider.shutdown()


@pytest.mark.parametrize(
    "role,user_id,store_id",
    [("shopper", 1, None), ("merchant", 9002, 2), ("support", 9501, None)],
)
def test_hw1_wrapper_appends_complete_drafts(
    role, user_id, store_id, world, tmp_path, monkeypatch, capsys, hosted_exports,
) -> None:
    @function_tool
    def lookup(order_id: int) -> dict:
        return {"ok": order_id == 4127, "order_id": order_id}

    model = FakeModel()
    model.set_next_output([
        tool_call("lookup", {"order_id": 4127}),
        tool_call("lookup", {"order_id": 3980}),
    ])
    model.set_next_output([text_message("First answer.\nTwo orders checked.")])
    model.set_next_output([text_message("Second answer.")])
    agent = Agent(name="offline-recorder", model=model, tools=[lookup])
    monkeypatch.setattr(cli, "build_agent", lambda *args, **kwargs: agent)
    monkeypatch.setattr(cli, "load_env", lambda: None)
    monkeypatch.chdir(tmp_path)
    output_path = tmp_path / "hw1-session.jsonl"
    existing = '{"expected":"preserve this assessment","met_requirement":true}'
    output_path.write_text(existing, encoding="utf-8")
    messages = iter(["First request", "Second request", "quit"])
    expected_line_counts = iter([1, 2, 3])

    def next_message(_):
        # The previous record must be on disk before accepting another request.
        line_count = len(output_path.read_text(encoding="utf-8").splitlines())
        assert line_count == next(expected_line_counts)
        return next(messages)

    monkeypatch.setattr("builtins.input", next_message)
    monkeypatch.setattr("sys.argv", ["agent.hw1_cli", "--role", role, "--user", str(user_id)])
    hw1_cli.main()

    text = output_path.read_text(encoding="utf-8")
    assert text.startswith(existing)
    lines = text.splitlines()
    assert len(lines) == 3
    records = [json.loads(line) for line in lines[1:]]
    assert records[0] == {
        "role": role,
        "user_id": user_id,
        "store_id": store_id,
        "request": "First request",
        "tool_calls": [
            {"name": "lookup", "arguments": {"order_id": 4127},
             "result": {"ok": True, "order_id": 4127}},
            {"name": "lookup", "arguments": {"order_id": 3980},
             "result": {"ok": False, "order_id": 3980}},
        ],
        "response": "First answer.\nTwo orders checked.",
        "expected": "TODO",
        "requirement": "TODO",
        "met_requirement": "TODO",
        "problem_source": "TODO",
    }
    assert records[1] == {
        **records[0],
        "request": "Second request",
        "tool_calls": [],
        "response": "Second answer.",
    }
    assert "First request" not in json.dumps(model.requests[2]["input"])
    assert not (tmp_path / ".sessions.db").exists()
    assert "Saved conversation" in capsys.readouterr().out
    processor, requests = hosted_exports
    processor.force_flush()
    assert requests == []


def test_hw1_wrapper_custom_output(tmp_path, monkeypatch, hosted_exports):
    output_path = tmp_path / "custom.jsonl"
    model = FakeModel()
    model.set_next_output([text_message("Observed response.")])
    agent = Agent(name="custom-recorder", model=model)
    monkeypatch.setattr(cli, "build_agent", lambda *args, **kwargs: agent)
    monkeypatch.setattr(cli, "resolve_auth", lambda *args: AuthContext(user_id=1, role="shopper"))
    monkeypatch.setattr(cli, "load_env", lambda: None)
    monkeypatch.setattr("sys.argv", ["agent.hw1_cli", "--output", str(output_path), "--debug"])
    messages = iter(["Custom request", "quit"])
    monkeypatch.setattr("builtins.input", lambda _: next(messages))
    hw1_cli.main()
    assert json.loads(output_path.read_text(encoding="utf-8"))["request"] == "Custom request"


def test_hw1_recording_failure_does_not_append(tmp_path, monkeypatch, hosted_exports):
    output_path = tmp_path / "drafts.jsonl"
    model = FakeModel()  # No response scripted: the real Runner must fail.
    agent = Agent(name="failing-recorder", model=model)
    monkeypatch.setattr(cli, "build_agent", lambda *args, **kwargs: agent)
    monkeypatch.setattr("builtins.input", lambda _: "Unfinished request")
    with output_path.open("a", encoding="utf-8") as output:
        with pytest.raises(AssertionError, match="ran out of scripted outputs"):
            asyncio.run(cli.chat(AuthContext(user_id=1, role="shopper"), None, record_file=output))
    assert output_path.read_text(encoding="utf-8") == ""


def test_hw1_recording_interruption_does_not_append(tmp_path, monkeypatch):
    async def paused_run(*args, **kwargs):
        return SimpleNamespace(interruptions=[object()])

    monkeypatch.setattr(cli.Runner, "run", paused_run)
    monkeypatch.setattr(cli, "build_agent", lambda *args, **kwargs: object())
    monkeypatch.setattr("builtins.input", lambda _: "Request requiring approval")
    output_path = tmp_path / "drafts.jsonl"
    with output_path.open("a", encoding="utf-8") as output:
        with pytest.raises(NotImplementedError, match="pending tool call"):
            asyncio.run(cli.chat(AuthContext(user_id=1, role="shopper"), None, record_file=output))
    assert output_path.read_text(encoding="utf-8") == ""


def test_hw1_output_failure_precedes_agent_execution(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "resolve_auth", lambda *args: AuthContext(user_id=1, role="shopper"))
    monkeypatch.setattr(cli, "load_env", lambda: None)
    monkeypatch.setattr("sys.argv", ["agent.hw1_cli", "--output", str(tmp_path)])
    monkeypatch.setattr(cli, "build_agent", lambda *args, **kwargs: pytest.fail("must not run"))
    with pytest.raises(SystemExit) as exc:
        hw1_cli.main()
    assert exc.value.code == 2
    assert "cannot open recording output" in capsys.readouterr().err


def test_hw1_tool_collection_pairs_outputs_and_preserves_observed_text():
    calls = [
        SimpleNamespace(type="tool_call_item", raw_item={"arguments": '{"order_id":4127}'},
                        tool_name="get_order", call_id="first"),
        SimpleNamespace(type="tool_call_item", raw_item={"arguments": "{}"},
                        tool_name="list_my_orders", call_id="second"),
        SimpleNamespace(type="tool_call_output_item", call_id="second", output="Observed text"),
        SimpleNamespace(type="tool_call_output_item", call_id="first", output='{"ok":true}'),
    ]
    assert cli._collect_tool_calls(calls) == [
        {"name": "get_order", "arguments": {"order_id": 4127}, "result": {"ok": True}},
        {"name": "list_my_orders", "arguments": {}, "result": "Observed text"},
    ]
    with pytest.raises(ValueError, match="missing result"):
        cli._collect_tool_calls(calls[:2])
