"""Trace selection signals: clustering, semantic neighbors, uncertainty.

This module holds the sampling machinery that ``select_traces`` and
``next_to_label`` in ``tools.py`` call. It reads a trace export (the
Langfuse export the tool pulls, or the committed demo export) and returns
trace ids with a one-line reason.

The signals are intentionally simple and dependency-light so the skill runs
anywhere:

  - clustering is k-means over a handful of numeric trace features (turn
    count, tool-call count, distinct tools, retrieval presence, token
    totals), standardized, with a deterministic seed;
  - semantic neighbors use a bag-of-words cosine over the trace text, which
    is enough to find "more failures like this one" without shipping an
    embedding model;
  - uncertainty reads flip counts the tool records from repeated judge runs.

None of these calls a model. A real deployment can swap the bag-of-words
neighbor step for embeddings; the interface (``next_candidates``) does not
change.
"""

from __future__ import annotations

import json
import math
import random
import re
from collections import Counter
from pathlib import Path
from typing import Any

from . import _state
from .normalization import normalize_traces


def excluded_trace_ids(root: Path | None = None) -> set[str]:
    """Read reversible human exclusions from future automatic picks."""
    document = _state.read_json(
        (root if root is not None else _state.state_root()) / "sampling_exclusions.json",
        default={"exclusions": []},
    )
    rows = document.get("exclusions") if isinstance(document, dict) else None
    if not isinstance(rows, list) or any(
        not isinstance(row, dict)
        or not isinstance(row.get("trace_id"), str)
        or not row["trace_id"]
        or type(row.get("excluded")) is not bool
        for row in rows
    ):
        raise ValueError("Sampling exclusions must contain trace IDs and boolean excluded flags")
    return {row["trace_id"] for row in rows if row["excluded"]}

# ---------------------------------------------------------------------------
# loading traces
# ---------------------------------------------------------------------------


