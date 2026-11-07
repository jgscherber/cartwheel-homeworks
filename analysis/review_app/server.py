"""Review traces/support_traces.json; synchronize judgments with Langfuse.

Run ``uv run python -m analysis.review_app.server`` from the repository root.
Use --live-traces to fetch traces from Langfuse instead of the saved export.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from analysis.helpers import _state, langfuse_io, selection
from analysis.review_app.loader import graph, load_records, sessions
from analysis.review_app import development

ASSETS = Path(__file__).parent
DEFAULT_EXPORT = ASSETS.parent.parent / "traces" / "support_traces.json"
MODE = re.compile(r"[a-z][a-z0-9_]*")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def items(document: Any, key: str) -> list[dict[str, Any]]:
    values = document.get(key, []) if isinstance(document, dict) else document
    if not isinstance(values, list) or not all(isinstance(v, dict) for v in values):
        raise ValueError(f"{key} must be a list of objects")
    return values


def replace_items(document: Any, key: str, values: list[dict[str, Any]]) -> Any:
    return {**document, key: values} if isinstance(document, dict) else values


class Conflict(ValueError):
    pass


class ReviewStore:
    def __init__(
        self, traces: list[dict[str, Any]], root: Path, *,
        client: Any = None, offline_reason: str = "", source: str = "langfuse",
    ):
        self.traces = {t["trace_id"]: t for t in traces}
        self.conversations = sessions(traces)
        self.graph = graph(traces)
        self.root = root
        self.client = client
        self.offline_reason = offline_reason
        self.source = source
        self.lock = threading.RLock()

    def read(self, name: str, default: Any) -> Any:
        return _state.read_json(self.root / name, default)

    def write(self, name: str, value: Any) -> None:
        _state.write_json(self.root / name, value)

    def revision(self) -> str:
        digest = hashlib.sha256()
        paths = [self.root / name for name in (
            "annotations.json", "patterns.json", "suggestions.json", "sample_manifest.json",
            "sampling_exclusions.json",
            "splits.json", "hw5_preparation.json", "hw5_trace_inputs.json", "hw5_reviews.json",
        )]
        paths += sorted((self.root / "labels").glob("*.jsonl"))
        paths += sorted((self.root / "hw5_labels").glob("*.jsonl"))
        paths += sorted((self.root / "judges").glob("*.json"))
        for path in paths:
            digest.update(str(path.relative_to(self.root)).encode())
            if path.exists():
                digest.update(path.read_bytes())
        return digest.hexdigest()

    def labels(self) -> dict[str, dict[str, Any]]:
        current: dict[str, dict[str, Any]] = {}
        for path in sorted((self.root / "labels").glob("*.jsonl")):
            for row in _state.read_jsonl(path):
                if not row.get("superseded_by"):
                    current[f"{path.stem}:{row['trace_id']}"] = {**row, "mode": path.stem}
        return current

    def source_info(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "trace_source": "live" if self.source == "langfuse" else "local_export",
            "trace_path": None if self.source == "langfuse" else self.source,
            "sync_mode": "langfuse" if self.client is not None else "local_only",
        }

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            patterns = self.read("patterns.json", {"modes": []})
            modes = items(patterns, "modes")
            annotations = items(self.read("annotations.json", {"annotations": []}), "annotations")
            suggestions = items(self.read("suggestions.json", []), "suggestions")
            manifest = self.read("sample_manifest.json", {"picks": []})
            picks = manifest.get("picks", [])
            selected = {p["trace_id"] for p in picks if p["trace_id"] in self.traces}
            labels = self.labels()
            final = [m["name"] for m in modes if m.get("status") == "final"]
            reviewed = {a["trace_id"] for a in annotations if a.get("trace_id") in selected and not a.get("deleted")}
            missing = [
                {"trace_id": t, "mode": m} for t in sorted(selected) for m in final
                if f"{m}:{t}" not in labels
            ]
            return {
                "revision": self.revision(), "sessions": self.conversations,
                "annotations": annotations, "patterns": patterns, "suggestions": suggestions,
                "manifest": manifest, "labels": labels, "graph": self.graph,
                "development": development.load(self.root),
                "excluded_trace_ids": sorted(selection.excluded_trace_ids(self.root)),
                "identity": hashlib.sha256(f"{self.root.resolve()}:{self.source}".encode()).hexdigest(),
                "legacy_identity": (
                    hashlib.sha256(f"{self.root.resolve()}:langfuse".encode()).hexdigest()
                    if self.source != "langfuse" and self.client is not None else None
                ),
                **self.source_info(), "offline_reason": self.offline_reason,
                "progress": {
                    "selected": len(selected), "reviewed": len(reviewed), "missing": missing,
                    "pending_suggestions": sum(s.get("status", "pending") == "pending" for s in suggestions),
                    "unsynced": sum(
                        r.get("sync_status") != "confirmed" for r in labels.values()
                        if r["trace_id"] in selected
                    ),
                },
            }

    def mutate(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if payload.get("revision") != self.revision():
                raise Conflict("State changed. Refresh before saving; your draft remains in the browser backup.")
            action = payload.get("action")
            if action == "development_review":
                return development.save(self.root, payload)
            if action == "patterns":
                patterns = payload.get("patterns")
                names = []
                for mode in items(patterns, "modes"):
                    name = mode.get("name", "")
                    if not isinstance(name, str) or not MODE.fullmatch(name):
                        raise ValueError("Mode names must be snake_case")
                    if mode.get("status", "candidate") not in {"candidate", "final"}:
                        raise ValueError("Mode status must be candidate or final")
                    if mode.get("status") == "final" and not str(mode.get("definition", "")).strip():
                        raise ValueError("A final mode needs a binary definition")
                    if mode.get("status") == "final" and "examples" in mode:
                        self.validate_final_mode(mode)
                    names.append(name)
                if len(names) != len(set(names)):
                    raise ValueError("Duplicate mode names")
                self.write("patterns.json", patterns)
                return {"ok": True}
            if action in {"taxonomy_example", "taxonomy_mode"}:
                return self.review_taxonomy(payload)
            if action == "suggestion":
                return self.decide_suggestion(payload)
            if action == "suggestion_batch":
                return self.decide_suggestions(payload)
            if action == "publish_search":
                if not isinstance(payload.get("result"), dict):
                    raise ValueError("Provide a structured search result")
                return {"ok": True, "report": self.publish_search(payload["result"])}
            trace_id = payload.get("trace_id")
            if trace_id not in self.traces:
                raise ValueError("Unknown trace ID")
            if action == "exclude":
                excluded = payload.get("excluded")
                if type(excluded) is not bool:
                    raise ValueError("Choose whether to exclude this trace from automatic picks")
                selection.excluded_trace_ids(self.root)
                document = self.read("sampling_exclusions.json", {"exclusions": []})
                rows = items(document, "exclusions")
                previous = next((row for row in rows if row["trace_id"] == trace_id), None)
                entry = {
                    **(previous or {}), "trace_id": trace_id, "excluded": excluded,
                    "updated_at": now(), "decision_source": "human", **self.source_info(),
                }
                if previous is not None:
                    rows[rows.index(previous)] = entry
                else:
                    rows.append(entry)
                self.write("sampling_exclusions.json", replace_items(document, "exclusions", rows))
                return {"ok": True}
            if action == "select":
                reason = str(payload.get("reason", "")).strip()
                if not reason:
                    raise ValueError("Record the reason this trace enters the review set")
                manifest = self.read("sample_manifest.json", {"picks": []})
                picks = list(manifest.get("picks", []))
                if not any(p["trace_id"] == trace_id for p in picks):
                    picks.append({
                        "trace_id": trace_id, "reason": reason, "selected_at": now(),
                        **self.source_info(),
                    })
                self.write("sample_manifest.json", {
                    **manifest, **self.source_info(), "picks": picks,
                    "source": manifest.get("source", self.source),
                    "offline_reason": self.offline_reason,
                })
                return {"ok": True}
            if action in {"note", "delete_note"}:
                return self.save_note(payload)
            selected = {p["trace_id"] for p in self.read("sample_manifest.json", {}).get("picks", [])}
            if trace_id not in selected:
                raise ValueError("Include the trace in the review set before labeling")
            if action == "label":
                return self.save_label(payload)
            if action == "retry":
                return self.sync_label(trace_id, payload.get("mode"))
            raise ValueError("Unknown action")

    def taxonomy_examples(self, mode: dict[str, Any], notes: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        annotations = {
            a["id"]: a for a in (notes if notes is not None else items(self.read("annotations.json", {"annotations": []}), "annotations"))
            if not a.get("deleted") and a.get("source", "human") in {"human", "accepted_suggestion"}
        }
        excluded = selection.excluded_trace_ids(self.root)
        return [
            example for example in items(mode, "examples")
            if example.get("trace_id") in self.traces
            and example["trace_id"] not in excluded
            and example.get("annotation_id") in annotations
            and annotations[example["annotation_id"]]["trace_id"] == example["trace_id"]
        ]

    def validate_final_mode(self, mode: dict[str, Any]) -> None:
        if mode.get("review_status") != "accepted" or mode.get("reviewed_definition") != mode.get("definition"):
            raise ValueError("Approve the current definition before finalizing")
        if mode.get("requirement_status") not in {"existing", "revised"} or not str(mode.get("requirement_source", "")).strip():
            raise ValueError("Resolve and document the specification requirement before finalizing")
        if mode.get("requirement_status") == "revised":
            revision_text = str(mode.get("requirement_revision_text", "")).strip()
            specification = (ASSETS.parent.parent / "SPEC.md").read_text(encoding="utf-8")
            if not revision_text or " ".join(revision_text.split()) not in " ".join(specification.split()):
                raise ValueError("Record requirement_revision_text from the approved SPEC.md revision before finalizing")
        if not mode.get("originating_annotation_ids") or not str(mode.get("boundary", "")).strip():
            raise ValueError("Record the human origins and mode boundary before finalizing")
        eligible = self.taxonomy_examples(mode)
        if not set(mode["originating_annotation_ids"]) & {e["annotation_id"] for e in eligible}:
            raise ValueError("A final mode needs an eligible originating human annotation")
        if mode.get("evaluator_type") not in {"code", "llm_judge"}:
            raise ValueError("Choose code or llm_judge as the likely evaluator")
        examples = [
            e for e in eligible
            if e.get("status") == "accepted" and e.get("reviewed_definition") == mode.get("definition")
        ]
        positives = {e["trace_id"] for e in examples if e.get("kind") == "positive"}
        negatives = {e["trace_id"] for e in examples if e.get("kind") == "close_negative"}
        absent = {e["trace_id"] for e in examples if e.get("kind") in {"close_negative", "negative"}}
        if positives & absent:
            raise ValueError("A trace cannot be both positive and negative for one mode")
        if len(positives) < 3:
            raise ValueError("Confirm at least three distinct positive traces before finalizing")
        if len(negatives) < 3 and not str(mode.get("close_negative_unavailability_reason", "")).strip():
            raise ValueError("Confirm three close negatives, or document why reviewed data does not permit them")

    def review_taxonomy(self, payload: dict[str, Any]) -> dict[str, Any]:
        document = self.read("patterns.json", {"modes": []})
        mode = next((m for m in items(document, "modes") if m.get("name") == payload.get("mode")), None)
        if mode is None or "examples" not in mode:
            raise ValueError("Choose an existing taxonomy proposal")
        decision = payload.get("decision")
        if decision not in {"accepted", "rejected", "pending", "final"}:
            raise ValueError("Choose an explicit taxonomy review decision")
        if not str(mode.get("definition", "")).strip():
            raise ValueError("A taxonomy proposal needs a binary definition")
        if payload["action"] == "taxonomy_example":
            if decision == "final":
                raise ValueError("Examples are accepted or rejected, not finalized")
            example = next((e for e in items(mode, "examples") if e.get("id") == payload.get("id")), None)
            if example is None:
                raise ValueError("Choose an existing proposed example")
            if decision == "accepted":
                if example not in self.taxonomy_examples(mode):
                    raise ValueError("Excluded, deleted, or unreviewed evidence cannot be accepted into a mode")
                if example.get("kind") not in {"positive", "close_negative", "negative"}:
                    raise ValueError("Choose a positive, close-negative, or true-negative example")
                if any(
                    e["trace_id"] == example["trace_id"]
                    and (e.get("kind") == "positive") != (example["kind"] == "positive")
                    and e.get("status") == "accepted" and e.get("reviewed_definition") == mode["definition"]
                    for e in self.taxonomy_examples(mode)
                ):
                    raise ValueError("This trace already has the opposite decision in this mode")
            example.update(status=decision, reviewed_definition=mode["definition"])
            example.setdefault("review_history", []).append({
                "decision": decision, "definition": mode["definition"], "ts": now(),
                "source": "human", "note": str(payload.get("note", "")),
            })
            mode["supporting_annotation_ids"] = list(dict.fromkeys(
                e["annotation_id"] for e in self.taxonomy_examples(mode)
                if e.get("kind") == "positive" and e.get("status") == "accepted"
                and e.get("reviewed_definition") == mode["definition"]
            ))
            mode["status"] = "candidate"
        elif decision == "final":
            self.validate_final_mode(mode)
            mode["status"] = "final"
        else:
            mode.update(review_status=decision, reviewed_definition=mode["definition"], status="candidate")
        if payload["action"] == "taxonomy_mode":
            mode.setdefault("review_history", []).append({
                "decision": decision, "definition": mode["definition"], "ts": now(), "source": "human",
            })
        self.write("patterns.json", document)
        return {"ok": True}

    def save_note(self, payload: dict[str, Any]) -> dict[str, Any]:
        document = self.read("annotations.json", {"annotations": []})
        notes = items(document, "annotations")
        note_id = payload.get("id")
        previous = next((n for n in notes if n.get("id") == note_id), None) if note_id else None
        if note_id and (not previous or previous.get("trace_id") != payload["trace_id"]):
            raise ValueError("Unknown annotation for this trace")
        if payload["action"] == "delete_note":
            if previous is None:
                raise ValueError("Choose an existing annotation to delete")
            previous["deleted"] = True
            previous["updated_at"] = now()
        else:
            note = str(payload.get("note", "")).strip()
            if not note:
                raise ValueError("A note cannot be empty")
            anchor = payload.get("anchor", "trace")
            trace = self.traces[payload["trace_id"]]
            anchors = {"trace"} | {f"message:{i}" for i in range(len(trace["trace"]))}
            if anchor not in anchors:
                raise ValueError("Unknown evidence anchor")
            start = payload.get("start")
            if start is not None and (type(start) is not int or start < 0):
                raise ValueError("Quote offsets must be nonnegative integers")
            entry = {
                **(previous or {}), "id": note_id or str(uuid.uuid4()),
                "trace_id": payload["trace_id"], "anchor": anchor, "note": note,
                "quote": str(payload.get("quote", "")), "source": "human", "ts": now(),
                "start": start,
            }
            if previous:
                notes[notes.index(previous)] = entry
            else:
                notes.append(entry)
        self.write("annotations.json", replace_items(document, "annotations", notes))
        return {"ok": True}

    def decide_suggestion(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.decide_suggestions({**payload, "ids": [payload.get("id")]})

    def suggestion_anchor(self, suggestion: dict[str, Any]) -> None:
        trace = self.traces[suggestion["trace_id"]]
        anchor = suggestion.get("anchor", "trace")
        quote = suggestion.get("quote", "")
        if anchor == "trace":
            if quote and suggestion.get("taxonomy_assignment"):
                raise ValueError("Quoted evidence needs a message anchor")
            return
        match = re.fullmatch(r"message:(\d+)", str(anchor))
        if not match or int(match[1]) >= len(trace["trace"]):
            raise ValueError("Unknown suggestion evidence anchor")
        message = trace["trace"][int(match[1])]
        if suggestion.get("taxonomy_assignment") and message.get("role") != "assistant":
            raise ValueError("Operational-claim suggestions must quote an assistant message")
        text = message.get("text", message.get("content", ""))
        if not isinstance(text, str):
            text = json.dumps(text, indent=2, ensure_ascii=False)
        start = suggestion.get("start")
        if start is None and not suggestion.get("taxonomy_assignment"):
            if not quote or quote in text:
                return
            raise ValueError("Suggestion quote does not match the recorded evidence")
        if type(start) is not int or start < 0 or not isinstance(quote, str) or not quote:
            raise ValueError("Suggestion quotes need an exact nonnegative character offset")
        encoded = text.encode("utf-16-le")
        if encoded[start * 2:(start + len(quote.encode("utf-16-le")) // 2) * 2] != quote.encode("utf-16-le"):
            raise ValueError("Suggestion quote does not match the recorded evidence")

    def decide_suggestions(self, payload: dict[str, Any]) -> dict[str, Any]:
        ids = payload.get("ids")
        if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
            raise ValueError("Select one or more distinct pending suggestions")
        decision = payload.get("decision")
        if decision not in {"accepted", "close_negative", "rejected"}:
            raise ValueError("Accept positives, confirm close negatives, or reject as true negatives")
        document = self.read("suggestions.json", [])
        suggestions = items(document, "suggestions")
        chosen = [next((s for s in suggestions if s.get("id") == identifier), None) for identifier in ids]
        if any(s is None or s.get("trace_id") not in self.traces for s in chosen):
            raise ValueError("Unknown suggestion or source trace")
        if any(s.get("status", "pending") != "pending" for s in chosen):
            raise ValueError("A selected suggestion already has a decision")
        annotations = self.read("annotations.json", {"annotations": []})
        notes = items(annotations, "annotations")
        patterns = self.read("patterns.json", {"modes": []})
        modes = items(patterns, "modes")
        excluded = selection.excluded_trace_ids(self.root)
        changed_modes: set[str] = set()
        for suggestion in chosen:
            if decision == "rejected" and not suggestion.get("taxonomy_assignment"):
                suggestion.update(status=decision, decided_at=now(), decision_note=str(payload.get("note", "")))
                continue
            if suggestion["trace_id"] in excluded:
                raise ValueError("Excluded traces cannot be accepted as suggestion evidence")
            self.suggestion_anchor(suggestion)
            note_id = f"suggestion:{suggestion['id']}"
            previous_note = next((n for n in notes if n.get("id") == note_id), None)
            if previous_note and (previous_note.get("deleted") or previous_note.get("trace_id") != suggestion["trace_id"]):
                raise ValueError("Deleted or mismatched suggestion evidence cannot be accepted")
            kind = {"accepted": "positive", "close_negative": "close_negative", "rejected": "negative"}[decision]
            mode = next((m for m in modes if m.get("name") == suggestion.get("mode")), None)
            if suggestion.get("taxonomy_assignment"):
                if mode is None or "examples" not in mode:
                    raise ValueError("Choose the existing taxonomy mode for this suggestion")
                if suggestion.get("definition") != mode.get("definition") or mode.get("review_status") != "accepted" or mode.get("reviewed_definition") != mode["definition"]:
                    raise ValueError("The mode definition changed or is not approved; re-search before accepting")
                if any(
                    e.get("trace_id") == suggestion["trace_id"]
                    and (e.get("kind") == "positive") != (kind == "positive")
                    and e.get("status") == "accepted" and e.get("reviewed_definition") == mode["definition"]
                    for e in self.taxonomy_examples(mode, notes)
                ):
                    raise ValueError("This trace already has the opposite decision in this mode")
                example_id = f"suggestion:{suggestion['id']}"
                example = next((e for e in mode["examples"] if e.get("id") == example_id), None)
                entry = {
                    **(example or {}), "id": example_id, "trace_id": suggestion["trace_id"],
                    "annotation_id": note_id, "kind": kind, "status": "accepted",
                    "reviewed_definition": mode["definition"], "source": "human_reviewed_search",
                    "rationale": suggestion.get("note", ""), "suggestion_id": suggestion["id"],
                }
                entry.setdefault("review_history", []).append({
                    "decision": "accepted", "kind": kind, "definition": mode["definition"],
                    "ts": now(), "source": "human", "note": str(payload.get("note", "")),
                })
                if example is not None:
                    mode["examples"][mode["examples"].index(example)] = entry
                else:
                    mode["examples"].append(entry)
                changed_modes.add(mode["name"])
            if not any(n.get("id") == note_id for n in notes):
                default_note = suggestion.get("note") or suggestion.get("mode") or "Accepted suggestion"
                if kind == "positive" and suggestion.get("proposed_kind") == "close_negative":
                    default_note = f"Positive for {suggestion['mode']}: this failure mode is present."
                if kind != "positive":
                    label = "Close negative" if kind == "close_negative" else "True negative"
                    default_note = f"{label} for {suggestion.get('mode') or 'this suggestion'}: this failure mode is absent."
                notes.append({
                    "id": note_id, "trace_id": suggestion["trace_id"],
                    "anchor": suggestion.get("anchor", "trace"),
                    "quote": suggestion.get("quote", ""), "start": suggestion.get("start"),
                    "note": str(payload.get("note", "")).strip() or default_note,
                    "source": "accepted_suggestion", "suggestion_id": suggestion["id"], "ts": now(),
                    "mode": suggestion.get("mode"), "review_kind": kind,
                })
            suggestion.update(status=decision, decided_at=now(), reviewed_kind=kind,
                              decision_note=str(payload.get("note", "")))
        if changed_modes:
            for mode in modes:
                if mode.get("name") in changed_modes:
                    mode["supporting_annotation_ids"] = list(dict.fromkeys(
                        e["annotation_id"] for e in self.taxonomy_examples(mode, notes)
                        if e.get("kind") == "positive" and e.get("status") == "accepted"
                        and e.get("reviewed_definition") == mode["definition"]
                    ))
        if any(s.get("reviewed_kind") for s in chosen):
            self.write("annotations.json", replace_items(annotations, "annotations", notes))
        if changed_modes:
            self.write("patterns.json", patterns)
        self.write("suggestions.json", replace_items(document, "suggestions", suggestions))
        return {"ok": True, "decided": len(chosen)}

    def publish_search(self, result: dict[str, Any]) -> dict[str, Any]:
        """Publish AI retrieval proposals without approving any examples."""
        with self.lock:
            revision = self.revision()
            name = result.get("mode")
            if name != "unsupported_operational_claim":
                raise ValueError("This search is limited to unsupported_operational_claim")
            mode = next((m for m in items(self.read("patterns.json", {"modes": []}), "modes") if m.get("name") == name), None)
            if not mode or result.get("definition") != mode.get("definition") or mode.get("review_status") != "accepted" or mode.get("reviewed_definition") != mode["definition"]:
                raise ValueError("Search results need the unchanged, approved mode definition")
            support = {
                e["trace_id"] for e in self.taxonomy_examples(mode)
                if e.get("status") == "accepted" and e.get("kind") == "positive"
                and e.get("reviewed_definition") == mode["definition"]
            }
            if len(support) < 3:
                raise ValueError("Search needs three confirmed positive traces")
            searched = result.get("eligible_trace_ids")
            if not isinstance(searched, list) or any(not isinstance(i, str) or i not in self.traces for i in searched) or len(searched) != len(set(searched)):
                raise ValueError("Search coverage must list distinct original trace IDs")
            document = self.read("suggestions.json", [])
            suggestions = items(document, "suggestions")
            annotations = items(self.read("annotations.json", {"annotations": []}), "annotations")
            reviewed = {a["trace_id"] for a in annotations if not a.get("deleted") and a.get("source", "human") in {"human", "accepted_suggestion"}}
            excluded = selection.excluded_trace_ids(self.root)
            existing = {s.get("trace_id") for s in suggestions if s.get("mode") == name and s.get("definition") == mode["definition"]}
            search_id = f"operational_claim_{uuid.uuid4().hex}"
            manifest = self.read("sample_manifest.json", {"picks": []})
            picks = items(manifest, "picks")
            selected = {p["trace_id"] for p in picks}
            added = []
            skipped = []
            for candidate in items(result, "candidates"):
                identifier = candidate.get("trace_id")
                if identifier not in searched:
                    raise ValueError("Suggestion target was not inspected in this search")
                if candidate.get("proposed_kind") not in {"positive", "close_negative"} or not isinstance(candidate.get("note"), str) or not candidate["note"].strip():
                    raise ValueError("Search suggestions need a proposed kind and evidence rationale")
                context = candidate.get("context_trace_ids", [])
                if not isinstance(context, list) or any(i not in self.traces for i in context):
                    raise ValueError("Search context must reference original trace IDs")
                conversation = next(s for s in self.conversations if any(t["trace_id"] == identifier for t in s["traces"]))
                if not set(context) <= {t["trace_id"] for t in conversation["traces"]}:
                    raise ValueError("Search context must stay within the target conversation")
                entry = {
                    **candidate, "mode": name, "definition": mode["definition"],
                    "taxonomy_assignment": True, "status": "pending",
                    "source": result.get("method", "local_agent_evidence_review"),
                    "search_id": search_id, "created_at": now(),
                }
                self.suggestion_anchor(entry)
                entry["id"] = "search-" + hashlib.sha256(json.dumps(
                    [name, mode["definition"], identifier, entry["anchor"], entry["quote"]],
                    ensure_ascii=False,
                ).encode()).hexdigest()[:24]
                reason = "excluded" if identifier in excluded else "already_reviewed" if identifier in reviewed else "duplicate" if identifier in existing else ""
                if reason:
                    skipped.append({"trace_id": identifier, "reason": reason})
                    continue
                suggestions.append(entry)
                added.append(entry)
                existing.add(identifier)
                if identifier not in selected:
                    picks.append({
                        "trace_id": identifier, "reason": f"Part D depth search for {name}: AI-proposed {entry['proposed_kind']} (unconfirmed)",
                        "strategy": "mode_search", "mode": name, "search_id": search_id,
                        "selected_at": now(), **self.source_info(),
                    })
                    selected.add(identifier)
            report = {
                **result, "search_id": search_id, "published_at": now(),
                "published_ids": [s["id"] for s in added], "skipped": skipped,
                "published_positive_count": sum(s["proposed_kind"] == "positive" for s in added),
                "published_close_negative_count": sum(s["proposed_kind"] == "close_negative" for s in added),
            }
            if self.revision() != revision:
                raise Conflict("Review state changed during publication; retry with the current state")
            if added:
                self.write("sample_manifest.json", replace_items(manifest, "picks", picks))
                self.write("suggestions.json", replace_items(document, "suggestions", suggestions))
            _state.write_json(self.root / "searches" / f"{search_id}.json", report)
            return report

    def save_label(self, payload: dict[str, Any]) -> dict[str, Any]:
        mode = payload.get("mode")
        modes = items(self.read("patterns.json", {"modes": []}), "modes")
        if not any(m.get("name") == mode and m.get("status") == "final" for m in modes):
            raise ValueError("Choose an existing final mode")
        if not isinstance(mode, str) or not MODE.fullmatch(mode):
            raise ValueError("Invalid mode")
        label = payload.get("label")
        if type(label) is not int or label not in (0, 1):
            raise ValueError("Present = 1, Absent = 0; Unjudged is not a binary judgment")
        trace_id = payload["trace_id"]
        row = {
            "trace_id": trace_id, "label": label, "source": "human", "ts": now(),
            "label_id": str(uuid.uuid4()), "note": str(payload.get("note", "")),
            "score_id": hashlib.sha256(f"hw4:{trace_id}:{mode}".encode()).hexdigest(),
            "sync_status": "pending" if self.client else "offline",
        }
        _state.append_jsonl(self.root / "labels" / f"{mode}.jsonl", row)
        return self.sync_label(trace_id, mode)

    def sync_label(self, trace_id: str, mode: str) -> dict[str, Any]:
        if not isinstance(mode, str) or not MODE.fullmatch(mode):
            raise ValueError("Invalid mode")
        row = self.labels().get(f"{mode}:{trace_id}")
        if not row or not row.get("score_id"):
            raise ValueError("No app judgment to synchronize; explicitly save a judgment first")
        if not self.client:
            return {"ok": True, "sync_status": "offline", "message": "Saved locally only"}
        if not re.fullmatch(r"[0-9a-fA-F]{32}", trace_id):
            raise ValueError("Live scores require the original Langfuse trace ID")
        # Score IDs are idempotency keys: corrections and retries update the
        # same remote score; label_id preserves the local correction history.
        try:
            self.client.create_score(
                score_id=row["score_id"], trace_id=trace_id, name=mode,
                value=float(row["label"]), data_type="NUMERIC",
                comment=f"{row.get('note', '')}\nHW4 revision {row['label_id']}",
            )
            self.client.flush()
            scores = self.client.api.score_v_2.get(
                score_ids=row["score_id"], trace_id=trace_id, limit=100,
            ).data or []
            confirmed = any(
                s.id == row["score_id"] and s.trace_id == trace_id and s.name == mode
                and s.value == row["label"] and f"HW4 revision {row['label_id']}" in (s.comment or "")
                for s in scores
            )
            row["sync_status"] = "confirmed" if confirmed else "pending_confirmation"
            row.pop("sync_error", None)
        except Exception as exc:
            # Preserve the accepted local judgment even when the canonical
            # store is down. The client sees the failure and can retry.
            row["sync_status"] = "error"
            row["sync_error"] = f"{type(exc).__name__}: Langfuse synchronization failed"
        row["sync_checked_at"] = now()
        _state.append_jsonl(self.root / "labels" / f"{mode}.jsonl",
                            {k: v for k, v in row.items() if k != "mode"})
        return {
            "ok": row["sync_status"] != "error", "sync_status": row["sync_status"],
            "message": row.get("sync_error", row["sync_status"]),
        }


def make_server(store: ReviewStore, port: int = 8021) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def send(self, data: Any, status: int = 200) -> None:
            body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            route = self.path.split("?", 1)[0]
            if route in {"/api/data", "/api/revision"}:
                try:
                    with store.lock:
                        self.send(store.snapshot() if route == "/api/data" else {"revision": store.revision()})
                except (ValueError, OSError, KeyError) as exc:
                    self.send({"error": str(exc)}, 500)
                return
            assets = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                      "/style.css": ("style.css", "text/css")}
            if route not in assets:
                self.send({"error": "Not found"}, 404)
                return
            name, mime = assets[route]
            body = (ASSETS / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", f"{mime}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' 'unsafe-inline'; object-src 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if self.path != "/api/action":
                self.send({"error": "Not found"}, 404)
                return
            origin = self.headers.get("Origin")
            if origin and origin != f"http://{self.headers.get('Host')}":
                self.send({"error": "Cross-origin writes are disabled"}, 403)
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self.send({"error": "Expected application/json"}, 415)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1_000_000:
                    raise ValueError("Body must contain 1 to 1000000 bytes")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("Expected an object")
                self.send(store.mutate(payload))
            except Conflict as exc:
                self.send({"error": str(exc)}, 409)
            except (ValueError, KeyError, TypeError) as exc:
                self.send({"error": str(exc)}, 400)
            except OSError as exc:
                self.send({"error": f"Local persistence failed: {type(exc).__name__}"}, 500)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8021)
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument("--traces", type=Path, help="Saved trace export (default: traces\\support_traces.json)")
    sources.add_argument("--live-traces", action="store_true", help="Fetch traces directly from Langfuse")
    sources.add_argument("--offline", type=Path, help="Saved export with local-only judgments; requires --offline-reason")
    parser.add_argument("--offline-reason", default="")
    args = parser.parse_args(argv)
    if args.offline and not args.offline_reason.strip():
        parser.error("--offline requires --offline-reason")
    if args.offline_reason and not args.offline:
        parser.error("--offline-reason requires --offline")
    args.traces = None if args.live_traces else args.offline or args.traces or DEFAULT_EXPORT
    return args


def main() -> None:
    args = parse_args()
    from observability.instrument import load_env

    load_env()
    client = None
    if not args.offline:
        client = langfuse_io._client()
    try:
        traces = load_records(args.traces, client)
        store = ReviewStore(traces, _state.state_root(), client=client,
                            offline_reason=args.offline_reason,
                            source=str(args.traces.resolve()) if args.traces else "langfuse")
        server = make_server(store, args.port)
        print(f"Review interface: http://127.0.0.1:{server.server_port}", flush=True)
        print(f"{len(traces)} traces in {len(store.conversations)} conversations; "
              f"trace source: {store.source}", flush=True)
        print(f"Judgments: {'local only; OFFLINE: ' + args.offline_reason if args.offline else 'Langfuse synchronization enabled'}",
              flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        if client:
            client.shutdown()


if __name__ == "__main__":
    main()
