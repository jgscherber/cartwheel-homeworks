"""Load individual traces without the shared loader's scenario-level merge."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from analysis.helpers import selection
from analysis.helpers.normalization import _data, _metadata, normalize_trace


def load_records(source: Path | None, client: Any = None) -> list[dict[str, Any]]:
    if source is not None:
        text = source.read_text(encoding="utf-8")
        records = (
            [json.loads(line) for line in text.splitlines() if line.strip()]
            if source.suffix == ".jsonl"
            else json.loads(text)
        )
        if isinstance(records, dict):
            records = records["traces"]
        if not isinstance(records, list):
            raise ValueError("The export must contain a list of full trace records")
    else:
        records = []
        summaries = {}
        cutoff = datetime.now(timezone.utc)
        page = 1
        while True:
            batch = list(client.api.trace.list(
                page=page, limit=100, to_timestamp=cutoff, order_by="timestamp.asc",
            ).data or [])
            for summary in batch:
                summaries[summary.id] = summary
            if len(batch) < 100:
                break
            page += 1
        for summary in summaries.values():
            full = _data(client.api.trace.get(summary.id))
            if not (_metadata(full).get("cartwheel.scenario_id") or full.get("cartwheel_scenario_id")):
                continue
            records.append(normalize_trace(full))
    if not records:
        raise ValueError("No traces found; provide a complete Module 1 export or live project")
    traces = []
    seen = set()
    for record in records:
        if isinstance(record, dict) and not any(
            record.get(key) for key in ("trace", "input", "output", "observations", "turns", "text")
        ):
            raise ValueError("Export has no conversation evidence; skeletal demo records are not reviewable")
        # Live records have already been normalized; normalizing them twice
        # would discard meta.session_id from older exports.
        trace = record if "meta" in record and "observations" in record else normalize_trace(record)
        if source is None and not trace.get("meta", {}).get("scenario_id"):
            continue
        trace_id = trace["trace_id"]
        if trace_id in seen:
            raise ValueError(f"Duplicate trace ID: {trace_id}")
        seen.add(trace_id)
        if not trace.get("trace"):
            raise ValueError(f"{trace_id}: no conversation evidence; skeletal demo records are not reviewable")
        traces.append(trace)
    if not traces:
        raise ValueError("No Module 1 scenario traces found")
    return traces


def time_key(value: str | None) -> float:
    if not value:
        return float("inf")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def sessions(traces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for trace in traces:
        session_id = trace.get("meta", {}).get("session_id")
        key = f"session:{session_id}" if session_id else f"trace:{trace['trace_id']}"
        groups.setdefault(key, []).append(trace)
    result = []
    for key, turns in groups.items():
        turns.sort(key=lambda t: (time_key(t.get("timestamp")), t["trace_id"]))
        result.append({"id": key, "traces": turns})
    return sorted(result, key=lambda s: (time_key(s["traces"][0].get("timestamp")), s["id"]))


def graph(traces: list[dict[str, Any]]) -> dict[str, Any]:
    vectors = selection._standardize([selection._feature_vector(t) for t in traces])
    matrix = np.array(vectors, dtype=float)
    centered = matrix - matrix.mean(axis=0)
    u, singular, _ = np.linalg.svd(centered, full_matrices=False)
    projected = u[:, :2] * singular[:2]
    if projected.shape[1] == 1:
        projected = np.column_stack((projected, np.zeros(len(traces))))
    clusters = selection._kmeans(vectors, k=min(8, len(traces)))
    tokens = sorted(t.get("features", {}).get("tokens", 0) for t in traces)
    nodes = []
    for i, trace in enumerate(traces):
        length = trace.get("features", {}).get("tokens", 0)
        flag = ""
        if len(traces) >= 10 and length > tokens[int(.9 * (len(tokens) - 1))]:
            flag = "Top 10% estimated text length"
        nodes.append({
            "trace_id": trace["trace_id"], "x": float(projected[i, 0]),
            "y": float(projected[i, 1]), "cluster": clusters[i], "flag": flag,
        })
    return {"nodes": nodes}
