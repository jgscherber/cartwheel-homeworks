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
from analysis.review_app import development
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


@pytest.fixture
def development_store(tmp_path, monkeypatch):
    records = load(tmp_path, [trace(letter * 32, session=f"session-{letter}") for letter in "abcd"])
    root = tmp_path / "state"
    monkeypatch.setenv("CARTWHEEL_ANALYSIS_STATE", str(root))
    value = ReviewStore(records, root, offline_reason="Development fixture")
    mode = "unsupported_operational_claim"
    _state.write_json(root / "patterns.json", {"modes": [{
        "name": mode, "definition": "Unsupported operational claim.", "boundary": "Grounded statements pass.",
    }]})
    inputs = [{"trace_id": t["trace_id"], "trace": t["trace"]} for t in records]
    _state.write_json(root / "hw5_trace_inputs.json", inputs)
    _state.write_jsonl(root / "hw5_labels" / f"{mode}.jsonl", [
        {"trace_id": letter * 32, "mode": mode, "label": int(letter in "ac"),
         "label_convention": "pass_positive", "definition": "Unsupported operational claim.",
         "boundary": "Grounded statements pass.", "evidence": [{"quote": f"Evidence {letter}", "note": "Human-reviewed note"}]}
        for letter in "abcd"
    ])
    metadata = {
        "mode": mode, "inputs_sha256": development.digest(root / "hw5_trace_inputs.json"),
        "labels_sha256": development.digest(root / "hw5_labels" / f"{mode}.jsonl"),
    }
    _state.write_json(root / "hw5_preparation.json", metadata)
    assignment = {"train": ["a" * 32], "dev": ["b" * 32, "c" * 32], "test": ["d" * 32]}
    _state.write_json(root / "splits.json", {mode: {
        **assignment, **metadata, "seed": 7, "fractions": [0.2, 0.4, 0.4],
        "assignment_sha256": development.fingerprint(assignment),
    }})
    prompt = "Judge the recorded evidence."
    prompt_hash = review_server.development._prompt_hash(prompt, "gpt-4o-mini")
    _state.write_json(root / "judges" / f"{mode}-v0.json", {
        "judge_id": f"{mode}-v0", "mode": mode, "model": "gpt-4o-mini", "version": 0,
        "prompt_text": prompt, "prompt_hash": prompt_hash, "status": "draft", "label_convention": "pass_positive",
        "predictions": {prompt_hash: {"b" * 32: 1, "c" * 32: 0}},
        "critiques": {prompt_hash: {"b" * 32: "Supported by the tool.", "c" * 32: "Claim not supported."}},
    })
    return value


def test_development_snapshot_matches_hw5_convention_and_never_exposes_test_verdicts(development_store):
    value = development_store
    mode = "unsupported_operational_claim"
    path = value.root / "judges" / f"{mode}-v0.json"
    doc = _state.read_json(path)
    doc["status"] = "frozen"
    doc["predictions"][doc["prompt_hash"]]["d" * 32] = 1
    doc["critiques"][doc["prompt_hash"]]["d" * 32] = "SECRET HELD-OUT CRITIQUE"
    _state.write_json(path, doc)
    state = value.snapshot()["development"]
    assert state["errors"] == []
    judge = state["judges"][0]
    assert [r["trace_id"] for r in judge["rows"]] == ["b" * 32, "c" * 32]
    assert [(r["human_label"], r["judge_label"]) for r in judge["rows"]] == [(0, 1), (1, 0)]
    assert all(r["needs_review"] for r in judge["rows"])
    assert judge["metrics"]["fp"] == judge["metrics"]["fn"] == 1
    assert judge["metrics"]["tp"] == judge["metrics"]["tn"] == 0
    assert not judge["label_corrections_allowed"]
    assert "SECRET HELD-OUT" not in json.dumps(state)
    assert "d" * 32 not in json.dumps(state)


