"""Offline HW5 input preparation and split checks."""

import json
from unittest.mock import Mock

import pytest

from analysis import run_judges
from analysis.helpers import _state

MODE = run_judges.DEFAULT_MODE


def trace(identifier, session=None, scenario=None, text=None, timestamp=None):
    return {
        "trace_id": identifier, "timestamp": timestamp or f"2026-01-01T00:00:{int(identifier) if identifier.isdigit() else 0:02d}Z",
        "meta": {"session_id": session or identifier, "scenario_id": scenario or identifier},
        "observations": [], "metadata": {"review_note": "LEAK"},
        "trace": [
            {"role": "user", "text": f"Request {identifier}" if text is None else text, "note": "LEAK"},
            {"role": "assistant", "text": f"Reply {identifier}" if text is None else text, "label": "LEAK"},
        ],
    }


@pytest.fixture
def state(tmp_path, monkeypatch):
    root = tmp_path / "state"
    monkeypatch.setenv("CARTWHEEL_ANALYSIS_STATE", str(root))
    monkeypatch.setenv("CARTWHEEL_JUDGE_TRACE_SOURCE", str(tmp_path / "unused-inputs.json"))
    return root


def seed(tmp_path, records, labels):
    source = tmp_path / "traces.json"
    _state.write_json(source, {"traces": records})
    _state.write_jsonl(_state.state_path("hw5_labels", f"{MODE}.jsonl"), [
        {"trace_id": tid, "label": label, "source": "human", "evidence": "LEAK"}
        for tid, label in labels.items()
    ])
    return source


def balanced(tmp_path):
    records = [trace(str(i)) for i in range(60)]
    labels = {str(i): int(i >= 30) for i in range(60)}
    return seed(tmp_path, records, labels)


def test_preparation_keeps_full_conversation_evidence_and_fail_dominates(state, tmp_path):
    records = [
        trace("a", "mixed", timestamp="2026-01-01T00:00:00Z"),
        trace("b", "mixed", timestamp="2026-01-01T00:01:00Z"),
        trace("c", "mixed", timestamp="2026-01-01T00:02:00Z"),
        trace("d", "pass", timestamp="2026-01-01T00:00:00Z"),
        trace("e", "pass", timestamp="2026-01-01T00:01:00Z"),
    ]
    policy = {"id": "POLICY-1", "body": "Recorded policy: refunds take five business days."}
    records[0]["trace"][1:1] = [
        {"role": "tool_call", "name": "get_policy", "arguments": {"query": "refund"}, "metadata": "LEAK"},
        {"role": "tool_result", "name": "get_policy", "content": policy, "annotation": "LEAK"},
    ]
    source = seed(tmp_path, records, {"a": 1, "b": 0, "d": 1})
    original_labels = _state.state_path("hw5_labels", f"{MODE}.jsonl").read_bytes()
    result = run_judges.prepare_inputs(trace_source=source)
    by_id = {r["trace_id"]: r for r in result}
    assert set(by_id) == {"b", "d"}
    assert by_id["b"]["trace"][0]["text"] == "Request a"
    assert by_id["b"]["trace"][-1]["text"] == "Reply c"
    assert by_id["d"]["trace"][-1]["text"] == "Reply e"
    assert by_id["b"]["trace"][1] == {"role": "tool_call", "name": "get_policy", "arguments": {"query": "refund"}}
    assert by_id["b"]["trace"][2] == {"role": "tool_result", "name": "get_policy", "content": policy}
    assert all(set(r) == {"trace_id", "trace"} for r in result)
    assert "LEAK" not in json.dumps(result)
    assert _state.read_json(state / "hw5_preparation.json")["class_counts"] == {"Pass": 1, "Fail": 1}
    assert _state.state_path("hw5_labels", f"{MODE}.jsonl").read_bytes() == original_labels


def test_duplicate_runs_reviewed_variants_and_identical_content_keep_one_record(state, tmp_path):
    source = seed(tmp_path, [
        trace("a", scenario="repeat"), trace("b", scenario="repeat"),
        trace("c"), trace("d"), trace("e", text="Identical"), trace("f", text="Identical"),
    ], {letter: 1 for letter in "abcdef"})
    inputs = run_judges.prepare_inputs(trace_source=source, related_groups={"c": "family", "d": "family"})
    assert [r["trace_id"] for r in inputs] == ["a", "c", "e"]
    assert _state.read_json(state / "hw5_preparation.json")["independent_inputs"] == 3


