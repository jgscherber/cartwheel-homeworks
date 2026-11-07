"""Development-only judge review, with immutable source labels and inputs."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from analysis.helpers import _state
from analysis.helpers.guards import is_frozen
from analysis.helpers.hw5 import alignment, label_rows
from analysis.helpers.tools import _prompt_hash


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def review_signature(judge: dict[str, Any], row: dict[str, Any]) -> str:
    return fingerprint([
        judge["prompt_hash"], judge["source_labels_sha256"], judge["current_definition"], judge["current_boundary"],
        row["human_label"], row["judge_label"], row["critique"],
    ])


def load(root: Path, reviews: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    reviews = _state.read_json(root / "hw5_reviews.json", []) if reviews is None else reviews
    if not isinstance(reviews, list) or not all(isinstance(row, dict) for row in reviews):
        raise ValueError("HW5 reviews must be a list of objects")
    result: dict[str, Any] = {"judges": [], "errors": []}
    documents = [
        _state.read_json(path) for path in sorted((root / "judges").glob("*.json"))
        if not path.name.startswith("_")
    ]
    patterns = _state.read_json(root / "patterns.json", {"modes": []}).get("modes", [])
    for doc in documents:
        mode = doc.get("mode")
        if not isinstance(mode, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", mode):
            result["errors"].append("A judge has an invalid mode identifier")
            continue
        if not (root / "hw5_labels" / f"{mode}.jsonl").exists():
            continue
        try:
            judge = build(root, doc, reviews, patterns)
            judge["label_corrections_allowed"] = not any(
                other.get("mode") == mode and (
                    is_frozen(other) or any(
                        tid in other.get("predictions", {}).get(other.get("prompt_hash"), {})
                        for tid in _state.read_json(root / "splits.json", {}).get(mode, {}).get("test", [])
                    )
                ) for other in documents
            )
            result["judges"].append(judge)
        except (ValueError, KeyError, OSError) as exc:
            result["errors"].append(f"{doc.get('judge_id', mode)}: {exc}")
    result["judges"].sort(key=lambda judge: (judge["mode"], -judge["version"]))
    return result


def build(root: Path, doc: dict[str, Any], reviews: list[dict[str, Any]], patterns: list[dict[str, Any]]) -> dict[str, Any]:
    mode, judge_id = doc["mode"], doc["judge_id"]
    if not re.fullmatch(re.escape(mode) + r"-v\d+", judge_id):
        raise ValueError("Invalid judge version identifier")
    if doc["prompt_hash"] != _prompt_hash(doc["prompt_text"], doc["model"]):
        raise ValueError("Judge prompt/model changed without a new version")
    metadata = _state.read_json(root / "hw5_preparation.json")
    if not metadata or metadata["mode"] != mode:
        raise ValueError("No matching saved HW5 preparation")
    if metadata["inputs_sha256"] != digest(root / "hw5_trace_inputs.json"):
        raise ValueError("Saved judge inputs changed")
    if metadata["labels_sha256"] != digest(root / "hw5_labels" / f"{mode}.jsonl"):
        raise ValueError("Original HW5 labels changed")
    saved = _state.read_json(root / "splits.json", {}).get(mode)
    if not saved:
        raise ValueError("No saved split for this judge")
    assignment = {key: saved[key] for key in ("train", "dev", "test")}
    if saved.get("assignment_sha256") != fingerprint(assignment):
        raise ValueError("Saved split assignment changed")
    if saved.get("inputs_sha256") != metadata["inputs_sha256"] or saved.get("labels_sha256") != metadata["labels_sha256"]:
        raise ValueError("Split provenance does not match the saved inputs and labels")
    inputs = {row["trace_id"]: row["trace"] for row in _state.read_json(root / "hw5_trace_inputs.json")}
    combined = [tid for ids in assignment.values() for tid in ids]
    if len(combined) != len(set(combined)) or set(combined) != set(inputs):
        raise ValueError("Saved splits must be disjoint and cover the prepared conversations")
    labels = {row["trace_id"]: row for row in label_rows(root, mode, reviews)}
    original = {row["trace_id"]: row for row in label_rows(root, mode, [])}
    ids = assignment["dev"]
    if not ids or any(tid not in labels for tid in ids):
        raise ValueError("Development needs saved inputs and human labels for every case")
    definition, boundary = original[ids[0]].get("definition", ""), original[ids[0]].get("boundary", "")
    current = next((row for row in patterns if row.get("name") == mode), {})
    judge = {
        "judge_id": judge_id, "mode": mode, "model": doc["model"], "version": int(doc["version"]),
        "status": doc["status"], "prompt_hash": doc["prompt_hash"],
        "source_labels_sha256": metadata["labels_sha256"],
        "definition": definition, "boundary": boundary,
        "current_definition": current.get("definition", definition), "current_boundary": current.get("boundary", boundary),
        "rows": [], "metrics": None,
    }
    cache = doc.get("predictions", {}).get(doc["prompt_hash"], {})
    critiques = doc.get("critiques", {}).get(doc["prompt_hash"], {})
    for tid in ids:
        pred = cache.get(tid)
        if pred is not None and (type(pred) is not int or pred not in (0, 1)):
            raise ValueError("Judge predictions must be integer binary verdicts")
        if pred is not None and doc.get("label_convention") != "pass_positive":
            pred = 1 - pred
        critique = critiques.get(tid, "")
        if not isinstance(critique, str):
            raise ValueError("Judge critiques must be text")
        history = [entry for entry in reviews if entry.get("judge_id") == judge_id and entry.get("trace_id") == tid]
        row = {
            "trace_id": tid, "human_label": labels[tid]["label"], "original_label": original[tid]["label"],
            "judge_label": pred, "critique": critique, "trace": inputs[tid],
            "evidence": original[tid].get("evidence", []), "human_note": original[tid].get("note", ""),
            "correction": labels[tid].get("correction"), "review": history[-1] if history else None,
            "review_history": history, "disagreement": pred is not None and pred != labels[tid]["label"],
        }
        row["review_current"] = bool(row["review"] and row["review"].get("signature") == review_signature(judge, row))
        row["needs_review"] = row["disagreement"] and (
            not row["review_current"] or row["review"]["decision"] == "pending"
        )
        judge["rows"].append(row)
    if all(row["judge_label"] is not None for row in judge["rows"]):
        judge["metrics"] = {
            "judge_id": judge_id, "split": "dev",
            **alignment({row["trace_id"]: row["human_label"] for row in judge["rows"]},
                        {row["trace_id"]: row["judge_label"] for row in judge["rows"]}, ids),
        }
    return judge


def save(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    before = load(root)
    judge = next((row for row in before["judges"] if row["judge_id"] == payload.get("judge_id")), None)
    if judge is None:
        raise ValueError("Choose an available development judge")
    row = next((row for row in judge["rows"] if row["trace_id"] == payload.get("trace_id")), None)
    if row is None:
        raise ValueError("Only development cases can be reviewed; test cases remain hidden")
    decision = payload.get("decision")
    if decision not in {"judge_wrong", "label_wrong", "definition_unclear", "pending"}:
        raise ValueError("Choose judge wrong, label wrong, definition unclear, or reset")
    if row["judge_label"] is None or not row["critique"].strip():
        raise ValueError("A saved judge verdict and critique are required before review")
    if decision != "pending" and not row["disagreement"]:
        raise ValueError("This case is not currently a disagreement")
    note = payload.get("note", "")
    if not isinstance(note, str) or (decision != "pending" and not note.strip()):
        raise ValueError("Record your evidence and the reason for this review decision")
    entry = {
        "id": str(uuid.uuid4()), "ts": datetime.now(timezone.utc).isoformat(), "source": "human",
        "judge_id": judge["judge_id"], "prompt_hash": judge["prompt_hash"], "mode": judge["mode"],
        "trace_id": row["trace_id"], "decision": decision, "note": note.strip(),
        "previous_label": row["human_label"], "judge_label": row["judge_label"],
        "source_labels_sha256": judge["source_labels_sha256"],
    }
    if decision == "label_wrong":
        if not judge["label_corrections_allowed"]:
            raise ValueError("A frozen judge or test predictions already exist; preserve the evaluation labels")
        label = payload.get("corrected_label")
        if type(label) is not int or label not in (0, 1) or label == row["human_label"]:
            raise ValueError("Explicitly choose the corrected HW5 label: Pass=1 or Fail=0")
        entry["corrected_label"] = label
        row = {**row, "human_label": label}
    elif payload.get("corrected_label") is not None:
        raise ValueError("A label correction only applies when you choose label wrong")
    entry["signature"] = review_signature(judge, row)
    reviews = [*_state.read_json(root / "hw5_reviews.json", []), entry]
    after = load(root, reviews)
    if after["errors"]:
        raise ValueError("Resolve judge data errors before saving review: " + "; ".join(after["errors"]))
    _state.write_json(root / "hw5_reviews.json", reviews)
    updated = []
    if decision == "label_wrong":
        for version in after["judges"]:
            if version["mode"] != judge["mode"] or version["metrics"] is None:
                continue
            path = root.parent / "report" / f"dev-{version['judge_id']}.json"
            metrics = version["metrics"]
            _state.write_json(path, {
                **_state.read_json(path, {}), **metrics, "mode": version["mode"], "model": version["model"],
                "class_counts": {"Pass": metrics["tp"] + metrics["fn"], "Fail": metrics["tn"] + metrics["fp"]},
                "label_review_updated_at": entry["ts"],
            })
            updated.append(version["judge_id"])
    return {"ok": True, "metrics_updated": updated}