def test_development_review_history_and_resets_do_not_edit_labels_or_prompt(development_store):
    value = development_store
    original = {path: path.read_bytes() for path in value.root.rglob("*") if path.is_file()}
    jid = "unsupported_operational_claim-v0"
    for decision in ("judge_wrong", "definition_unclear", "pending"):
        change(value, "development_review", judge_id=jid, trace_id="b" * 32, decision=decision, note="My evidence.")
    row = value.snapshot()["development"]["judges"][0]["rows"][0]
    assert [r["decision"] for r in row["review_history"]] == ["judge_wrong", "definition_unclear", "pending"]
    assert row["needs_review"]
    assert all(path.read_bytes() == content for path, content in original.items())


def test_development_label_correction_preserves_exports_and_recalculates_all_versions(development_store):
    from analysis.helpers import judge_alignment, tools

    value = development_store
    mode = "unsupported_operational_claim"
    old_path = value.root / "judges" / f"{mode}-v0.json"
    judge = _state.read_json(old_path)
    revised = {**judge, "judge_id": f"{mode}-v1", "version": 1}
    _state.write_json(value.root / "judges" / f"{mode}-v1.json", revised)
    original = {path: path.read_bytes() for path in value.root.rglob("*") if path.is_file()}
    result = change(value, "development_review", judge_id=f"{mode}-v0", trace_id="b" * 32,
                    decision="label_wrong", corrected_label=1, note="The tool supports the statement.")
    assert set(result["metrics_updated"]) == {f"{mode}-v0", f"{mode}-v1"}
    row = value.snapshot()["development"]["judges"][0]["rows"][0]
    assert row["human_label"] == 1 and row["original_label"] == 0
    assert row["disagreement"] is False
    for version in (0, 1):
        report = _state.read_json(value.root.parent / "report" / f"dev-{mode}-v{version}.json")
        assert (report["tp"], report["fn"], report["fp"], report["tn"]) == (1, 1, 0, 0)
        assert report["tpr"] == 0.5
        assert report["class_counts"] == {"Pass": 2, "Fail": 0}
    assert all(path.read_bytes() == content for path, content in original.items())
    assert next(row for row in tools._load_labels(mode) if row["trace_id"] == "b" * 32)["label"] == 0
    assert judge_alignment(f"{mode}-v0", "dev")["tp"] == 1
    reviewed = next(j for j in value.snapshot()["development"]["judges"] if j["judge_id"] == f"{mode}-v0")["rows"][0]
    assert reviewed["review_current"] and not reviewed["needs_review"]


@pytest.mark.parametrize("problem", ["test_case", "no_note", "boolean_label", "same_label", "frozen", "missing_critique"])
def test_development_invalid_decisions_make_no_writes(development_store, problem):
    value = development_store
    payload = {"judge_id": "unsupported_operational_claim-v0", "trace_id": "b" * 32,
               "decision": "label_wrong", "corrected_label": 1, "note": "My evidence."}
    if problem == "test_case":
        payload["trace_id"] = "d" * 32
    elif problem == "no_note":
        payload["note"] = " "
    elif problem == "boolean_label":
        payload["corrected_label"] = True
    elif problem == "same_label":
        payload["corrected_label"] = 0
    else:
        path = value.root / "judges" / "unsupported_operational_claim-v0.json"
        doc = _state.read_json(path)
        if problem == "frozen":
            doc["status"] = "frozen"
        else:
            doc["critiques"][doc["prompt_hash"]].pop("b" * 32)
        _state.write_json(path, doc)
    original = {path: path.read_bytes() for path in value.root.rglob("*") if path.is_file()}
    with pytest.raises(ValueError):
        change(value, "development_review", **payload)
    assert all(path.read_bytes() == content for path, content in original.items())
    assert not (value.root / "hw5_reviews.json").exists()


