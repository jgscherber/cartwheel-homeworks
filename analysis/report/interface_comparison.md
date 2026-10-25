# HW4 Part A: conversation-first review interface

The interface in `analysis/review_app/` lists complete conversations, keeps their turns together, and saves free-text observations beside the evidence. Individual trace IDs remain the unit of sampling and binary labeling.

## Review observations and approved design

The student reviewed five traces in the standard Langfuse view and reported three friction points: expanding separate traces belonging to the same conversation, writing annotations in another document, and manually filtering to find a conversation rather than choosing it from a list.

Before implementation, the coding agent inspected eight live Langfuse traces through the API. Session `a3c09d5f-e2d7-4e28-8a24-35d5a745a223` contained three separate turns: `f0371f2043a76905dfff1c3d06d359c3`, `56126eff83a88f5c06f30dafa4d3db29`, and `c0e1dfd4f64fa8c2133f24ad1b1debbf`. A follow-up question needed earlier policy retrieval to be understood in context. Other observed fields included nested session/scenario metadata, tool arguments, retrieval snippets, policy bodies, tool outputs, unordered observations, and generation observations with null outputs.

The implementation plan received an independent Claude Sonnet 5.5 review before code was written. Revisions clarified session loading, idempotent score synchronization, and the boundary between this interface and later human review work. The student then approved the visual proposal. No Workshop comparison was performed: no notes were available, and Part C is optional.

## Retained design

The reference interface's paired tool-call/result containers and adjacent free-text notes were retained. Role-specific borders distinguish messages, structured evidence uses monospace, and selected text stays highlighted while a reviewer enters a note. Retrieval and policy content remain readable, with long results expandable rather than discarded.

## Changed design

Trace-at-a-time navigation was replaced with a searchable conversation list and chronological session timeline. Grouping uses `cartwheel.session_id`, not the scenario ID: repeated scenario executions remain separate conversations. All constituent trace IDs are retained, including context-only sibling turns that do not automatically enter the review set.

The interface also separates open coding from explicit Present/Absent judgments, shows taxonomy definitions alongside supporting human notes, displays review and synchronization progress, and keeps AI suggestions separate until explicitly accepted or rejected. Rejected suggestions remain inspectable. Accepting a suggestion creates a note, never an automatic binary judgment.

## Remaining limitation

Startup retrieves a bounded-time snapshot of the full trace collection before serving the interface. This is practical for the course dataset but can be slow for a much larger Langfuse project; there is no incremental background loading.

## Running and saving

Run from the repository root with existing local `.env` credentials:

```powershell
uv run python -m analysis.review_app.server
```

Open `http://127.0.0.1:8021`. Choose a conversation, explicitly include each review trace with its selection reason, then save free-text observations beside its steps. The taxonomy starts empty; no failure modes or judgments are fabricated. In the taxonomy editor, use this shape, replacing the illustrative fields with human-reviewed definitions and annotation IDs:

```json
{
  "modes": [
    {
      "name": "your_mode",
      "status": "candidate",
      "definition": "Your binary decision rule",
      "requirement_source": "An identifier in SPEC.md or a documented revision",
      "boundary": "How this differs from the nearest neighboring mode",
      "supporting_annotation_ids": []
    }
  ]
}
```

Only modes explicitly marked `"status": "final"` become available for structured labeling. Present is `1`, Absent is `0`, and Unjudged is not saved as a score. Corrections append local history; the latest nonsuperseded record is current. The interface does not offer resetting an explicit judgment to Unjudged.

Notes, taxonomy, suggestion decisions, selection provenance, and per-mode label histories use the existing `analysis/state/` layout. Existing files and unknown document fields are preserved. Mutations reject stale state, and browser backups do not automatically overwrite newer server records.

Accepted judgments are saved locally before Langfuse synchronization. A deterministic score ID makes corrections and retries update the same app-owned remote score. Confirmation requires reading back the exact trace, score ID, mode, value, and judgment revision. Ingestion delay remains pending; failures remain visible and retryable. Free-text notes stay local; binary judgments are mirrored as Langfuse scores.

Use offline mode only when Langfuse is temporarily unavailable, with a genuine full Module 1 export and an explicit reason. Set `CARTWHEEL_ANALYSIS_STATE` to a separate local directory for a disposable demonstration:

```powershell
$env:CARTWHEEL_ANALYSIS_STATE = "$env:TEMP\cartwheel-review-demo"
uv run python -m analysis.review_app.server --offline "<full-trace-export.json>" --offline-reason "<why Langfuse is unavailable>"
```

Replace both placeholders. The dialogue-only scenario results and skeletal demonstration trace store do not replace complete tool evidence. Offline labels remain local until the same trace IDs are available in live mode and the reviewer explicitly retries synchronization.

## Observed validation

Offline checks covered session boundaries, chronology, retained raw/tool evidence, stale-write rejection, note persistence, suggestion decisions, incomplete judgments, append-only corrections, and mocked Langfuse failure/retry/confirmation. A real headless Chrome session exercised navigation, selected-text highlighting, adjacent notes, responsive layout, explicit Absent labeling, suggestion acceptance/rejection, progress, taxonomy, map navigation, and save/reload against temporary state.

A read-only live HTTP check served 284 individual traces in 219 conversations, including 48 multi-turn conversations. No live model inference was used, and no test labels or annotations were written to the student's Langfuse project. Actual remote score writes were not exercised; synchronization behavior was checked with mocks against the installed SDK contract.

Parts B through E, the student's failure assessments, the final taxonomy, and the recording remain human work; this interface does not claim those deliverables are complete.
