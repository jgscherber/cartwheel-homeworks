"""Offline HW5 input preparation and split checks."""

import json
from unittest.mock import Mock

import pytest

from analysis import run_judges
from analysis.helpers import _state, scale
from analysis.helpers.guards import GuardViolation

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


def test_single_class_conversations_cannot_form_a_stratified_split(state, tmp_path):
    source = seed(tmp_path, [
        trace(str(i), session=str(i // 2), scenario=str(i // 2))
        for i in range(60)
    ], {str(i): i % 2 for i in range(60)})
    _state.write_json(state / "splits.json", {"older_mode": {"custom": "preserve"}})
    original = (state / "splits.json").read_bytes()
    run_judges.prepare_inputs(trace_source=source, related_groups={})
    with pytest.raises(GuardViolation, match="minimum of 1 per class"):
        run_judges.split_data()
    assert (state / "splits.json").read_bytes() == original


def test_split_accepts_the_chosen_smaller_cohort(state, tmp_path):
    source = seed(tmp_path, [trace(str(i)) for i in range(56)], {
        str(i): int(i >= 35) for i in range(56)
    })
    run_judges.prepare_inputs(trace_source=source)
    assignment = run_judges.split_data()
    labels = {str(i): int(i >= 35) for i in range(56)}
    for name, passes, fails in (("train", 4, 7), ("dev", 8, 14), ("test", 9, 14)):
        assert sum(labels[tid] == 1 for tid in assignment[name]) == passes
        assert sum(labels[tid] == 0 for tid in assignment[name]) == fails
    assert len({tid for values in assignment.values() for tid in values}) == 56


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


def test_split_cli_uses_saved_inputs_without_close_variant_review(state, tmp_path, monkeypatch):
    source = balanced(tmp_path)
    run_judges.prepare_inputs(trace_source=source)
    original_inputs = (state / "hw5_trace_inputs.json").read_bytes()
    original_preparation = (state / "hw5_preparation.json").read_bytes()
    original_labels = (state / "hw5_labels" / f"{MODE}.jsonl").read_bytes()
    monkeypatch.setattr("sys.argv", ["run_judges.py", "split", "--mode", MODE])
    run_judges.main()
    saved = _state.read_json(state / "splits.json")[MODE]
    combined = [tid for name in ("train", "dev", "test") for tid in saved[name]]
    assert len(combined) == len(set(combined)) == 60
    assert (state / "hw5_trace_inputs.json").read_bytes() == original_inputs
    assert (state / "hw5_preparation.json").read_bytes() == original_preparation
    assert (state / "hw5_labels" / f"{MODE}.jsonl").read_bytes() == original_labels


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


def test_development_saves_metrics_critiques_and_reuses_or_versions_prompts(state, tmp_path, monkeypatch):
    source = balanced(tmp_path)
    run_judges.prepare_inputs(trace_source=source)
    assignment = run_judges.split_data()
    preserved = {path: path.read_bytes() for path in (
        state / "splits.json", state / "hw5_trace_inputs.json",
        state / "hw5_preparation.json", state / "hw5_labels" / f"{MODE}.jsonl",
    )}
    labels = {str(i): int(i >= 30) for i in range(60)}
    flipped = {next(tid for tid in assignment["dev"] if labels[tid] == value) for value in (0, 1)}
    calls = []

    def classify(prompt, model, ids):
        assert model == "gpt-4o-mini"
        assert set(ids) <= set(assignment["dev"])
        calls.append(list(ids))
        batch = scale.JudgeBatch()
        for tid in ids:
            batch[tid] = 1 - labels[tid] if tid in flipped else labels[tid]
            batch.critiques[tid] = f"Recorded evidence for request {tid}."
        return batch

    monkeypatch.setattr(scale, "classify_store", classify)
    prompt = tmp_path / "mode-v0.md"
    prompt.write_text("Initial judge prompt.", encoding="utf-8")
    result = run_judges.run_development(MODE, prompt)
    assert (result["tp"], result["fn"], result["tn"], result["fp"]) == (11, 1, 11, 1)
    assert result["tpr"] == result["tnr"] == 0.9167
    assert result["class_counts"] == {"Pass": 12, "Fail": 12}
    assert set(result["disagreements"]) == flipped
    for rate in ("tpr", "tnr"):
        low, high = result[f"{rate}_interval"]
        assert 0 < low < result[rate] < high < 1
    assert [len(ids) for ids in calls] == [10, 10, 4]
    report_path = tmp_path / "report" / f"dev-{result['judge_id']}.json"
    assert _state.read_json(report_path) == result
    judge = _state.read_json(state / "judges" / f"{result['judge_id']}.json")
    assert set(judge["predictions"][judge["prompt_hash"]]) == set(assignment["dev"])
    assert set(judge["critiques"][judge["prompt_hash"]]) == set(assignment["dev"])
    assert run_judges.run_development(MODE, prompt)["judge_id"] == result["judge_id"]
    assert len(calls) == 3
    original_report = report_path.read_bytes()
    revised_prompt = tmp_path / "mode-v1.md"
    revised_prompt.write_text("Revised judge prompt.", encoding="utf-8")
    revised = run_judges.run_development(MODE, revised_prompt)
    assert revised["judge_id"] != result["judge_id"]
    assert report_path.read_bytes() == original_report
    assert len(_state.read_json(state / "judges" / f"_history_{MODE}.json")["versions"]) == 2
    assert all(path.read_bytes() == content for path, content in preserved.items())


def test_interrupted_development_resumes_same_judge_and_only_missing_batches(state, tmp_path, monkeypatch):
    source = balanced(tmp_path)
    run_judges.prepare_inputs(trace_source=source)
    assignment = run_judges.split_data()
    prompt = tmp_path / "mode-v0.md"
    prompt.write_text("Initial judge prompt.", encoding="utf-8")
    calls = []

    def classify(prompt, model, ids):
        calls.append(list(ids))
        if len(calls) == 2:
            raise RuntimeError("Interrupted development batch")
        return {tid: int(int(tid) >= 30) for tid in ids}

    monkeypatch.setattr(scale, "classify_store", classify)
    with pytest.raises(RuntimeError, match="Interrupted development batch"):
        run_judges.run_development(MODE, prompt)
    history = _state.read_json(state / "judges" / f"_history_{MODE}.json")
    judge_id = history["versions"][0]["judge_id"]
    assert not (tmp_path / "report" / f"dev-{judge_id}.json").exists()
    result = run_judges.run_development(MODE, prompt)
    assert result["judge_id"] == judge_id
    assert result["n"] == len(assignment["dev"])
    assert result["tpr"] == result["tnr"] == 1
    assert not (set(calls[0]) & {tid for batch in calls[2:] for tid in batch})
    assert _state.read_json(state / "judges" / f"_history_{MODE}.json") == history


@pytest.mark.parametrize("problem", ["missing_split", "changed_inputs", "changed_split", "empty_prompt", "changed_judge"])
def test_development_rejects_invalid_inputs_before_registration_or_calls(state, tmp_path, monkeypatch, problem):
    source = balanced(tmp_path)
    run_judges.prepare_inputs(trace_source=source)
    prompt = tmp_path / "mode-v0.md"
    prompt.write_text("Initial judge prompt.", encoding="utf-8")
    if problem != "missing_split":
        run_judges.split_data()
    if problem == "changed_inputs":
        path = state / "hw5_trace_inputs.json"
        inputs = _state.read_json(path)
        inputs[0]["trace"][0]["text"] = "Changed input."
        _state.write_json(path, inputs)
    elif problem == "changed_split":
        path = state / "splits.json"
        saved = _state.read_json(path)
        saved[MODE]["dev"].reverse()
        _state.write_json(path, saved)
    elif problem == "empty_prompt":
        prompt.write_text(" \n", encoding="utf-8")
    elif problem == "changed_judge":
        record = run_judges.register_judge(MODE, prompt.read_text(), run_judges.JUDGE_MODEL)
        path = state / "judges" / f"{record['judge_id']}.json"
        judge = _state.read_json(path)
        judge["prompt_text"] = "Tampered judge prompt."
        _state.write_json(path, judge)
        prompt.write_text(judge["prompt_text"], encoding="utf-8")
    register = Mock(side_effect=AssertionError("Must not register a judge"))
    backend = Mock(side_effect=AssertionError("Must not call a model"))
    monkeypatch.setattr(run_judges, "register_judge", register)
    monkeypatch.setattr(scale, "classify_store", backend)
    with pytest.raises(ValueError):
        run_judges.run_development(MODE, prompt)
    register.assert_not_called()
    backend.assert_not_called()


def test_development_cli_requires_paid_call_approval(state, tmp_path, monkeypatch, capsys):
    source = balanced(tmp_path)
    run_judges.prepare_inputs(trace_source=source)
    run_judges.split_data()
    prompt = tmp_path / "mode-v0.md"
    prompt.write_text("Initial judge prompt.", encoding="utf-8")
    run = Mock()
    monkeypatch.setattr(run_judges, "run_development", run)
    args = ["run_judges.py", "develop", "--mode", MODE, "--prompt", str(prompt)]
    monkeypatch.setattr("sys.argv", args)
    with pytest.raises(SystemExit) as error:
        run_judges.main()
    assert error.value.code == 2
    assert "gpt-4o-mini on 24 development traces" in capsys.readouterr().err
    run.assert_not_called()
    assert not (state / "judges").exists()
    monkeypatch.setattr("sys.argv", args + ["--yes"])
    run_judges.main()
    run.assert_called_once_with(MODE, prompt)