def test_development_partial_predictions_are_pending_and_reviews_become_stale(development_store):
    value = development_store
    change(value, "development_review", judge_id="unsupported_operational_claim-v0", trace_id="b" * 32,
           decision="judge_wrong", note="Missing support.")
    path = value.root / "judges" / "unsupported_operational_claim-v0.json"
    doc = _state.read_json(path)
    doc["critiques"][doc["prompt_hash"]]["b" * 32] = "A changed critique."
    doc["predictions"][doc["prompt_hash"]].pop("c" * 32)
    _state.write_json(path, doc)
    judge = value.snapshot()["development"]["judges"][0]
    assert judge["metrics"] is None
    assert not judge["rows"][0]["review_current"] and judge["rows"][0]["needs_review"]
    assert judge["rows"][1]["judge_label"] is None
    assert not judge["rows"][1]["needs_review"]


def test_development_revision_detects_new_predictions_and_preserves_review_drafts(development_store):
    value = development_store
    revision = value.revision()
    path = value.root / "judges" / "unsupported_operational_claim-v0.json"
    doc = _state.read_json(path)
    doc["critiques"][doc["prompt_hash"]]["b" * 32] += " Updated."
    _state.write_json(path, doc)
    assert value.revision() != revision
    with pytest.raises(Conflict):
        value.mutate({"revision": revision, "action": "development_review",
                      "judge_id": doc["judge_id"], "trace_id": "b" * 32, "decision": "judge_wrong", "note": "Draft"})


@pytest.fixture
def taxonomy_store(tmp_path):
    records = load(tmp_path, [trace(letter * 32, session=f"session-{letter}") for letter in "abcdef"])
    value = ReviewStore(records, tmp_path / "state", offline_reason="Taxonomy test")
    for letter in "abcdef":
        change(value, "note", trace_id=letter * 32, note=f"Human observation {letter}")
    annotations = {a["trace_id"]: a["id"] for a in value.snapshot()["annotations"]}
    mode = {
        "name": "test_mode", "status": "candidate", "review_status": "pending",
        "definition": "An unsupported operational claim is present.",
        "boundary": "A grounded statement is negative.", "requirement_source": "SPEC.md RESP-3",
        "requirement_status": "existing", "evaluator_type": "llm_judge",
        "originating_annotation_ids": [annotations["a" * 32]], "supporting_annotation_ids": [],
        "examples": [
            {"id": letter, "trace_id": letter * 32, "annotation_id": annotations[letter * 32],
             "kind": "positive" if letter in "abc" else "close_negative", "status": "pending"}
            for letter in "abcdef"
        ],
    }
    change(value, "patterns", patterns={"modes": [mode]})
    return value


def test_taxonomy_example_decisions_preserve_notes_and_do_not_create_labels(taxonomy_store):
    value = taxonomy_store
    before = value.snapshot()
    change(value, "taxonomy_example", mode="test_mode", id="a", decision="accepted")
    mode = value.snapshot()["patterns"]["modes"][0]
    assert mode["examples"][0]["status"] == "accepted"
    assert mode["supporting_annotation_ids"] == [mode["examples"][0]["annotation_id"]]
    assert mode["status"] == "candidate"
    change(value, "taxonomy_example", mode="test_mode", id="a", decision="rejected")
    mode = value.snapshot()["patterns"]["modes"][0]
    assert mode["supporting_annotation_ids"] == []
    assert [r["decision"] for r in mode["examples"][0]["review_history"]] == ["accepted", "rejected"]
    change(value, "taxonomy_example", mode="test_mode", id="a", decision="pending")
    after = value.snapshot()
    assert after["annotations"] == before["annotations"]
    assert after["manifest"] == before["manifest"]
    assert after["labels"] == {}
    assert after["progress"] == before["progress"]


def test_taxonomy_cannot_accept_excluded_or_deleted_evidence(taxonomy_store):
    value = taxonomy_store
    change(value, "exclude", trace_id="a" * 32, excluded=True)
    with pytest.raises(ValueError, match="Excluded"):
        change(value, "taxonomy_example", mode="test_mode", id="a", decision="accepted")
    annotation_id = value.snapshot()["patterns"]["modes"][0]["examples"][1]["annotation_id"]
    change(value, "delete_note", trace_id="b" * 32, id=annotation_id)
    with pytest.raises(ValueError, match="deleted"):
        change(value, "taxonomy_example", mode="test_mode", id="b", decision="accepted")


