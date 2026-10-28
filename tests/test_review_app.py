"""Offline contract checks for the student's session-first HW4 interface."""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from analysis.helpers import _state
from analysis.helpers.langfuse_io import LangfuseNotConfigured
from analysis.review_app import server as review_server
from analysis.review_app.loader import graph, load_records, sessions
from analysis.review_app.server import Conflict, ReviewStore, make_server, parse_args


def trace(identifier="a" * 32, session="session-a", timestamp="2026-07-01T12:00:00Z"):
    return {
        "id": identifier, "timestamp": timestamp,
        "input": "Please check my refund", "output": "Queued for approval",
        "metadata": {"attributes": {
            "cartwheel.session_id": session, "cartwheel.scenario_id": "scenario-1",
            "cartwheel.user_role": "shopper",
        }},
        "observations": [
            {"id": "tool-1", "type": "TOOL", "name": "issue_refund",
             "startTime": timestamp, "input": {"amount": 150},
             "output": {"status": "queued_for_approval"}},
            {"id": "model-1", "type": "GENERATION", "output": None},
            {"id": "span-1", "type": "SPAN", "name": "cartwheel.session_message",
             "input": "Earlier context", "output": "Queued for approval"},
        ],
    }


def load(tmp_path, records):
    source = tmp_path / "traces.json"
    source.write_text(json.dumps({"traces": records}), encoding="utf-8")
    return load_records(source)


@pytest.fixture
def store(tmp_path):
    traces = load(tmp_path, [
        trace(), trace("b" * 32, timestamp="2026-07-01T12:01:00+00:00"),
        trace("c" * 32, session="session-b"),
    ])
    return ReviewStore(traces, tmp_path / "state", offline_reason="Offline test fixture",
                       source=str((tmp_path / "traces.json").resolve()))


def change(store, action, **payload):
    return store.mutate({"revision": store.revision(), "action": action, **payload})


def prepare_label(store):
    change(store, "select", trace_id="a" * 32, reason="human-selected test fixture")
    change(store, "patterns", patterns={"modes": [
        {"name": "unconfirmed_write", "status": "final", "definition": "Claims a write succeeded without confirmation"},
        {"name": "candidate_only", "status": "candidate"},
    ]})


def test_session_grouping_preserves_trace_ids_and_separates_scenario_replays(tmp_path):
    values = load(tmp_path, [
        trace("b", timestamp="2026-07-01T12:01:00Z"),
        trace("a"), trace("c", session="session-b"),
        trace("d", session=None), trace("e", session=None),
    ])
    groups = sessions(values)
    assert len(groups) == 4
    group = next(g for g in groups if g["id"] == "session:session-a")
    assert [t["trace_id"] for t in group["traces"]] == ["a", "b"]
    assert {t["trace_id"] for g in groups for t in g["traces"]} == {"a", "b", "c", "d", "e"}


def test_missing_time_is_flaggable_and_utc_order_is_correct(tmp_path):
    groups = sessions(load(tmp_path, [
        trace("missing", timestamp=None),
        trace("offset", timestamp="2026-07-01T13:00:00+02:00"),
        trace("utc", timestamp="2026-07-01T12:00:00Z"),
    ]))
    assert [t["trace_id"] for t in groups[0]["traces"]] == ["offset", "utc", "missing"]
    assert groups[0]["traces"][-1]["timestamp"] is None


def test_complete_observations_survive_null_generation_outputs(tmp_path):
    value = load(tmp_path, [trace()])[0]
    assert len(value["observations"]) == 3
    assert next(o for o in value["observations"] if o["id"] == "span-1")["input"] == "Earlier context"
    assert [m["role"] for m in value["trace"]] == ["user", "tool_call", "tool_result", "assistant"]
    assert value["trace"][2]["content"]["status"] == "queued_for_approval"


