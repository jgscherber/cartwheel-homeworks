"""HW5 human-label overlays and shared Pass-positive alignment statistics."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any

from . import _state


def label_rows(root: Path, mode: str, reviews: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    path = root / "hw5_labels" / f"{mode}.jsonl"
    live = {}
    for row in _state.read_jsonl(path):
        if row.get("superseded_by"):
            continue
        if not isinstance(row.get("trace_id"), str) or not row["trace_id"]:
            raise ValueError("HW5 labels need an original trace ID")
        if type(row.get("label")) is not int or row["label"] not in (0, 1):
            raise ValueError("HW5 requires integer Pass=1 / Fail=0 labels")
        if row.get("mode", mode) != mode or row.get("label_convention", "pass_positive") != "pass_positive":
            raise ValueError("HW5 labels must match their mode and Pass-positive convention")
        live[row["trace_id"]] = dict(row)
    reviews = _state.read_json(root / "hw5_reviews.json", []) if reviews is None else reviews
    if not isinstance(reviews, list) or not all(isinstance(row, dict) for row in reviews):
        raise ValueError("HW5 reviews must be a list of objects")
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    for review in reviews:
        if review.get("mode") != mode or review.get("decision") != "label_wrong":
            continue
        tid, label = review.get("trace_id"), review.get("corrected_label")
        if review.get("source_labels_sha256") != source_hash:
            raise ValueError("HW5 source labels changed; recheck their saved corrections")
        if tid not in live or type(label) is not int or label not in (0, 1) or review.get("source") != "human":
            raise ValueError("HW5 corrections require a known trace and an explicit human binary label")
        if tid not in _state.read_json(root / "splits.json", {}).get(mode, {}).get("dev", []):
            raise ValueError("Only development labels may be corrected during HW5 review")
        if review.get("previous_label") != live[tid]["label"]:
            raise ValueError("HW5 correction history does not match the previous human label")
        live[tid] = {**live[tid], "label": label, "correction": review}
    return list(live.values())


def wilson_interval(successes: int, total: int, z: float = 1.96) -> list[float | None]:
    if total <= 0:
        return [None, None]
    rate = successes / total
    denominator = 1 + (z * z / total)
    center = (rate + (z * z / (2 * total))) / denominator
    margin = z * math.sqrt((rate * (1 - rate) / total) + (z * z / (4 * total * total))) / denominator
    return [round(max(0.0, center - margin), 4), round(min(1.0, center + margin), 4)]


def alignment(human: dict[str, int], predicted: dict[str, int], ids: list[str]) -> dict[str, Any]:
    tp = fn = tn = fp = 0
    disagreements = []
    for tid in ids:
        label, pred = human[tid], predicted[tid]
        if type(label) is not int or label not in (0, 1) or type(pred) is not int or pred not in (0, 1):
            raise ValueError("Alignment requires integer Pass=1 / Fail=0 labels and predictions")
        if label == 1 and pred == 1:
            tp += 1
        elif label == 1 and pred == 0:
            fn += 1
        elif label == 0 and pred == 0:
            tn += 1
        else:
            fp += 1
        if label != pred:
            disagreements.append(tid)
    total = tp + fn + tn + fp
    return {
        "tpr": round(tp / (tp + fn), 4) if tp + fn else 0.0,
        "tnr": round(tn / (tn + fp), 4) if tn + fp else 0.0,
        "tpr_interval": wilson_interval(tp, tp + fn),
        "tnr_interval": wilson_interval(tn, tn + fp),
        "agreement": round((tp + tn) / total, 4) if total else 0.0,
        "tp": tp, "fn": fn, "tn": tn, "fp": fp, "n": total, "disagreements": disagreements,
    }
