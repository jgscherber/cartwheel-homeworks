"use strict";

let data, currentSession, view = "conversation", pending = null, busy = false, recoveryKey = null;
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[c]));
const pretty = value => typeof value === "string" ? value : JSON.stringify(value, null, 2);
const traces = () => data.sessions.flatMap(s => s.traces);
const selected = id => data.manifest.picks.some(p => p.trace_id === id);
const notes = id => data.annotations.filter(a => a.trace_id === id && !a.deleted);
const modes = () => data.patterns.modes || [];
const finals = () => modes().filter(m => m.status === "final");
const judgment = (id, mode) => data.labels[`${mode}:${id}`];
const noteKey = a => `note-${a.id}`;
const backupKey = () => `cartwheel-review:${data.identity}`;

function fail(error) {
  $("#error").textContent = error.message || String(error);
  $("#error").hidden = false;
}

async function getData() {
  const response = await fetch("/api/data");
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "State could not be loaded");
  return result;
}

async function refresh() {
  if (busy || pending) throw new Error("Finish or cancel the current note before refreshing.");
  data = await getData();
  if (!data.sessions.some(s => s.id === currentSession)) currentSession = data.sessions[0]?.id;
  $("#updates").hidden = true;
  render();
}

async function act(payload) {
  if (busy) throw new Error("A save is already in progress.");
  busy = true;
  const revision = data.revision;
  localStorage.setItem(backupKey(), JSON.stringify({revision, payload, saved_at: new Date().toISOString()}));
  try {
    const response = await fetch("/api/action", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({...payload, revision})
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Save failed");
    localStorage.removeItem(backupKey());
    if (payload.action === "note" && recoveryKey) {
      localStorage.removeItem(recoveryKey);
      recoveryKey = null;
    }
    data = await getData();
    render();
    if (!result.ok) throw new Error(result.message);
    $("#error").hidden = true;
    return result;
  } finally {
    busy = false;
  }
}

function render() {
  const count = data.sessions.length;
  const source = data.trace_source === "local_export"
    ? `Local trace export: ${data.trace_path}. Re-export and restart to refresh.`
    : "Live Langfuse trace snapshot.";
  const sync = data.sync_mode === "langfuse"
    ? "Judgments synchronize with Langfuse; confirmed labels have been read back."
    : `OFFLINE: ${data.offline_reason}. Labels save locally only.`;
  $("#status").textContent = `${source} ${sync} ${count} conversations / ${traces().length} traces.`;
  document.querySelectorAll("[data-view]").forEach(b => b.classList.toggle("active", b.dataset.view === view));
  renderNavigator();
  const main = $("#main");
  if (view === "conversation") renderConversation(main);
  if (view === "taxonomy") renderTaxonomy(main);
  if (view === "labels") renderLabels(main);
  if (view === "suggestions") renderSuggestions(main);
  if (view === "progress") renderProgress(main);
  if (view === "map") renderMap(main);
}

function renderNavigator() {
  const query = $("#search").value.toLowerCase();
  $("#sessions").innerHTML = data.sessions.map(session => {
    const first = session.traces[0];
    const reviewed = session.traces.filter(t => notes(t.trace_id).length).length;
    const title = `${first.meta.scenario_id || "No scenario"} / ${first.meta.role || "Unknown role"}`;
    const search = `${session.id} ${session.traces.map(t => `${t.trace_id} ${t.meta.scenario_id} ${t.meta.role}`).join(" ")}`;
    if (!search.toLowerCase().includes(query)) return "";
    return `<button class="session ${session.id === currentSession ? "active" : ""}" data-session="${esc(session.id)}">
      <strong>${esc(title)}</strong><br>${session.traces.length} turns; ${reviewed} with notes
      <div class="meta">${esc(session.id)}</div></button>`;
  }).join("");
}

function evidenceText(message) {
  return pretty(message.role === "tool_call" ? message.arguments : (message.text ?? message.content ?? ""));
}

function renderConversation(main) {
  const session = data.sessions.find(s => s.id === currentSession);
  if (!session) { main.textContent = "No conversations loaded."; return; }
  main.innerHTML = `<h2>Complete conversation</h2><div class="meta">${esc(session.id)}</div>
    <p>Read in order; note the first failure in your own words. Stop the initial review there, or record no failure observed. Inclusion and judgments are per trace, not per conversation.</p>
    ${session.traces.map(t => renderTrace(t)).join("")}`;
  for (const trace of session.traces) {
    const section = document.getElementById(`trace-${trace.trace_id}`);
    const margin = section.querySelector(".margin");
    const human = notes(trace.trace_id).map(a => ({...a, kind: "human"}));
    const suggested = data.suggestions.filter(s => s.trace_id === trace.trace_id && (s.status || "pending") === "pending")
      .map(s => ({...s, note: s.note || s.mode, kind: "suggestion"}));
    for (const note of [...human, ...suggested]) {
      const anchor = note.anchor || "trace";
      const body = [...section.querySelectorAll(".content")].find(e => e.dataset.anchor === anchor);
      let target = section.querySelector("h3");
      if (body) {
        const details = body.closest("details");
        if (details) details.open = true;
        target = body;
        if (note.quote) target = highlight(body, note.quote, note.id, note.kind, note.start) || body;
      }
      const box = document.createElement("div");
      box.className = `note ${note.kind === "suggestion" ? "suggestion" : ""}`;
      box.id = noteKey(note);
      box.dataset.target = target.id;
      box.innerHTML = `<div class="small">${note.kind === "suggestion" ? "AI suggestion - not a judgment" : esc(note.source || "human")}</div>
        ${note.quote ? `<q>${esc(note.quote)}</q>` : ""}<p>${esc(note.note)}</p>
        ${note.kind === "suggestion"
          ? suggestionControls(note)
          : `<div class="edit-controls"><button data-edit="${esc(note.id)}">Edit</button> <button data-delete="${esc(note.id)}" data-trace="${esc(trace.trace_id)}">Delete</button></div>`}`;
      box.addEventListener("mouseenter", () => target.classList.add("linked"));
      box.addEventListener("mouseleave", () => target.classList.remove("linked"));
      target.addEventListener("mouseenter", () => box.classList.add("linked"));
      target.addEventListener("mouseleave", () => box.classList.remove("linked"));
      margin.append(box);
    }
    section.querySelectorAll("details").forEach(d => d.addEventListener("toggle", placeNotes));
  }
  requestAnimationFrame(placeNotes);
}

function renderTrace(trace) {
  const id = trace.trace_id;
  const flag = data.graph.nodes.find(n => n.trace_id === id)?.flag;
  const blocks = [];
  for (let i = 0; i < trace.trace.length; i++) {
    const m = trace.trace[i];
    let pair = renderMessage(m, i, id);
    const next = trace.trace[i + 1];
    if (m.role === "tool_call" && next?.role === "tool_result" && next.name === m.name) {
      pair += renderMessage(next, ++i, id);
    }
    blocks.push(`<div class="step">${pair}</div>`);
  }
  const raw = [...(trace.observations || [])].sort((a, b) =>
    String(a.start_time || "").localeCompare(String(b.start_time || "")) || String(a.id).localeCompare(String(b.id)));
  return `<section class="trace" id="trace-${esc(id)}">
    <h3 id="heading-${esc(id)}">Trace ${esc(id)}</h3>
    <div class="meta">${esc(trace.timestamp || "Timestamp missing: chronology is uncertain")} | ${esc(trace.meta.role || "Role missing")} | ${esc(trace.meta.scenario_id || "Scenario missing")}</div>
    ${flag ? `<span class="badge">${esc(flag)}</span>` : ""}
    <div class="toolbar">
      ${selected(id) ? '<span class="badge">In review set</span>' : `<button data-include="${esc(id)}">Include in review set</button>`}
      <button data-note="${esc(id)}">Add trace note</button>
      <button data-clean="${esc(id)}">No failure observed</button>
      <button data-label-trace="${esc(id)}">Label this trace</button>
    </div>
    <div class="trace-grid"><article class="evidence">${blocks.join("")}
      <details><summary>Complete raw evidence: ${raw.length} observations, trace input and output</summary>
        <p>Preserved even when generation output is null. Repeated session-history wrappers are not additional turns.</p>
        <pre>${esc(pretty({input: trace.input, output: trace.output, observations: raw, metadata: trace.metadata}))}</pre></details>
      </article><aside class="margin" aria-label="Annotations beside evidence"></aside></div>
    </section>`;
}

function renderMessage(message, i, id) {
  const content = evidenceText(message);
  const structured = ["tool_call", "tool_result", "retrieval"].includes(message.role);
  const text = `<div class="content ${structured ? "json" : ""}" id="content-${esc(id)}-${i}" data-trace="${esc(id)}" data-anchor="message:${i}">${esc(content)}</div>`;
  return `<div class="message" data-role="${esc(message.role)}"><div class="role">${esc(message.role.replaceAll("_", " "))} ${esc(message.name || message.label || "")}</div>
    ${structured && content.length > 500 ? `<details><summary>${esc(message.name || "Structured evidence")} (${content.length} characters)</summary>${text}</details>` : text}
    <button class="small" data-message-note="${esc(id)}" data-anchor="message:${i}">Add note beside this step</button></div>`;
}

function highlight(body, quote, id, kind, start) {
  const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
  let node, offset = 0;
  while ((node = walker.nextNode())) {
    const index = Number.isInteger(start) ? start - offset : node.textContent.indexOf(quote);
    offset += node.textContent.length;
    if (index < 0 || node.textContent.slice(index, index + quote.length) !== quote || node.parentElement.closest("mark")) continue;
    const range = document.createRange();
    range.setStart(node, index);
    range.setEnd(node, index + quote.length);
    const mark = document.createElement("mark");
    mark.className = `highlight ${kind === "suggestion" ? "suggestion" : ""}`;
    mark.id = `highlight-${id}`;
    range.surroundContents(mark);
    return mark;
  }
  return null;
}

function placeNotes() {
  for (const margin of document.querySelectorAll(".margin")) {
    if (matchMedia("(max-width: 1100px)").matches) continue;
    let bottom = 0;
    for (const note of margin.children) {
      const target = document.getElementById(note.dataset.target);
      const y = target ? target.getBoundingClientRect().top - margin.getBoundingClientRect().top : 0;
      note.style.top = `${Math.max(bottom, y)}px`;
      bottom = Math.max(bottom, y) + note.offsetHeight + 10;
    }
    margin.style.height = `${bottom}px`;
  }
}

function openNote(trace_id, anchor = "trace", quote = "", id = null, text = "", range = null, start = null) {
  cancelNote();
  let mark = null;
  if (range) {
    mark = document.createElement("mark");
    mark.className = "pending-highlight";
    range.surroundContents(mark);
  }
  pending = {trace_id, anchor, quote, id, mark, start};
  $("#annotation").hidden = false;
  $("#annotation-text").value = text;
  $("#annotation-text").focus();
}

function cancelNote() {
  if (pending?.mark?.isConnected) pending.mark.replaceWith(...pending.mark.childNodes);
  pending = null;
  recoveryKey = null;
  $("#annotation").hidden = true;
}

function renderTaxonomy(main) {
  const cards = modes().map(m => {
    const ids = new Set(m.supporting_annotation_ids || []);
    const support = data.annotations.filter(a => !a.deleted && (ids.has(a.id) || a.mode === m.name));
    return `<section class="mode-card" style="flex-grow:${Math.max(1, support.length)}">
      <h3>${esc(m.name)} <span class="badge">${esc(m.status || "candidate")}</span></h3>
      <p>${esc(m.definition || "Definition pending")}</p>
      <p><strong>Requirement:</strong> ${esc(m.requirement_source || "Pending")}</p>
      <p><strong>Boundary:</strong> ${esc(m.boundary || "Pending")}</p>
      <p>${support.length} supporting human notes</p>
      ${support.map(a => `<p>${traceLink(a.trace_id, a.note)}</p>`).join("")}</section>`;
  }).join("");
  main.innerHTML = `<h2>Taxonomy and supporting annotations</h2>
    <p>Open coding stays free text. Candidate modes are not final labeling decisions. Link origins with <code>supporting_annotation_ids</code>; set <code>status: "final"</code> only after human review.</p>
    <div class="mode-grid">${cards || "<p>No modes yet. Define them only from reviewed human observations.</p>"}</div>
    <details><summary>Edit taxonomy JSON (explicit human save)</summary>
      <p>Keep existing fields. Each mode needs a snake_case name, definition, requirement_source, boundary, and supporting_annotation_ids. Final modes require status "final".</p>
      <textarea id="patterns-editor" class="json-editor" aria-label="Taxonomy JSON">${esc(pretty(data.patterns))}</textarea>
      <button id="save-patterns">Save taxonomy</button></details>
    <h3>Human annotation IDs</h3>${data.annotations.filter(a => !a.deleted).map(a =>
      `<p><code>${esc(a.id)}</code> ${traceLink(a.trace_id, a.note)}</p>`).join("") || "<p>No human annotations.</p>"}`;
}

function renderLabels(main) {
  const ids = new Set(data.manifest.picks.map(p => p.trace_id));
  const session = data.sessions.find(s => s.id === currentSession);
  main.innerHTML = `<h2>Explicit binary judgments</h2>
    <p>Present = 1 (failure); Absent = 0 (no failure); Unjudged has no score. Saving is an explicit human decision. Corrections preserve local history and update the same remote score.</p>
    ${!finals().length ? '<p>No final modes yet; labeling disabled.</p>' : ""}
    ${(session?.traces || []).filter(t => ids.has(t.trace_id)).map(t => `<h3>${traceLink(t.trace_id)}</h3>
      <table><thead><tr><th>Final mode</th><th>Decision</th><th>Canonical save status</th></tr></thead><tbody>
      ${finals().map(m => {
        const row = judgment(t.trace_id, m.name);
        return `<tr><td>${esc(m.name)}<div class="small">${esc(m.definition)}</div></td>
          <td><button data-label="1" data-mode="${esc(m.name)}" data-trace="${esc(t.trace_id)}" class="${row?.label === 1 ? "active" : ""}">Present</button>
          <button data-label="0" data-mode="${esc(m.name)}" data-trace="${esc(t.trace_id)}" class="${row?.label === 0 ? "active" : ""}">Absent</button>
          <div class="small">${row ? "Explicit human judgment" : "Unjudged"}</div>
          <input data-label-note="${esc(m.name)}:${esc(t.trace_id)}" aria-label="Judgment evidence" placeholder="Evidence / reasoning" value="${esc(row?.note || "")}"></td>
          <td>${esc(row?.sync_status || (row ? "Existing local label; not verified by this app" : "No score"))}
          ${row?.score_id && row.sync_status !== "confirmed" ? `<button data-retry="${esc(m.name)}" data-trace="${esc(t.trace_id)}">Retry / confirm</button>` : ""}</td></tr>`;
      }).join("")}</tbody></table>`).join("") || "<p>Include a trace from this conversation in the review set to label it.</p>"}`;
}

function suggestionControls(s) {
  return `<button data-decision="accepted" data-suggestion="${esc(s.id)}">Accept as note</button>
    <button data-decision="rejected" data-suggestion="${esc(s.id)}">Reject</button>`;
}

function renderSuggestions(main) {
  main.innerHTML = `<h2>AI hypotheses, not human judgments</h2>
    <p>Acceptance creates an attributed note only. It never silently assigns a binary label. Rejections remain saved for boundary review.</p>
    <div class="toolbar"><label><input id="suggestion-all" type="checkbox"> Select all pending</label>
      <button data-bulk="accepted">Accept selected as notes</button><button data-bulk="rejected">Reject selected</button></div>
    ${data.suggestions.map(s => `<section class="panel suggestion-row">
      ${(s.status || "pending") === "pending" ? `<input type="checkbox" data-pick-suggestion="${esc(s.id)}" aria-label="Select suggestion">` : ""}
      <strong>${esc(s.mode || "Candidate observation")}</strong> <span class="badge">${esc(s.status || "pending")}</span>
      <p>${traceLink(s.trace_id)} ${esc(s.quote || "")}</p><p>${esc(s.note || "")}</p>
      ${(s.status || "pending") === "pending" ? suggestionControls(s) : `<p>${esc(s.decision_note || "")}</p>`}
      </section>`).join("") || "<p>No AI suggestions.</p>"}`;
}

function traceLink(id, text = id) {
  return `<a href="#trace-${esc(id)}" data-go-trace="${esc(id)}">${esc(text)}</a>`;
}

function renderProgress(main) {
  const p = data.progress;
  const reviewed = new Set(data.annotations.filter(a => !a.deleted).map(a => a.trace_id));
  main.innerHTML = `<h2>Review progress</h2>
    <p><strong>${p.reviewed} / ${p.selected}</strong> distinct selected traces have open-coding notes. ${p.missing.length} incomplete final-mode judgments. ${p.pending_suggestions} pending AI suggestions. ${p.unsynced} local judgments not confirmed in Langfuse.</p>
    <p>Viewing a conversation does not count as reviewing its traces. Notes on context-only turns do not enlarge the review set.</p>
    <h3>Open coding still needed</h3>${data.manifest.picks.filter(x => !reviewed.has(x.trace_id)).map(x =>
      `<p>${traceLink(x.trace_id)} - ${esc(x.reason)}</p>`).join("") || "<p>No outstanding selected traces.</p>"}
    <h3>Incomplete judgments</h3>${!finals().length ? "<p>No final taxonomy; structured coverage has not started.</p>" : ""}
    ${p.missing.map(x => `<p>${traceLink(x.trace_id)}: ${esc(x.mode)}</p>`).join("")}
    <h3>Label synchronization</h3>${Object.values(data.labels).filter(r => selected(r.trace_id) && r.sync_status !== "confirmed").map(r =>
      `<p>${traceLink(r.trace_id)} / ${esc(r.mode)}: ${esc(r.sync_status || "Existing local label, unverified")}
      ${r.score_id ? `<button data-retry="${esc(r.mode)}" data-trace="${esc(r.trace_id)}">Retry / confirm</button>` : ""}</p>`).join("")}`;
}

function hull(points) {
  const sorted = [...points].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const half = list => {
    const result = [];
    for (const p of list) {
      while (result.length > 1 && cross(result.at(-2), result.at(-1), p) <= 0) result.pop();
      result.push(p);
    }
    return result;
  };
  return [...half(sorted).slice(0, -1), ...half(sorted.reverse()).slice(0, -1)];
}

function renderMap(main) {
  const nodes = data.graph.nodes;
  const extent = axis => Math.max(1, ...nodes.map(n => Math.abs(n[axis])));
  const xx = extent("x"), yy = extent("y");
  const xy = n => [450 + n.x / xx * 410, 280 + n.y / yy * 240];
  const colors = ["#2563eb", "#047857", "#9333ea", "#b45309", "#0891b2", "#be123c", "#4338ca", "#374151"];
  const polygons = [...new Set(nodes.map(n => n.cluster))].map(c => {
    const polygon = hull(nodes.filter(n => n.cluster === c).map(xy));
    return `<polygon points="${polygon.map(p => p.join(",")).join(" ")}" fill="${colors[c]}" opacity=".08"/>`;
  }).join("");
  const points = nodes.map(n => {
    const [x, y] = xy(n), t = traces().find(t => t.trace_id === n.trace_id);
    const chosen = selected(n.trace_id), r = chosen ? 7 : 4;
    const color = notes(n.trace_id).length ? "#ea580c" : colors[n.cluster];
    const attrs = `fill="${color}" stroke="${chosen ? "#111827" : "white"}" stroke-width="${chosen ? 2 : 1}"`;
    const shape = t.meta.role === "merchant" ? `<rect x="${x-r}" y="${y-r}" width="${2*r}" height="${2*r}" ${attrs}/>`
      : t.meta.role === "support" ? `<polygon points="${x},${y-r} ${x-r},${y+r} ${x+r},${y+r}" ${attrs}/>`
        : `<circle cx="${x}" cy="${y}" r="${r}" ${attrs}/>`;
    const title = `${n.trace_id}; ${t.meta.role || "unknown role"}; ${t.meta.scenario_id || ""}; cluster ${n.cluster}; ${notes(n.trace_id).length} notes`;
    return `<g ${chosen ? `data-go-trace="${esc(n.trace_id)}" tabindex="0" role="link"` : ""}><title>${esc(title)}</title>${shape}</g>`;
  }).join("");
  main.innerHTML = `<h2>Structural diversity map</h2><p>PCA of standardized trace features; hue = cluster, orange = annotated, dark outline/larger = review set. Shopper circles, merchant squares, support triangles. Click selected points to review. This is not a failure prediction or prevalence estimate.</p>
    <svg class="graph" viewBox="0 0 900 560" aria-label="Trace structural diversity map">${polygons}${points}</svg>`;
}

function goTrace(id, targetView = "conversation") {
  const session = data.sessions.find(s => s.traces.some(t => t.trace_id === id));
  if (!session) throw new Error("The source trace is not in this dataset.");
  cancelNote();
  currentSession = session.id;
  view = targetView;
  render();
  document.getElementById(`trace-${id}`)?.scrollIntoView({block: "start"});
}

document.addEventListener("click", async event => {
  const button = event.target.closest("button, [data-go-trace]");
  if (!button) return;
  try {
    if (button.dataset.view) { cancelNote(); view = button.dataset.view; render(); }
    if (button.dataset.session) { cancelNote(); currentSession = button.dataset.session; render(); }
    if (button.id === "refresh") await refresh();
    if (button.dataset.goTrace) { event.preventDefault(); goTrace(button.dataset.goTrace); }
    if (button.dataset.labelTrace) goTrace(button.dataset.labelTrace, "labels");
    if (button.dataset.include) {
      const reason = prompt("Why is this trace entering the review set? Record the selection method / batch.");
      if (reason) await act({action: "select", trace_id: button.dataset.include, reason});
    }
    if (button.dataset.note) openNote(button.dataset.note);
    if (button.dataset.messageNote) openNote(button.dataset.messageNote, button.dataset.anchor);
    if (button.dataset.clean) await act({action: "note", trace_id: button.dataset.clean, note: "no failure observed"});
    if (button.id === "cancel-note") cancelNote();
    if (button.dataset.edit) {
      const a = data.annotations.find(a => a.id === button.dataset.edit);
      openNote(a.trace_id, a.anchor, a.quote, a.id, a.note, null, a.start);
    }
    if (button.dataset.delete && confirm("Delete this annotation? It will remain marked deleted in the state file.")) {
      await act({action: "delete_note", trace_id: button.dataset.trace, id: button.dataset.delete});
    }
    if (button.id === "save-patterns") await act({action: "patterns", patterns: JSON.parse($("#patterns-editor").value)});
    if (button.dataset.label !== undefined) {
      const input = [...document.querySelectorAll("[data-label-note]")].find(e =>
        e.dataset.labelNote === `${button.dataset.mode}:${button.dataset.trace}`);
      await act({action: "label", trace_id: button.dataset.trace, mode: button.dataset.mode,
        label: Number(button.dataset.label), note: input.value});
    }
    if (button.dataset.retry) await act({action: "retry", trace_id: button.dataset.trace, mode: button.dataset.retry});
    if (button.dataset.decision) {
      const note = prompt("Optional reason for this decision (especially a rejected boundary):", "");
      if (note !== null) await act({action: "suggestion", id: button.dataset.suggestion, decision: button.dataset.decision, note});
    }
    if (button.dataset.bulk) {
      const ids = [...document.querySelectorAll("[data-pick-suggestion]:checked")].map(e => e.dataset.pickSuggestion);
      for (const id of ids) await act({action: "suggestion", id, decision: button.dataset.bulk});
    }
  } catch (error) { fail(error); }
});

$("#search").addEventListener("input", renderNavigator);
document.addEventListener("change", event => {
  if (event.target.id === "suggestion-all") document.querySelectorAll("[data-pick-suggestion]").forEach(e => { e.checked = event.target.checked; });
});
$("#annotation").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    const {mark, ...payload} = pending;
    await act({action: "note", ...payload, note: $("#annotation-text").value});
    cancelNote();
  } catch (error) { fail(error); }
});
$("#annotation-text").addEventListener("input", () => {
  if (pending) {
    const {mark, ...payload} = pending;
    localStorage.setItem(backupKey(), JSON.stringify({revision: data.revision,
      payload: {action: "note", ...payload, note: $("#annotation-text").value}}));
  }
});
document.addEventListener("mouseup", event => {
  if (pending || event.target.closest("#annotation")) return;
  const selection = getSelection();
  if (!selection || selection.isCollapsed || !selection.rangeCount) return;
  const range = selection.getRangeAt(0);
  const node = range.commonAncestorContainer;
  const body = (node.nodeType === Node.TEXT_NODE ? node.parentElement : node).closest(".content");
  if (!body || !body.contains(range.startContainer) || !body.contains(range.endContainer)) return;
  // Only surround a single text node; existing overlapping highlights remain
  // available through step-level notes rather than corrupting their anchors.
  if (range.startContainer !== range.endContainer || range.startContainer.parentElement.closest("mark")) return;
  const quote = selection.toString();
  const prefix = document.createRange();
  prefix.selectNodeContents(body);
  prefix.setEnd(range.startContainer, range.startOffset);
  if (quote.trim()) openNote(body.dataset.trace, body.dataset.anchor, quote, null, "", range, prefix.toString().length);
});
document.addEventListener("mousedown", event => {
  if (pending && !event.target.closest("#annotation") && !event.target.closest(".pending-highlight")) cancelNote();
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape") cancelNote();
  if (event.key === "Enter" && event.target.matches("g[data-go-trace]")) goTrace(event.target.dataset.goTrace);
});
window.addEventListener("resize", placeNotes);
window.addEventListener("beforeunload", event => {
  if (pending || busy) { event.preventDefault(); event.returnValue = ""; }
});