def test_duplicate_and_skeletal_exports_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="Duplicate"):
        load(tmp_path, [trace(), trace()])
    with pytest.raises(ValueError, match="no conversation evidence"):
        load(tmp_path, [{"trace_id": "store-0000", "segments": {"role": "merchant"}}])


def test_live_loading_paginates_and_keeps_siblings(tmp_path):
    client = Mock()
    batch = [SimpleNamespace(id=str(i)) for i in range(100)]
    client.api.trace.list.side_effect = [
        SimpleNamespace(data=batch), SimpleNamespace(data=[SimpleNamespace(id="100")]),
    ]
    client.api.trace.get.side_effect = lambda identifier: trace(identifier)
    values = load_records(None, client)
    assert len(values) == 101
    assert client.api.trace.list.call_args_list[1].kwargs["page"] == 2
    assert client.api.trace.list.call_args_list[1].kwargs["order_by"] == "timestamp.asc"
    assert client.api.trace.list.call_args_list[0].kwargs["to_timestamp"] == client.api.trace.list.call_args_list[1].kwargs["to_timestamp"]
    assert len(sessions(values)) == 1


def test_live_loading_deduplicates_summary_pages_and_skips_noncourse_traces():
    client = Mock()
    client.api.trace.list.side_effect = [
        SimpleNamespace(data=[SimpleNamespace(id=str(i)) for i in range(100)]),
        SimpleNamespace(data=[SimpleNamespace(id="99"), SimpleNamespace(id="other")]),
    ]
    client.api.trace.get.side_effect = lambda identifier: (
        {"id": identifier, "metadata": {}} if identifier == "other" else trace(identifier)
    )
    assert len(load_records(None, client)) == 100
    assert client.api.trace.get.call_count == 101


def test_local_export_never_reads_trace_api(tmp_path):
    load(tmp_path, [trace()])
    client = Mock()
    client.api.trace.list.side_effect = AssertionError("Must not fetch live traces")
    client.api.trace.get.side_effect = AssertionError("Must not fetch live traces")
    assert len(load_records(tmp_path / "traces.json", client)) == 1
    client.api.trace.list.assert_not_called()
    client.api.trace.get.assert_not_called()


@pytest.mark.parametrize("switches", [
    ["--traces", "saved.json", "--live-traces"],
    ["--offline", "saved.json", "--traces", "other.json", "--offline-reason", "outage"],
    ["--offline", "saved.json", "--live-traces", "--offline-reason", "outage"],
    ["--offline", "saved.json"],
    ["--offline-reason", "outage"],
])
def test_source_switches_and_offline_reason_are_unambiguous(switches):
    with pytest.raises(SystemExit) as error:
        parse_args(switches)
    assert error.value.code == 2


def test_default_export_is_anchored_to_repository_not_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    expected = Path(review_server.__file__).resolve().parents[2] / "traces" / "support_traces.json"
    assert parse_args([]).traces == expected


@pytest.mark.parametrize("mode", ["default", "export", "live", "offline"])
def test_startup_separates_trace_source_from_score_client(tmp_path, monkeypatch, mode):
    records = load(tmp_path, [trace()])
    export = tmp_path / "traces.json"
    flags = {
        "default": [], "export": ["--traces", str(export)], "live": ["--live-traces"],
        "offline": ["--offline", str(export), "--offline-reason", "temporary outage"],
    }[mode]
    client = Mock()
    client.api.trace.list.return_value = SimpleNamespace(data=[SimpleNamespace(id="a" * 32)])
    client.api.trace.get.return_value = trace()
    factory = Mock(return_value=client)
    listener = Mock(server_port=8021)
    make = Mock(return_value=listener)
    monkeypatch.setattr(review_server, "DEFAULT_EXPORT", export)
    monkeypatch.setattr(review_server.langfuse_io, "_client", factory)
    monkeypatch.setattr(review_server._state, "state_root", lambda: tmp_path / "state")
    monkeypatch.setattr(review_server, "make_server", make)
    monkeypatch.setattr("observability.instrument.load_env", lambda: None)
    monkeypatch.setattr(sys, "argv", ["review-app", *flags])
    review_server.main()
    opened = make.call_args.args[0]
    assert opened.client is (None if mode == "offline" else client)
    assert set(opened.traces) == {records[0]["trace_id"]}
    if mode == "offline":
        factory.assert_not_called()
    else:
        factory.assert_called_once()
        client.shutdown.assert_called_once()
    if mode == "live":
        client.api.trace.list.assert_called_once()
        assert opened.source == "langfuse"
    else:
        client.api.trace.list.assert_not_called()
        client.api.trace.get.assert_not_called()
        assert opened.source == str(export.resolve())
    assert not opened.root.exists()
    listener.serve_forever.assert_called_once()
    listener.server_close.assert_called_once()