def test_taxonomy_finalization_requires_approval_and_confirmed_examples(taxonomy_store):
    value = taxonomy_store
    with pytest.raises(ValueError, match="Approve"):
        change(value, "taxonomy_mode", mode="test_mode", decision="final")
    change(value, "taxonomy_mode", mode="test_mode", decision="accepted")
    with pytest.raises(ValueError, match="three distinct positive"):
        change(value, "taxonomy_mode", mode="test_mode", decision="final")
    for identifier in "abc":
        change(value, "taxonomy_example", mode="test_mode", id=identifier, decision="accepted")
    with pytest.raises(ValueError, match="three close negatives"):
        change(value, "taxonomy_mode", mode="test_mode", decision="final")
    for identifier in "def":
        change(value, "taxonomy_example", mode="test_mode", id=identifier, decision="accepted")
    change(value, "taxonomy_mode", mode="test_mode", decision="final")
    assert value.snapshot()["patterns"]["modes"][0]["status"] == "final"
    assert value.snapshot()["labels"] == {}


def test_taxonomy_specification_gaps_cannot_be_finalized_through_json_editor(taxonomy_store):
    value = taxonomy_store
    for identifier in "abcdef":
        change(value, "taxonomy_example", mode="test_mode", id=identifier, decision="accepted")
    change(value, "taxonomy_mode", mode="test_mode", decision="accepted")
    patterns = value.snapshot()["patterns"]
    patterns["modes"][0].update(requirement_status="needs_revision", status="final")
    with pytest.raises(ValueError, match="specification"):
        change(value, "patterns", patterns=patterns)
    assert value.snapshot()["patterns"]["modes"][0]["status"] == "candidate"


def test_taxonomy_definition_changes_require_rechecking_earlier_examples(taxonomy_store):
    value = taxonomy_store
    for identifier in "abcdef":
        change(value, "taxonomy_example", mode="test_mode", id=identifier, decision="accepted")
    change(value, "taxonomy_mode", mode="test_mode", decision="accepted")
    patterns = value.snapshot()["patterns"]
    patterns["modes"][0]["definition"] += " Revised boundary."
    change(value, "patterns", patterns=patterns)
    with pytest.raises(ValueError, match="Approve"):
        change(value, "taxonomy_mode", mode="test_mode", decision="final")
    change(value, "taxonomy_mode", mode="test_mode", decision="accepted")
    with pytest.raises(ValueError, match="three distinct positive"):
        change(value, "taxonomy_mode", mode="test_mode", decision="final")


def test_taxonomy_revision_metadata_alone_does_not_satisfy_a_specification_gap(taxonomy_store):
    value = taxonomy_store
    for identifier in "abcdef":
        change(value, "taxonomy_example", mode="test_mode", id=identifier, decision="accepted")
    change(value, "taxonomy_mode", mode="test_mode", decision="accepted")
    patterns = value.snapshot()["patterns"]
    patterns["modes"][0].update(
        requirement_status="revised",
        requirement_revision_text="An invented requirement that is not recorded in SPEC.md.",
    )
    change(value, "patterns", patterns=patterns)
    with pytest.raises(ValueError, match="approved SPEC.md revision"):
        change(value, "taxonomy_mode", mode="test_mode", decision="final")


def test_taxonomy_counts_distinct_traces_not_duplicate_annotations(taxonomy_store):
    value = taxonomy_store
    patterns = value.snapshot()["patterns"]
    source = patterns["modes"][0]["examples"][0]
    for example in patterns["modes"][0]["examples"][:3]:
        example.update(trace_id=source["trace_id"], annotation_id=source["annotation_id"])
    change(value, "patterns", patterns=patterns)
    for identifier in "abcdef":
        change(value, "taxonomy_example", mode="test_mode", id=identifier, decision="accepted")
    change(value, "taxonomy_mode", mode="test_mode", decision="accepted")
    with pytest.raises(ValueError, match="three distinct positive"):
        change(value, "taxonomy_mode", mode="test_mode", decision="final")