def test_identical_inputs_with_conflicting_labels_do_not_get_silently_deduplicated(state, tmp_path):
    source = seed(tmp_path, [trace("a", text="Identical"), trace("b", text="Identical")], {"a": 1, "b": 0})
    with pytest.raises(ValueError, match="conflicting human labels"):
        run_judges.prepare_inputs(trace_source=source)
    assert not (state / "hw5_trace_inputs.json").exists()


def test_saved_inputs_are_reused_without_reading_original_export_and_detect_changes(state, tmp_path, monkeypatch):
    source = seed(tmp_path, [trace("a")], {"a": 1})
    result = run_judges.prepare_inputs(trace_source=source, related_groups={})
    original = (state / "hw5_trace_inputs.json").read_bytes()
    monkeypatch.setattr(run_judges, "load_records", Mock(side_effect=AssertionError("Must reuse snapshot")))
    assert run_judges.prepare_inputs(trace_source=source, related_groups={}) == result
    assert (state / "hw5_trace_inputs.json").read_bytes() == original
    _state.write_json(state / "hw5_trace_inputs.json", [{"trace_id": "a", "trace": [{"role": "assistant", "text": "Changed"}]}])
    with pytest.raises(ValueError, match="inputs changed"):
        run_judges.prepare_inputs(trace_source=source, related_groups={})


def test_raw_label_counts_cannot_bypass_independent_conversation_minimum(state, tmp_path):
    source = seed(tmp_path, [
        trace(str(i), session=str(i // 2), scenario=str(i // 2))
        for i in range(60)
    ], {str(i): i % 2 for i in range(60)})
    _state.write_json(state / "splits.json", {"older_mode": {"custom": "preserve"}})
    original = (state / "splits.json").read_bytes()
    run_judges.prepare_inputs(trace_source=source, related_groups={})
    with pytest.raises(ValueError, match="30 Pass and 30 Fail independent conversations"):
        run_judges.split_data()
    assert (state / "splits.json").read_bytes() == original


def test_split_is_stratified_persisted_once_and_preserves_older_modes(state, tmp_path, monkeypatch):
    source = balanced(tmp_path)
    previous = {"older_mode": {"custom": {"preserve": True}, "train": ["original"]}}
    _state.write_json(state / "splits.json", previous)
    run_judges.prepare_inputs(trace_source=source, related_groups={})
    assignment = run_judges.split_data()
    labels = {str(i): int(i >= 30) for i in range(60)}
    for name, count in (("train", 6), ("dev", 12), ("test", 12)):
        assert sum(labels[tid] == 1 for tid in assignment[name]) == count
        assert sum(labels[tid] == 0 for tid in assignment[name]) == count
    combined = [tid for values in assignment.values() for tid in values]
    assert len(combined) == len(set(combined)) == 60
    assert _state.read_json(state / "splits.json")["older_mode"] == previous["older_mode"]
    original_split = (state / "splits.json").read_bytes()
    original_inputs = (state / "hw5_trace_inputs.json").read_bytes()
    monkeypatch.setattr(run_judges, "split_labels", Mock(side_effect=AssertionError("Must reuse split")))
    assert run_judges.split_data() == assignment
    assert (state / "splits.json").read_bytes() == original_split
    with pytest.raises(ValueError, match="saved split"):
        run_judges.prepare_inputs(trace_source=source, related_groups={}, refresh=True)
    assert (state / "hw5_trace_inputs.json").read_bytes() == original_inputs


def test_close_variant_review_is_required_before_splitting(state, tmp_path):
    source = balanced(tmp_path)
    run_judges.prepare_inputs(trace_source=source)
    with pytest.raises(ValueError, match="Review close variants"):
        run_judges.split_data()
    assert not (state / "splits.json").exists()


def test_registered_judge_blocks_refresh_even_before_predictions(state, tmp_path):
    source = seed(tmp_path, [trace("a")], {"a": 1})
    run_judges.prepare_inputs(trace_source=source)
    original = (state / "hw5_trace_inputs.json").read_bytes()
    _state.write_json(state / "judges" / f"{MODE}-v0.json", {"mode": MODE, "predictions": {}})
    with pytest.raises(ValueError, match="Judge work already exists"):
        run_judges.prepare_inputs(trace_source=source, refresh=True)
    assert (state / "hw5_trace_inputs.json").read_bytes() == original


def test_invalid_labels_do_not_create_judge_inputs(state, tmp_path):
    source = seed(tmp_path, [trace("a")], {"a": True})
    with pytest.raises(ValueError, match="integer Pass=1"):
        run_judges.prepare_inputs(trace_source=source)
    assert not (state / "hw5_trace_inputs.json").exists()