def test_local_source_does_not_silently_disable_sync_without_credentials(tmp_path, monkeypatch):
    load(tmp_path, [trace()])
    monkeypatch.setattr(sys, "argv", ["review-app", "--traces", str(tmp_path / "traces.json")])
    monkeypatch.setattr("observability.instrument.load_env", lambda: None)
    factory = Mock(side_effect=LangfuseNotConfigured("Missing Langfuse credentials"))
    monkeypatch.setattr(review_server.langfuse_io, "_client", factory)
    with pytest.raises(LangfuseNotConfigured):
        review_server.main()


def test_missing_default_export_does_not_fall_back_to_live(tmp_path, monkeypatch):
    client = Mock()
    monkeypatch.setattr(review_server, "DEFAULT_EXPORT", tmp_path / "missing.json")
    monkeypatch.setattr(review_server.langfuse_io, "_client", Mock(return_value=client))
    monkeypatch.setattr("observability.instrument.load_env", lambda: None)
    monkeypatch.setattr(sys, "argv", ["review-app"])
    with pytest.raises(FileNotFoundError):
        review_server.main()
    client.api.trace.list.assert_not_called()
    client.api.trace.get.assert_not_called()
    client.shutdown.assert_called_once()


def test_source_sync_metadata_and_legacy_backup_keys_are_independent(store):
    old_offline_key = hashlib.sha256(f"{store.root.resolve()}:{store.source}".encode()).hexdigest()
    local = store.snapshot()
    assert local["trace_source"] == "local_export"
    assert local["sync_mode"] == "local_only"
    assert local["identity"] == old_offline_key
    assert local["legacy_identity"] is None
    store.client = Mock()
    local_synced = store.snapshot()
    old_live_key = hashlib.sha256(f"{store.root.resolve()}:langfuse".encode()).hexdigest()
    assert local_synced["trace_source"] == "local_export"
    assert local_synced["sync_mode"] == "langfuse"
    assert local_synced["identity"] == old_offline_key
    assert local_synced["legacy_identity"] == old_live_key
    live = ReviewStore(list(store.traces.values()), store.root, client=store.client).snapshot()
    assert live["identity"] == old_live_key
    assert live["trace_source"] == "live"
    assert live["trace_path"] is None


def test_new_picks_keep_export_provenance_without_rewriting_old_picks(store):
    prior_pick = {"trace_id": "b" * 32, "reason": "previous live selection"}
    store.write("sample_manifest.json", {"picks": [prior_pick], "source": "langfuse", "custom": 42})
    change(store, "select", trace_id="a" * 32, reason="from saved export")
    manifest = store.read("sample_manifest.json", {})
    assert manifest["picks"][0] == prior_pick
    assert manifest["source"] == "langfuse"
    assert manifest["custom"] == 42
    assert manifest["picks"][1]["source"] == store.source
    assert manifest["picks"][1]["trace_source"] == "local_export"
    assert manifest["trace_path"] == store.source


def test_read_only_use_does_not_write_existing_state(store):
    store.root.mkdir()
    (store.root / "annotations.json").write_text('{"annotations": [], "custom": 42}\n', encoding="utf-8")
    before = {p.name: p.read_bytes() for p in store.root.iterdir()}
    store.snapshot()
    store.snapshot()
    assert {p.name: p.read_bytes() for p in store.root.iterdir()} == before