def test_taxonomy_cannot_confirm_opposite_assignments_for_one_trace(taxonomy_store):
    value = taxonomy_store
    patterns = value.snapshot()["patterns"]
    positive = patterns["modes"][0]["examples"][0]
    patterns["modes"][0]["examples"][3].update(
        trace_id=positive["trace_id"], annotation_id=positive["annotation_id"],
    )
    change(value, "patterns", patterns=patterns)
    change(value, "taxonomy_example", mode="test_mode", id="a", decision="accepted")
    with pytest.raises(ValueError, match="opposite"):
        change(value, "taxonomy_example", mode="test_mode", id="d", decision="accepted")


def test_taxonomy_finalization_requires_real_human_origins(taxonomy_store):
    value = taxonomy_store
    for identifier in "abcdef":
        change(value, "taxonomy_example", mode="test_mode", id=identifier, decision="accepted")
    change(value, "taxonomy_mode", mode="test_mode", decision="accepted")
    patterns = value.snapshot()["patterns"]
    patterns["modes"][0]["originating_annotation_ids"] = ["invented-annotation"]
    change(value, "patterns", patterns=patterns)
    with pytest.raises(ValueError, match="originating human annotation"):
        change(value, "taxonomy_mode", mode="test_mode", decision="final")


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

@pytest.fixture
def claim_store(tmp_path):
    records = load(tmp_path, [trace(letter * 32, session=f"session-{letter}") for letter in "abcdef"])
    value = ReviewStore(records, tmp_path / "state", offline_reason="Focused search test")
    definition = "The assistant asserts an operational fact unsupported by recorded evidence."
    for letter in "abc":
        change(value, "note", trace_id=letter * 32, note="Human-confirmed originating example")
    annotations = value.snapshot()["annotations"]
    value.write("patterns.json", {"custom": "preserve", "modes": [{
        "name": "unsupported_operational_claim", "definition": definition, "status": "candidate",
        "review_status": "accepted", "reviewed_definition": definition,
        "supporting_annotation_ids": [a["id"] for a in annotations],
        "examples": [{"id": a["id"], "annotation_id": a["id"], "trace_id": a["trace_id"],
                      "kind": "positive", "status": "accepted", "reviewed_definition": definition}
                     for a in annotations],
    }]})
    return value


def claim_suggestion(value, letter, identifier=None):
    message_index = next(i for i, m in enumerate(value.traces[letter * 32]["trace"]) if m["role"] == "assistant" and m.get("text"))
    return {
        "id": identifier or letter, "trace_id": letter * 32, "anchor": f"message:{message_index}",
        "quote": value.traces[letter * 32]["trace"][message_index]["text"], "start": 0,
        "proposed_kind": "positive", "note": "AI proposal, not human truth",
        "taxonomy_assignment": True, "mode": "unsupported_operational_claim",
        "definition": value.snapshot()["patterns"]["modes"][0]["definition"],
        "context_trace_ids": [letter * 32],
    }


@pytest.mark.parametrize("decision,kind", [("accepted", "positive"), ("close_negative", "close_negative"), ("rejected", "negative")])
def test_bulk_search_decisions_link_human_evidence_and_preserve_other_state(claim_store, decision, kind):
    value = claim_store
    value.write("suggestions.json", {"custom": 99, "suggestions": [claim_suggestion(value, letter) for letter in "de"]})
    before = value.snapshot()
    result = change(value, "suggestion_batch", ids=["d", "e"], decision=decision, note="Human decision")
    after = value.snapshot()
    assert result == {"ok": True, "decided": 2}
    assert [s["status"] for s in after["suggestions"]] == [decision, decision]
    assert [s["reviewed_kind"] for s in after["suggestions"]] == [kind, kind]
    mode = after["patterns"]["modes"][0]
    assert [(e["kind"], e["status"]) for e in mode["examples"][3:]] == [(kind, "accepted")] * 2
    assert all(e["review_history"][0]["source"] == "human" for e in mode["examples"][3:])
    assert len(mode["supporting_annotation_ids"]) == (5 if kind == "positive" else 3)
    assert [(a["review_kind"], a["note"], a["start"]) for a in after["annotations"][3:]] == [(kind, "Human decision", 0)] * 2
    assert after["annotations"][:3] == before["annotations"]
    assert after["manifest"] == before["manifest"]
    assert after["labels"] == {}
    assert value.read("suggestions.json", {})["custom"] == 99
    assert after["patterns"]["custom"] == "preserve"