def load_traces(source: str | Path | None) -> list[dict[str, Any]]:
    """Load traces from a JSON or JSONL export path.

    The source is a Module 1 JSON or JSONL export. Missing and empty sources
    raise because a silent empty sample makes a review appear to have run.
    """
    if source is None:
        raise ValueError("pass a Module 1 trace export path")
    path = Path(source)
    if not path.exists():
        raise FileNotFoundError(f"trace export does not exist: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"trace export is empty: {path}")
    if path.suffix == ".jsonl":
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
        return normalize_traces(records)
    data = json.loads(text)
    if isinstance(data, dict) and "traces" in data:
        data = data["traces"]
    if not isinstance(data, list):
        raise ValueError(f"trace export must contain a list of records: {path}")
    return normalize_traces(data)


# ---------------------------------------------------------------------------
# features
# ---------------------------------------------------------------------------

_FEATURES = ("turn_count", "tool_call_count", "distinct_tools", "has_retrieval", "tokens")


def _feature_vector(trace: dict[str, Any]) -> list[float]:
    """Extract the numeric feature vector used for clustering. Missing
    features default to 0 so partial exports still cluster."""
    return [float(trace.get("features", {}).get(name, 0)) for name in _FEATURES]


def _standardize(vectors: list[list[float]]) -> list[list[float]]:
    if not vectors:
        return vectors
    n_dims = len(vectors[0])
    means = [sum(v[d] for v in vectors) / len(vectors) for d in range(n_dims)]
    stds = []
    for d in range(n_dims):
        var = sum((v[d] - means[d]) ** 2 for v in vectors) / len(vectors)
        stds.append(math.sqrt(var) or 1.0)
    return [[(v[d] - means[d]) / stds[d] for d in range(n_dims)] for v in vectors]


def _kmeans(
    vectors: list[list[float]], k: int, seed: int = 7, iters: int = 25
) -> list[int]:
    """Tiny deterministic k-means; returns a cluster index per vector."""
    if not vectors:
        return []
    k = min(k, len(vectors))
    rng = random.Random(seed)
    centroids = [vectors[i][:] for i in rng.sample(range(len(vectors)), k)]
    assign = [0] * len(vectors)
    for _ in range(iters):
        changed = False
        for i, v in enumerate(vectors):
            best, best_d = 0, float("inf")
            for c, cen in enumerate(centroids):
                d = sum((a - b) ** 2 for a, b in zip(v, cen))
                if d < best_d:
                    best, best_d = c, d
            if assign[i] != best:
                assign[i] = best
                changed = True
        for c in range(k):
            members = [vectors[i] for i in range(len(vectors)) if assign[i] == c]
            if members:
                centroids[c] = [
                    sum(m[d] for m in members) / len(members)
                    for d in range(len(members[0]))
                ]
        if not changed:
            break
    return assign


# ---------------------------------------------------------------------------
# select: diversity / random / outlier
# ---------------------------------------------------------------------------


def select(
    traces: list[dict[str, Any]],
    k: int,
    strategy: str,
    exclude_ids: set[str],
    seed: int = 7,
) -> list[dict[str, str]]:
    """Return ``k`` picks as ``{"trace_id", "reason"}`` dicts."""
    pool = [t for t in traces if t.get("id") not in exclude_ids]
    if not pool:
        return []
    if strategy == "random":
        return _random_picks(pool, k, seed)
    if strategy == "outlier":
        return _outlier_picks(pool, k)
    return _diversity_picks(pool, k, seed)


def _random_picks(pool: list[dict[str, Any]], k: int, seed: int) -> list[dict[str, str]]:
    rng = random.Random(seed)
    chosen = rng.sample(pool, min(k, len(pool)))
    return [{"trace_id": t["id"], "reason": "random pick"} for t in chosen]


def _diversity_picks(
    pool: list[dict[str, Any]], k: int, seed: int
) -> list[dict[str, str]]:
    """Two thirds cluster representatives, one third random. This is the
    ``select_traces`` diversity default: cluster the store on trace features
    and return representatives from each cluster plus random picks, because
    clustering never captures every dimension."""
    n_rep = max(1, (k * 2) // 3)
    n_rand = k - n_rep
    n_clusters = min(max(1, n_rep // 2), len(pool))

    vectors = _standardize([_feature_vector(t) for t in pool])
    assign = _kmeans(vectors, k=n_clusters, seed=seed)

    rng = random.Random(seed)
    picks: list[dict[str, str]] = []
    chosen_ids: set[str] = set()

    # Representatives: for each cluster, take the members closest to the
    # centroid first, spreading picks evenly across clusters.
    by_cluster: dict[int, list[int]] = {}
    for i, c in enumerate(assign):
        by_cluster.setdefault(c, []).append(i)
    clusters = sorted(by_cluster)
    ci = 0
    while len([p for p in picks]) < n_rep and clusters:
        c = clusters[ci % len(clusters)]
        members = by_cluster[c]
        if members:
            idx = members.pop(0)
            tid = pool[idx]["id"]
            if tid not in chosen_ids:
                picks.append(
                    {"trace_id": tid, "reason": f"cluster {c} representative"}
                )
                chosen_ids.add(tid)
        else:
            clusters.remove(c)
            continue
        ci += 1
        if all(not by_cluster[c] for c in clusters):
            break

    remaining = [t for t in pool if t["id"] not in chosen_ids]
    rng.shuffle(remaining)
    for t in remaining[:n_rand]:
        picks.append({"trace_id": t["id"], "reason": "random pick"})
        chosen_ids.add(t["id"])

    return picks[:k]


def _outlier_picks(pool: list[dict[str, Any]], k: int) -> list[dict[str, str]]:
    """Flag outliers by the interquartile-range rule on token totals: a
    prompt to read traces, never a failure mode by itself."""
    vals = sorted((t.get("features", {}).get("tokens", 0), t["id"]) for t in pool)
    n = len(vals)
    q1 = vals[n // 4][0]
    q3 = vals[(3 * n) // 4][0]
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    flagged = [
        {"trace_id": tid, "reason": "IQR outlier on token total"}
        for tokens, tid in vals
        if tokens < lo or tokens > hi
    ]
    return flagged[:k]


# ---------------------------------------------------------------------------
# next_candidates: enrich / uncertainty / disagreement / random
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9']+")


def _trace_text(trace: dict[str, Any]) -> str:
    """Flatten a trace to a text blob for bag-of-words similarity."""
    if "text" in trace:
        return str(trace["text"]).lower()
    parts: list[str] = []
    for turn in trace.get("turns", []):
        parts.append(str(turn.get("user", "")))
        parts.append(str(turn.get("agent", "")))
    return " ".join(parts).lower()


def _bow(text: str) -> Counter:
    return Counter(_TOKEN_RE.findall(text))


def _cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    dot = sum(a[t] * b[t] for t in common)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def next_candidates(
    traces: list[dict[str, Any]],
    mode: str,
    k: int,
    strategy: str,
    confirmed_failures: list[str],
    already_labeled: set[str],
    seed: int = 7,
) -> list[dict[str, str]]:
    """Propose the next traces to label, by the requested signal."""
    pool = [t for t in traces if t.get("id") not in already_labeled]
    if not pool:
        return []

    if strategy == "random":
        rng = random.Random(seed)
        chosen = rng.sample(pool, min(k, len(pool)))
        return [{"trace_id": t["id"], "signal": "random"} for t in chosen]

    if strategy == "uncertainty":
        scored = sorted(
            pool,
            key=lambda t: -abs(t.get("features", {}).get("judge_flip_rate", 0.0)),
        )
        return [
            {"trace_id": t["id"], "signal": "judge flips across runs"}
            for t in scored[:k]
        ]

    if strategy == "disagreement":
        conflicting = [
            t for t in pool if t.get("features", {}).get("code_vs_judge_conflict")
        ]
        return [
            {"trace_id": t["id"], "signal": "code check vs judge conflict"}
            for t in conflicting[:k]
        ]

    # enrich (default): semantic neighbors of confirmed failures.
    by_id = {t["id"]: t for t in traces}
    seed_bows = [_bow(_trace_text(by_id[f])) for f in confirmed_failures if f in by_id]
    if not seed_bows:
        # No confirmed failures yet: fall back to a diverse read.
        return [
            {"trace_id": p["trace_id"], "signal": "no confirmed failures; diverse pick"}
            for p in _diversity_picks(pool, k, seed)
        ]
    scored = []
    for t in pool:
        bow = _bow(_trace_text(t))
        best = max((_cosine(bow, s) for s in seed_bows), default=0.0)
        scored.append((best, t["id"]))
    scored.sort(key=lambda x: -x[0])
    return [
        {"trace_id": tid, "signal": "semantic neighbor of a confirmed failure"}
        for _, tid in scored[:k]
    ]