def test_sampling_exclusion_is_reversible_and_preserves_existing_review_work(store):
    change(store, "select", trace_id="a" * 32, reason="random batch")
    change(store, "note", trace_id="a" * 32, note="no failure observed")
    before = store.snapshot()
    revision = store.revision()
    change(store, "exclude", trace_id="a" * 32, excluded=True)
    after = store.snapshot()
    assert after["excluded_trace_ids"] == ["a" * 32]
    assert after["manifest"] == before["manifest"]
    assert after["annotations"] == before["annotations"]
    assert after["progress"] == before["progress"]
    assert ReviewStore(list(store.traces.values()), store.root).snapshot()["excluded_trace_ids"] == ["a" * 32]
    with pytest.raises(Conflict):
        store.mutate({"revision": revision, "action": "exclude", "trace_id": "b" * 32, "excluded": True})
    change(store, "exclude", trace_id="a" * 32, excluded=False)
    assert store.snapshot()["excluded_trace_ids"] == []
    assert store.snapshot()["manifest"] == before["manifest"]
    assert len(store.read("sampling_exclusions.json", {})["exclusions"]) == 1


@pytest.mark.parametrize("excluded", [None, "true", 1])
def test_sampling_exclusion_requires_an_explicit_boolean(store, excluded):
    revision = store.revision()
    with pytest.raises(ValueError, match="exclude"):
        change(store, "exclude", trace_id="a" * 32, excluded=excluded)
    assert store.revision() == revision


@pytest.mark.parametrize("strategy", ["random", "diversity"])
def test_automatic_picks_respect_saved_exclusions_and_restoration(store, tmp_path, monkeypatch, strategy):
    from analysis.helpers import next_to_label, select_traces

    monkeypatch.setenv("CARTWHEEL_ANALYSIS_STATE", str(store.root))
    source = tmp_path / "candidates.json"
    source.write_text(json.dumps([
        {"id": identifier, "text": "Refund policy question", "features": {"code_vs_judge_conflict": True}}
        for identifier in store.traces
    ]), encoding="utf-8")
    change(store, "exclude", trace_id="a" * 32, excluded=True)
    assert {p["trace_id"] for p in select_traces(source, k=10, strategy=strategy)} == {"b" * 32, "c" * 32}
    assert [p["trace_id"] for p in select_traces(source, k=10, strategy=strategy, exclude_ids=["c" * 32])] == ["b" * 32]
    for candidate_strategy in ("random", "enrich", "uncertainty", "disagreement"):
        candidates = next_to_label("new_mode", k=10, strategy=candidate_strategy, trace_source=source)
        assert {p["trace_id"] for p in candidates} == {"b" * 32, "c" * 32}
    assert store.snapshot()["excluded_trace_ids"] == ["a" * 32]
    change(store, "exclude", trace_id="a" * 32, excluded=False)
    assert {p["trace_id"] for p in select_traces(source, k=10, strategy=strategy)} == set(store.traces)
    for identifier in store.traces:
        change(store, "exclude", trace_id=identifier, excluded=True)
    assert select_traces(source, k=10, strategy=strategy) == []
    assert next_to_label("new_mode", k=10, strategy="random", trace_source=source) == []


def test_notes_preserve_fields_and_reviewed_counts_are_per_selected_trace(store):
    store.write("annotations.json", {"annotations": [], "custom": 42})
    change(store, "select", trace_id="a" * 32, reason="uniform batch")
    change(store, "note", trace_id="a" * 32, anchor="message:2", note="tool says pending", quote="queued_for_approval")
    change(store, "note", trace_id="b" * 32, note="context only")
    snapshot = store.snapshot()
    assert snapshot["progress"]["selected"] == snapshot["progress"]["reviewed"] == 1
    assert store.read("annotations.json", {})["custom"] == 42
    note = snapshot["annotations"][0]
    change(store, "note", trace_id=note["trace_id"], id=note["id"], anchor=note["anchor"], note="revised observation")
    assert len(store.snapshot()["annotations"]) == 2
    change(store, "delete_note", trace_id=note["trace_id"], id=note["id"])
    assert store.snapshot()["progress"]["reviewed"] == 0