@pytest.mark.parametrize("invalid", ["excluded", "quote", "stale_definition"])
def test_bulk_search_validates_the_whole_batch_before_writing(claim_store, invalid):
    value = claim_store
    suggestions = [claim_suggestion(value, letter) for letter in "de"]
    if invalid == "excluded":
        change(value, "exclude", trace_id="e" * 32, excluded=True)
    elif invalid == "quote":
        suggestions[1]["quote"] = "Invented text"
    else:
        suggestions[1]["definition"] = "Old definition"
    value.write("suggestions.json", suggestions)
    before = value.snapshot()
    with pytest.raises(ValueError):
        change(value, "suggestion_batch", ids=["d", "e"], decision="rejected")
    assert value.snapshot() == before


def test_search_preserves_utf16_quote_offsets_and_separates_opposite_decisions(claim_store):
    value = claim_store
    suggestion = claim_suggestion(value, "d")
    message = value.traces["d" * 32]["trace"][int(suggestion["anchor"].split(":")[1])]
    message["text"] = "\U0001f600 Echo. Echo."
    suggestion.update(quote="Echo.", start=9, proposed_kind="close_negative")
    value.write("suggestions.json", [suggestion])
    change(value, "suggestion", id="d", decision="accepted")
    note = value.snapshot()["annotations"][-1]
    assert note["start"] == 9 and note["quote"] == "Echo."
    assert "this failure mode is present" in note["note"]
    suggestions = value.read("suggestions.json", [])
    other = {**suggestion, "id": "opposite"}
    suggestions.append(other)
    value.write("suggestions.json", suggestions)
    before = value.snapshot()
    with pytest.raises(ValueError, match="opposite"):
        change(value, "suggestion", id="opposite", decision="close_negative")
    assert value.snapshot() == before


def test_search_publication_filters_scope_deduplicates_and_retains_pending_provenance(claim_store):
    value = claim_store
    change(value, "exclude", trace_id="f" * 32, excluded=True)
    value.write("sample_manifest.json", {"custom": "keep", "picks": [{"trace_id": "a" * 32, "reason": "existing pick"}]})
    before = value.snapshot()
    result = {
        "mode": "unsupported_operational_claim", "definition": before["patterns"]["modes"][0]["definition"],
        "method": "local_agent_evidence_review", "eligible_trace_ids": [letter * 32 for letter in "adef"],
        "candidates": [claim_suggestion(value, letter) for letter in "adef"],
    }
    result["candidates"][2]["proposed_kind"] = "close_negative"
    report = change(value, "publish_search", result=result)["report"]
    after = value.snapshot()
    assert [s["trace_id"] for s in after["suggestions"]] == ["d" * 32, "e" * 32]
    assert all(s["status"] == "pending" and s["search_id"] == report["search_id"] for s in after["suggestions"])
    assert report["published_positive_count"] == report["published_close_negative_count"] == 1
    assert [s["reason"] for s in report["skipped"]] == ["already_reviewed", "excluded"]
    assert after["manifest"]["custom"] == "keep"
    assert after["manifest"]["picks"][0] == before["manifest"]["picks"][0]
    assert [p["strategy"] for p in after["manifest"]["picks"][1:]] == ["mode_search"] * 2
    assert after["annotations"] == before["annotations"]
    assert after["patterns"] == before["patterns"]
    assert after["labels"] == {}
    second = value.publish_search(result)
    assert second["published_ids"] == []
    assert value.snapshot() == after
    result["candidates"][-1]["quote"] = "Fabricated text"
    with pytest.raises(ValueError, match="quote"):
        value.publish_search(result)
    assert value.snapshot() == after


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