refresh().then(() => {
  let key = backupKey();
  let backup = localStorage.getItem(key);
  if (!backup && data.legacy_identity) {
    key = `cartwheel-review:${data.legacy_identity}`;
    backup = localStorage.getItem(key);
  }
  if (!backup) return;
  const draft = JSON.parse(backup);
  if (draft.payload.action === "note" && !traces().some(t => t.trace_id === draft.payload.trace_id)) {
    fail(new Error("A browser draft belongs to a trace outside this export. It remains saved; use --live-traces or its original export to recover it."));
    return;
  }
  if (draft.payload.action === "note" && confirm("A browser note backup exists. Recover it for review (not automatically saved)?")) {
    const p = draft.payload;
    goTrace(p.trace_id);
    openNote(p.trace_id, p.anchor, p.quote, p.id, p.note, null, p.start);
    recoveryKey = key;
  } else if (draft.revision !== data.revision) {
    fail(new Error("An older browser backup exists. It has not been replayed over newer server state."));
  }
}).catch(fail);
setInterval(async () => {
  if (!data || busy || document.hidden) return;
  try {
    const response = await fetch("/api/revision");
    const next = await response.json();
    if (!response.ok) throw new Error(next.error || "Could not check for review updates.");
    if (next.revision !== data.revision) $("#updates").hidden = false;
  } catch (error) { fail(error); }
}, 15000);