def test_selected_quote_offsets_survive_edit_and_reload(store):
    change(store, "note", trace_id="a" * 32, anchor="message:0",
           quote="check", start=7, note="about this specific occurrence")
    note = store.snapshot()["annotations"][0]
    assert note["start"] == 7
    change(store, "note", trace_id=note["trace_id"], id=note["id"], anchor=note["anchor"],
           quote=note["quote"], start=note["start"], note="updated")
    assert store.snapshot()["annotations"][0]["start"] == 7
    with pytest.raises(ValueError, match="offset"):
        change(store, "note", trace_id=note["trace_id"], start=-1, note="invalid")


def test_stale_state_is_rejected_without_overwriting(store):
    old = store.revision()
    change(store, "note", trace_id="a" * 32, note="existing")
    with pytest.raises(Conflict):
        store.mutate({"revision": old, "action": "note", "trace_id": "a" * 32, "note": "stale"})
    assert len(store.snapshot()["annotations"]) == 1


def test_suggestion_decisions_are_retained_and_acceptance_is_not_a_label(store):
    store.write("suggestions.json", [
        {"id": "one", "trace_id": "a" * 32, "note": "possible failure", "original": 99},
        {"id": "two", "trace_id": "b" * 32, "note": "possible failure"},
    ])
    change(store, "suggestion", id="one", decision="accepted")
    change(store, "suggestion", id="two", decision="rejected", note="close negative")
    state = store.snapshot()
    assert [s["status"] for s in state["suggestions"]] == ["accepted", "rejected"]
    assert state["suggestions"][0]["original"] == 99
    assert state["suggestions"][1]["decision_note"] == "close negative"
    assert state["annotations"][0]["source"] == "accepted_suggestion"
    assert state["labels"] == {}
    with pytest.raises(ValueError, match="already"):
        change(store, "suggestion", id="one", decision="accepted")


def test_unjudged_is_not_absent_and_corrections_append_history(store):
    prepare_label(store)
    assert len(store.snapshot()["progress"]["missing"]) == 1
    assert not store.snapshot()["labels"]
    result = change(store, "label", trace_id="a" * 32, mode="unconfirmed_write", label=0)
    assert result["sync_status"] == "offline"
    first = store.snapshot()["labels"]["unconfirmed_write:" + "a" * 32]
    assert first["label"] == 0
    assert store.snapshot()["progress"]["missing"] == []
    change(store, "label", trace_id="a" * 32, mode="unconfirmed_write", label=1)
    history = _state.read_jsonl(store.root / "labels" / "unconfirmed_write.jsonl")
    assert [r["label"] for r in history] == [0, 1]
    assert history[0]["label_id"] != history[1]["label_id"]
    assert history[0]["score_id"] == history[1]["score_id"]


@pytest.mark.parametrize("label", [None, True, False, "0", 2, -1])
def test_only_exact_binary_integers_are_accepted(store, label):
    prepare_label(store)
    with pytest.raises(ValueError, match="Unjudged"):
        change(store, "label", trace_id="a" * 32, mode="unconfirmed_write", label=label)
    assert not (store.root / "labels").exists()


def test_unselected_traces_candidate_modes_and_unsafe_paths_are_rejected(store):
    prepare_label(store)
    for trace_id, mode in [
        ("b" * 32, "unconfirmed_write"), ("a" * 32, "candidate_only"),
        ("a" * 32, "../escape"), ("not-a-trace", "unconfirmed_write"),
    ]:
        with pytest.raises(ValueError):
            change(store, "label", trace_id=trace_id, mode=mode, label=1)
    with pytest.raises(ValueError):
        change(store, "note", trace_id="a" * 32, anchor="message:999", note="bad anchor")


