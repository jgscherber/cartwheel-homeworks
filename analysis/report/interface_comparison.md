# HW4 Part A: conversation-first review interface

The interface in `analysis/review_app/` reads `traces/support_traces.json` by default, lists complete conversations, and saves free-text observations beside the evidence. Individual trace IDs remain the unit of sampling and binary labeling. Langfuse remains the canonical store for accepted binary judgments.

## Review observations and approved design

The student reviewed five traces in the standard Langfuse view and reported three friction points: expanding separate traces belonging to the same conversation, writing annotations in another document, and manually filtering to find a conversation rather than choosing it from a list.

Before implementation, the coding agent inspected eight live Langfuse traces through the API. Session `a3c09d5f-e2d7-4e28-8a24-35d5a745a223` contained three separate turns: `f0371f2043a76905dfff1c3d06d359c3`, `56126eff83a88f5c06f30dafa4d3db29`, and `c0e1dfd4f64fa8c2133f24ad1b1debbf`. A follow-up question needed earlier policy retrieval to be understood in context. Other observed fields included nested session/scenario metadata, tool arguments, retrieval snippets, policy bodies, tool outputs, unordered observations, and generation observations with null outputs.

The implementation plan received an independent Claude Sonnet 5.5 review before code was written. Revisions clarified session loading, idempotent score synchronization, and the boundary between this interface and later human review work. The student then approved the visual proposal. No Workshop comparison was performed: no notes were available, and Part C is optional.

After completing the HW3 export, the student requested using that saved snapshot as the interface's trace source. The export dated `2026-10-25T19:30:21.580453+00:00` contains 398 traces from 250 scenarios, grouped into 250 conversations, with no missing scenario IDs. Eight distributed exported records were inspected. Claude Sonnet 5.5 reviewed this source-update plan before implementation, emphasizing independent trace/sync status, preservation of source provenance, and recovery of existing browser drafts. This is an explicitly requested snapshot-review workflow, not a claim that Langfuse is unavailable; `--live-traces` retains the handout's live-source workflow.

## Retained design

The reference interface's paired tool-call/result containers and adjacent free-text notes were retained. Role-specific borders distinguish messages, structured evidence uses monospace, and selected text stays highlighted while a reviewer enters a note. Retrieval and policy content remain readable, with long results expandable rather than discarded.

## Changed design

Trace-at-a-time navigation was replaced with a searchable conversation list and chronological session timeline. Grouping uses `cartwheel.session_id`, not the scenario ID: repeated scenario executions remain separate conversations. All constituent trace IDs are retained, including context-only sibling turns that do not automatically enter the review set.

The sidebar conversation order is randomized on each page load and stays stable through saves, filtering, and state refreshes. Turns within a conversation remain chronological, and "Next trace" follows the displayed conversation order.

The interface also separates open coding from explicit Present/Absent judgments, shows taxonomy definitions alongside supporting human notes, displays review and synchronization progress, and keeps AI suggestions separate until explicitly accepted or rejected. Rejected suggestions remain inspectable. Accepting a suggestion creates a note, never an automatic binary judgment.

## Remaining limitation

The default dataset is a saved snapshot, not a live trace feed. Traces created after the export timestamp do not appear until the student re-exports and restarts the interface. Refreshing review state does not reload the export. The interface displays the source path separately from the judgment synchronization mode.

## Running and saving

Run from the repository root with existing local `.env` credentials. The normal command reads the committed HW3 export locally and still synchronizes accepted binary judgments with Langfuse:

```powershell
uv run python -m analysis.review_app.server
```

Use `--traces "<another-full-trace-export.json>"` for a different saved export, or `--live-traces` to fetch traces directly from Langfuse. These switches cannot be combined with each other or with `--offline`. A missing export or missing Langfuse credentials produces an error, not a silent source change or local-only fallback.

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

Role and recorded user ID are prominent at the top of each trace. Trace-level notes, "No failure observed," and "Next trace" are at the bottom. "No failure observed" saves the observation before advancing; "Next trace" advances without recording a judgment. Navigation follows turn order, then the next conversation, without wrapping at the end. "Hide traces with notes" removes fully annotated conversations from the sidebar, opens mixed conversations at their first unannotated turn, and makes Next skip annotated turns; the full conversation remains visible for context. "Include in review set" explicitly adds a trace to the HW4 sample with its selection reason, enabling structured labels and counting it in sample progress; it does not itself mark the trace reviewed.

"Don't include in the review set" saves a reversible exclusion from future automatic picks in `analysis/state/sampling_exclusions.json`. Both `select_traces` and `next_to_label` respect it. It does not remove existing review-set membership, hide evidence, create a note, or change labels; manual inclusion remains available. "Allow future automatic picks" clears the exclusion. Exclusions change the sampling pool: clean cases are still needed, so this is not a failure-only filter.

Notes, taxonomy, suggestion decisions, selection provenance, and per-mode label histories use the existing `analysis/state/` layout. Existing files and unknown document fields are preserved. New selection records include their trace source/path and score-sync mode without rewriting older picks or their legacy manifest source. Mutations reject stale state, and browser backups do not automatically overwrite newer server records. Existing live/offline backup keys are unchanged; when switching to local-export review, a matching old live-source note can be explicitly recovered if its trace is in the export.

Accepted judgments are saved locally before Langfuse synchronization. A deterministic score ID makes corrections and retries update the same app-owned remote score. Confirmation requires reading back the exact trace, score ID, mode, value, and judgment revision. Ingestion delay remains pending; failures remain visible and retryable. Free-text notes stay local; binary judgments are mirrored as Langfuse scores.

Use offline mode only when Langfuse is temporarily unavailable, with a genuine full Module 1 export and an explicit reason. Set `CARTWHEEL_ANALYSIS_STATE` to a separate local directory for a disposable demonstration:

```powershell
$env:CARTWHEEL_ANALYSIS_STATE = "$env:TEMP\cartwheel-review-demo"
uv run python -m analysis.review_app.server --offline "<full-trace-export.json>" --offline-reason "<why Langfuse is unavailable>"
```

Replace both placeholders. The dialogue-only scenario results and skeletal demonstration trace store do not replace complete tool evidence. Offline labels remain local until the same trace IDs are available in live mode and the reviewer explicitly retries synchronization.

## Observed validation

Offline checks covered session boundaries, chronology, retained raw/tool evidence, stale-write rejection, note persistence, suggestion decisions, incomplete judgments, append-only corrections, and mocked Langfuse failure/retry/confirmation. A real headless Chrome session exercised navigation, selected-text highlighting, adjacent notes, responsive layout, explicit Absent labeling, suggestion acceptance/rejection, progress, taxonomy, map navigation, and save/reload against temporary state.

Before the source update, a read-only live HTTP check served 284 individual traces in 219 conversations, including 48 multi-turn conversations. No live model inference was used, and no test labels or annotations were written to the student's Langfuse project. Actual remote score writes were not exercised; synchronization behavior was checked with mocks against the installed SDK contract.

After the source update, the normal launch command and a read-only Chrome session loaded all 398 exported trace IDs and their complete observation records in 250 conversations. Focused offline tests proved that local loading never calls the live trace API, local-source judgments still synchronize through a mocked Langfuse client, source flags remain unambiguous, and missing files/credentials do not trigger a fallback. A temporary-state Chrome workflow also recovered an old live-source note draft and confirmed a mocked judgment without creating a second score. The original export and all student analysis-state files remained byte-identical. No live model inference or live score writes were used for this update's checks.

Parts B through E, the student's failure assessments, the final taxonomy, and the recording remain human work; this interface does not claim those deliverables are complete.
