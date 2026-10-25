"""Offline contract checks for the student's session-first HW4 interface."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from analysis.helpers import _state
from analysis.review_app.loader import graph, load_records, sessions
from analysis.review_app.server import Conflict, ReviewStore, make_server


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
    return ReviewStore(traces, tmp_path / "state", offline_reason="Offline test fixture")


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


def test_read_only_use_does_not_write_existing_state(store):
    store.root.mkdir()
    (store.root / "annotations.json").write_text('{"annotations": [], "custom": 42}\n', encoding="utf-8")
    before = {p.name: p.read_bytes() for p in store.root.iterdir()}
    store.snapshot()
    store.snapshot()
    assert {p.name: p.read_bytes() for p in store.root.iterdir()} == before


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