def test_sync_failure_is_durable_and_retry_confirms_exact_revision(store):
    prepare_label(store)
    client = Mock()
    store.client = client
    client.flush.side_effect = RuntimeError("network down")
    result = change(store, "label", trace_id="a" * 32, mode="unconfirmed_write", label=1, note="evidence")
    assert not result["ok"]
    row = store.snapshot()["labels"]["unconfirmed_write:" + "a" * 32]
    assert row["sync_status"] == "error"
    client.flush.side_effect = None
    score = SimpleNamespace(
        id=row["score_id"], trace_id=row["trace_id"], name="unconfirmed_write",
        value=1, comment=f"HW4 revision {row['label_id']}",
    )
    client.api.score_v_2.get.return_value = SimpleNamespace(data=[score])
    result = change(store, "retry", trace_id="a" * 32, mode="unconfirmed_write")
    assert result["sync_status"] == "confirmed"
    assert client.create_score.call_args.kwargs["score_id"] == row["score_id"]
    assert client.create_score.call_args.kwargs["trace_id"] == "a" * 32
    assert store.snapshot()["progress"]["unsynced"] == 0


def test_ingestion_lag_and_old_same_value_revision_are_not_confirmed(store):
    prepare_label(store)
    client = Mock()
    store.client = client
    client.api.score_v_2.get.return_value = SimpleNamespace(data=[])
    assert change(store, "label", trace_id="a" * 32, mode="unconfirmed_write", label=0)["sync_status"] == "pending_confirmation"
    row = store.snapshot()["labels"]["unconfirmed_write:" + "a" * 32]
    client.api.score_v_2.get.return_value = SimpleNamespace(data=[SimpleNamespace(
        id=row["score_id"], trace_id=row["trace_id"], name="unconfirmed_write",
        value=0, comment="HW4 revision old-revision",
    )])
    assert change(store, "retry", trace_id="a" * 32, mode="unconfirmed_write")["sync_status"] == "pending_confirmation"


def test_map_has_one_point_per_trace_even_when_features_are_identical(store):
    projection = graph(list(store.traces.values()))
    assert len(projection["nodes"]) == 3
    assert all(isinstance(n["x"], float) for n in projection["nodes"])


def test_http_serves_assets_saves_reloads_and_rejects_bad_requests(store):
    server = make_server(store, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        for route in ["/", "/app.js", "/style.css"]:
            with urlopen(base + route) as response:
                assert response.status == 200
                assert response.headers["Content-Type"].endswith("charset=utf-8")
        with urlopen(base + "/api/data") as response:
            initial = json.load(response)
        with urlopen(base + "/api/revision") as response:
            assert json.load(response) == {"revision": initial["revision"]}
        payload = {
            "revision": initial["revision"], "action": "note",
            "trace_id": "a" * 32, "note": "no failure observed",
        }
        request = Request(base + "/api/action", json.dumps(payload).encode(),
                          {"Content-Type": "application/json"})
        with urlopen(request) as response:
            assert json.load(response)["ok"]
        with urlopen(base + "/api/data") as response:
            assert json.load(response)["annotations"][0]["note"] == "no failure observed"
        with pytest.raises(HTTPError) as stale:
            urlopen(request)
        assert stale.value.code == 409
        with pytest.raises(HTTPError) as malformed:
            urlopen(Request(base + "/api/action", b"{", {"Content-Type": "application/json"}))
        assert malformed.value.code == 400
        with pytest.raises(HTTPError) as cross_origin:
            urlopen(Request(base + "/api/action", b"{}", {
                "Content-Type": "application/json", "Origin": "https://example.invalid",
            }))
        assert cross_origin.value.code == 403
        with pytest.raises(HTTPError) as traversal:
            urlopen(base + "/../server.py")
        assert traversal.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
