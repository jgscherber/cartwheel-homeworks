"""Run with ``uv run python -m analysis.review_app.server`` from the repo root."""

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

from analysis.helpers import _state, langfuse_io
from analysis.review_app.loader import graph, load_records, sessions

ASSETS = Path(__file__).parent
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
        )]
        paths += sorted((self.root / "labels").glob("*.jsonl"))
        for path in paths:
            digest.update(path.name.encode())
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
                "identity": hashlib.sha256(f"{self.root.resolve()}:{self.source}".encode()).hexdigest(),
                "source": self.source, "offline_reason": self.offline_reason,
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
                    names.append(name)
                if len(names) != len(set(names)):
                    raise ValueError("Duplicate mode names")
                self.write("patterns.json", patterns)
                return {"ok": True}
            if action == "suggestion":
                return self.decide_suggestion(payload)
            trace_id = payload.get("trace_id")
            if trace_id not in self.traces:
                raise ValueError("Unknown trace ID")
            if action == "select":
                reason = str(payload.get("reason", "")).strip()
                if not reason:
                    raise ValueError("Record the reason this trace enters the review set")
                manifest = self.read("sample_manifest.json", {"picks": []})
                picks = list(manifest.get("picks", []))
                if not any(p["trace_id"] == trace_id for p in picks):
                    picks.append({"trace_id": trace_id, "reason": reason, "selected_at": now()})
                self.write("sample_manifest.json", {
                    **manifest, "picks": picks, "source": self.source,
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
        document = self.read("suggestions.json", [])
        suggestions = items(document, "suggestions")
        suggestion = next((s for s in suggestions if s.get("id") == payload.get("id")), None)
        if not suggestion or suggestion.get("trace_id") not in self.traces:
            raise ValueError("Unknown suggestion or source trace")
        if payload.get("decision") not in {"accepted", "rejected"}:
            raise ValueError("Explicitly accept or reject the suggestion")
        if suggestion.get("status", "pending") != "pending":
            raise ValueError("This suggestion already has a decision")
        if payload["decision"] == "accepted":
            annotations = self.read("annotations.json", {"annotations": []})
            notes = items(annotations, "annotations")
            note_id = f"suggestion:{suggestion['id']}"
            if not any(n.get("id") == note_id for n in notes):
                notes.append({
                    "id": note_id, "trace_id": suggestion["trace_id"],
                    "anchor": suggestion.get("anchor", "trace"),
                    "quote": suggestion.get("quote", ""),
                    "note": suggestion.get("note") or suggestion.get("mode") or "Accepted suggestion",
                    "source": "accepted_suggestion", "suggestion_id": suggestion["id"], "ts": now(),
                })
                self.write("annotations.json", replace_items(annotations, "annotations", notes))
        suggestion.update(status=payload["decision"], decided_at=now(),
                          decision_note=str(payload.get("note", "")))
        self.write("suggestions.json", replace_items(document, "suggestions", suggestions))
        return {"ok": True}

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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8021)
    parser.add_argument("--offline", type=Path, help="Complete Module 1 JSON/JSONL trace export")
    parser.add_argument("--offline-reason", default="")
    args = parser.parse_args()
    if args.offline and not args.offline_reason.strip():
        parser.error("--offline requires --offline-reason")
    from observability.instrument import load_env

    load_env()
    client = None
    if not args.offline:
        client = langfuse_io._client()
    try:
        traces = load_records(args.offline, client)
        store = ReviewStore(traces, _state.state_root(), client=client,
                            offline_reason=args.offline_reason,
                            source=str(args.offline.resolve()) if args.offline else "langfuse")
        server = make_server(store, args.port)
        print(f"Review interface: http://127.0.0.1:{server.server_port}", flush=True)
        print(f"{len(traces)} traces in {len(store.conversations)} conversations; "
              f"{'OFFLINE: ' + args.offline_reason if args.offline else 'live Langfuse'}", flush=True)
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
