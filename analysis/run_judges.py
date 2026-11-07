r"""HW5 Part B: prepare conversation inputs and save one immutable split.

Run from the repository root:
    uv run python -m analysis.run_judges prepare
    uv run python -m analysis.run_judges split

Any human Fail makes the conversation Fail; existing Pass decisions cover
the whole conversation. Inputs contain recorded messages only, not labels.
Automatic deduplication handles repeated scenario IDs and identical inputs.
Before splitting, review close variants and supply --related-groups with a
JSON object mapping original trace IDs to shared group names ({} if none).
Use --refresh-inputs to apply new labels/groups before any judge run.

For later judge commands in PowerShell:
    $env:CARTWHEEL_JUDGE_TRACE_SOURCE = Join-Path (Get-Location).Path 'analysis\state\hw5_trace_inputs.json'
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from analysis.helpers import _state, split_labels
from analysis.review_app.loader import load_records, sessions

DEFAULT_MODE = "unsupported_operational_claim"
DEFAULT_TRACES = Path("traces") / "support_traces.json"
MESSAGE_FIELDS = {"role", "text", "name", "arguments", "content"}
FRACTIONS = (0.20, 0.40, 0.40)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _labels(mode: str) -> dict[str, int]:
    if not re.fullmatch(r"[a-z][a-z0-9_]*", mode):
        raise ValueError("Mode names must be snake_case")
    path = _state.state_path("hw5_labels", f"{mode}.jsonl")
    if not path.exists():
        raise FileNotFoundError(f"Export HW5 human labels first: {path}")
    current = {}
    for row in _state.read_jsonl(path):
        if row.get("superseded_by"):
            continue
        if not isinstance(row.get("trace_id"), str) or not row["trace_id"]:
            raise ValueError("Every label needs an original trace_id")
        if type(row.get("label")) is not int or row["label"] not in (0, 1):
            raise ValueError("HW5 requires integer Pass=1 / Fail=0 labels")
        if row.get("mode", mode) != mode or row.get("label_convention", "pass_positive") != "pass_positive":
            raise ValueError("Labels must match the selected mode and HW5 convention")
        current[row["trace_id"]] = row["label"]
    if not current:
        raise ValueError(f"No current human labels for {mode}")
    return current


def _no_judge_runs(mode: str) -> None:
    for path in _state.state_path("judges").glob("*.json"):
        judge = _state.read_json(path)
        if judge.get("mode") == mode:
            raise ValueError("Judge work already exists; keep the prepared inputs and splits unchanged")


def _clean_message(message: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(message, dict) or not isinstance(message.get("role"), str) or not message["role"]:
        raise ValueError("Recorded messages need a role")
    clean = {key: value for key, value in message.items() if key in MESSAGE_FIELDS}
    if not any(key in clean for key in ("text", "arguments", "content")):
        raise ValueError("Recorded messages need text or tool data")
    return clean


def _deduplicate(
    conversations: list[dict[str, Any]], records: list[dict[str, Any]], groups: dict[str, str],
    labels: dict[str, int],
) -> list[dict[str, Any]]:
    parents = list(range(len(records)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    seen = {}
    for index, (conversation, record) in enumerate(zip(conversations, records)):
        content = ("content", _fingerprint(record["trace"]))
        if content in seen and labels[records[seen[content]]["trace_id"]] != labels[record["trace_id"]]:
            raise ValueError("Identical conversations have conflicting human labels; review them before preparing inputs")
        keys = {content}
        for trace in conversation["traces"]:
            scenario = trace.get("meta", {}).get("scenario_id")
            if scenario:
                keys.add(("scenario", scenario))
            if trace["trace_id"] in groups:
                keys.add(("reviewed_variant", groups[trace["trace_id"]]))
        for key in keys:
            if key in seen:
                parents[find(index)] = find(seen[key])
            else:
                seen[key] = index
    kept = []
    components = set()
    for index, record in enumerate(records):
        component = find(index)
        if component not in components:
            kept.append(record)
            components.add(component)
    return kept


def _counts(records: list[dict[str, Any]], labels: dict[str, int]) -> dict[str, int]:
    return {name: sum(labels[r["trace_id"]] == value for r in records) for name, value in (("Pass", 1), ("Fail", 0))}


def _prepared(mode: str) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, int]]:
    labels = _labels(mode)
    path = _state.state_path("hw5_trace_inputs.json")
    metadata = _state.read_json(_state.state_path("hw5_preparation.json"))
    if not path.exists() or metadata is None:
        raise FileNotFoundError("Run prepare_inputs() before splitting; existing unverified inputs are not overwritten")
    if metadata.get("mode") != mode:
        raise ValueError("Saved inputs belong to another mode; preserve them")
    if metadata.get("inputs_sha256") != _digest(path):
        raise ValueError("Saved judge inputs changed; restore the original snapshot")
    if metadata.get("labels_sha256") != _digest(_state.state_path("hw5_labels", f"{mode}.jsonl")):
        raise ValueError("Human labels changed; refresh inputs explicitly before any judge run")
    records = _state.read_json(path)
    if not isinstance(records, list) or not records:
        raise ValueError("Prepared inputs must be a nonempty JSON list")
    ids = []
    for record in records:
        if not isinstance(record, dict) or set(record) != {"trace_id", "trace"} or record["trace_id"] not in labels:
            raise ValueError("Each input needs only trace_id and trace, with a matching human label")
        if not isinstance(record["trace"], list) or not record["trace"]:
            raise ValueError("Prepared conversations cannot be empty")
        if any(_clean_message(message) != message for message in record["trace"]):
            raise ValueError("Review metadata must not enter judge messages")
        ids.append(record["trace_id"])
    if len(ids) != len(set(ids)):
        raise ValueError("Prepared trace IDs must be unique")
    os.environ["CARTWHEEL_JUDGE_TRACE_SOURCE"] = str(path.resolve())
    return records, metadata, labels


def prepare_inputs(
    mode: str = DEFAULT_MODE, trace_source: Path = DEFAULT_TRACES, *,
    related_groups: dict[str, str] | None = None, refresh: bool = False,
) -> list[dict[str, Any]]:
    """Save full conversations, using a Fail-labeled ID whenever any turn fails.

    Later unlabeled turns are retained as conversation evidence. Existing
    prepared data is reused without fetching the original export again.
    Group keys can identify any constituent original trace, including context
    turns. Related conversations keep the earliest recorded conversation,
    not whichever conversation has the preferred outcome.
    """
    labels = _labels(mode)
    source = Path(trace_source)
    path = _state.state_path("hw5_trace_inputs.json")
    if path.exists() and not refresh:
        records, metadata, _ = _prepared(mode)
        if metadata["trace_source"] != str(source.resolve()) or metadata["related_groups"] != related_groups:
            raise ValueError("Use --refresh-inputs explicitly to change sources or reviewed variant groups")
        return records
    _no_judge_runs(mode)
    if path.exists():
        if mode in _state.read_json(_state.state_path("splits.json"), {}):
            raise ValueError("A saved split already uses these inputs; preserve both artifacts")
        metadata = _state.read_json(_state.state_path("hw5_preparation.json"))
        if not metadata or metadata.get("mode") != mode:
            raise ValueError("Preserve existing inputs without matching preparation provenance")
    label_path = _state.state_path("hw5_labels", f"{mode}.jsonl")
    label_hash, source_hash = _digest(label_path), _digest(source)
    traces = load_records(source)
    known = {t["trace_id"] for t in traces}
    if not set(labels) <= known:
        raise ValueError("Some human-labeled original trace IDs are missing from the export")
    if related_groups is not None and (
        not isinstance(related_groups, dict) or not set(related_groups) <= known
        or any(not isinstance(value, str) or not value.strip() for value in related_groups.values())
    ):
        raise ValueError("Reviewed variant groups must map known trace IDs to nonempty group names")
    conversations, records = [], []
    for conversation in sessions(traces):
        labeled = [t for t in conversation["traces"] if t["trace_id"] in labels]
        if not labeled:
            continue
        failing = [t for t in labeled if labels[t["trace_id"]] == 0]
        representative = (failing or labeled)[-1]
        conversations.append(conversation)
        records.append({
            "trace_id": representative["trace_id"],
            "trace": [_clean_message(message) for t in conversation["traces"] for message in t["trace"]],
        })
    records = _deduplicate(conversations, records, related_groups or {}, labels)
    if label_hash != _digest(label_path) or source_hash != _digest(source) or _labels(mode) != labels:
        raise ValueError("Source data changed during preparation; retry from a consistent snapshot")
    _no_judge_runs(mode)
    _state.write_json(path, records)
    _state.write_json(_state.state_path("hw5_preparation.json"), {
        "mode": mode, "aggregation": "any_human_fail_conversation",
        "trace_source": str(source.resolve()), "trace_source_sha256": source_hash,
        "labels_sha256": label_hash, "inputs_sha256": _digest(path),
        "related_groups": related_groups, "related_groups_reviewed": related_groups is not None,
        "labeled_conversations": len(conversations), "independent_inputs": len(records),
        "class_counts": _counts(records, labels),
    })
    os.environ["CARTWHEEL_JUDGE_TRACE_SOURCE"] = str(path.resolve())
    return records


def split_data(mode: str = DEFAULT_MODE) -> dict[str, list[str]]:
    """Create or verify the saved 20/40/40 split without replacing an old split.

    mode identifies the evaluated failure category. It selects the HW5 label
    file and the key in splits.json, not the judge model or a split name.
    """
    records, metadata, labels = _prepared(mode)
    counts = _counts(records, labels)
    if any(count < 30 for count in counts.values()):
        raise ValueError(f"Need at least 30 Pass and 30 Fail independent conversations; currently {counts}")
    if not metadata["related_groups_reviewed"]:
        raise ValueError("Review close variants and prepare with --related-groups ({} if none) before splitting")
    path = _state.state_path("splits.json")
    document = _state.read_json(path, {})
    ids = {r["trace_id"] for r in records}
    if mode in document:
        saved = document[mode]
        if saved.get("inputs_sha256") != metadata["inputs_sha256"] or saved.get("labels_sha256") != metadata["labels_sha256"]:
            raise ValueError("An existing split uses different or unverified inputs/labels; preserve it")
        if saved.get("seed") != 7 or saved.get("fractions") != list(FRACTIONS):
            raise ValueError("Preserve the existing split; its parameters differ from HW5 Part B")
        assignment = {name: saved[name] for name in ("train", "dev", "test")}
        if saved.get("assignment_sha256") != _fingerprint(assignment):
            raise ValueError("The saved split assignment changed; restore the original split")
        combined = [tid for values in assignment.values() for tid in values]
        if len(combined) != len(set(combined)) or set(combined) != ids:
            raise ValueError("Saved splits must be disjoint and cover exactly the eligible conversations")
    else:
        _no_judge_runs(mode)
        assignment = split_labels(mode, fractions=FRACTIONS, seed=7, min_per_class=10, eligible_trace_ids=sorted(ids))
        document = _state.read_json(path)
        document[mode].update(
            inputs_sha256=metadata["inputs_sha256"], labels_sha256=metadata["labels_sha256"],
            assignment_sha256=_fingerprint(assignment),
        )
        _state.write_json(path, document)
    print(json.dumps({name: _counts([{"trace_id": tid} for tid in values], labels) for name, values in assignment.items()}, indent=2))
    return assignment


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Save label-free conversation inputs")
    prepare.add_argument("--mode", default=DEFAULT_MODE)
    prepare.add_argument("--traces", type=Path, default=DEFAULT_TRACES)
    prepare.add_argument("--related-groups", type=Path, help="Reviewed JSON mapping: trace ID -> variant group ({} if none)")
    prepare.add_argument("--refresh-inputs", action="store_true", help="Refresh explicitly, only before any judge run")
    split = commands.add_parser("split", help="Save or verify the immutable 20/40/40 split")
    split.add_argument("--mode", default=DEFAULT_MODE)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            groups = None
            if args.related_groups is not None:
                if not args.related_groups.exists():
                    raise FileNotFoundError(args.related_groups)
                groups = _state.read_json(args.related_groups)
                if not isinstance(groups, dict):
                    raise ValueError("Reviewed variant groups must be a JSON object")
            prepare_inputs(args.mode, args.traces, related_groups=groups, refresh=args.refresh_inputs)
            print(json.dumps(_state.read_json(_state.state_path("hw5_preparation.json")), indent=2))
        else:
            split_data(args.mode)
    except (ValueError, FileNotFoundError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
