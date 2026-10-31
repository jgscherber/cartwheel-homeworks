const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "analysis", "review_app", "app.js"), "utf8");

function app() {
  const elements = new Map(), events = new Map(), moves = [], saves = [];
  function element(selector) {
    if (!elements.has(selector)) elements.set(selector, {addEventListener() {}, value: "", hidden: true});
    return elements.get(selector);
  }
  const context = vm.createContext({
    document: {
      querySelector: element, querySelectorAll: () => [],
      addEventListener: (name, handler) => events.set(name, handler),
      getElementById: id => ({
        focus: () => moves.push(id), scrollIntoView() {},
      }),
    },
    window: {addEventListener() {}}, localStorage: {getItem() { return null; }},
    fetch: () => new Promise(() => {}), setInterval() {}, saves,
  });
  vm.runInContext(source, context);
  vm.runInContext(`
    data = {
      sessions: [
        {id: "first", traces: [
          {trace_id: "a", meta: {role: "shopper"}, metadata: {"cartwheel.user_id": "17"}, trace: [{role: "user", text: "Question"}]},
          {trace_id: "b", meta: {role: "shopper"}, trace: [{role: "assistant", text: "Answer"}]}
        ]},
        {id: "second", traces: [
          {trace_id: "c", meta: {role: "support"}, trace: [{role: "user", text: "Another question"}]}
        ]}
      ],
      manifest: {picks: []}, annotations: [], graph: {nodes: []}
    };
    currentSession = "first";
    render = () => {};
    act = async payload => { saves.push(payload); return {ok: true}; };
  `, context);
  return {
    run: code => vm.runInContext(code, context),
    click: dataset => events.get("click")({target: {closest: () => ({dataset})}}),
    change: target => events.get("change")({target}),
    elements, moves, saves,
  };
}

test("loading randomizes conversations without splitting turns, and Next follows the displayed order", async () => {
  const ui = app();
  ui.run(`
    const snapshot = JSON.stringify(data);
    data = undefined;
    Math.random = () => 0;
    fetch = async () => ({ok: true, json: async () => JSON.parse(snapshot)});
  `);
  await ui.run("getData().then(result => { data = result; })");
  assert.equal(ui.run("data.sessions.map(s => s.id).join(',')"), "second,first");
  assert.equal(ui.run("traces().map(t => t.trace_id).join(',')"), "c,a,b");
  ui.run("renderNavigator()");
  const sidebar = ui.elements.get("#sessions").innerHTML;
  assert(sidebar.indexOf('data-session="second"') < sidebar.indexOf('data-session="first"'));
  await ui.click({nextTrace: "c"});
  assert.equal(ui.moves.at(-1), "trace-a");
  await ui.click({nextTrace: "a"});
  assert.equal(ui.moves.at(-1), "trace-b");
  assert.equal(ui.run("nextTrace('b')"), null);
});

test("state reloads keep the randomized order and append new conversations without restoring removed ones", async () => {
  const ui = app();
  ui.run(`
    data.sessions.reverse();
    const snapshot = JSON.parse(JSON.stringify(data));
    snapshot.sessions.reverse();
    snapshot.sessions.push({id: "third", traces: []}, {id: "fourth", traces: []});
    Math.random = () => 0;
    fetch = async () => ({ok: true, json: async () => JSON.parse(JSON.stringify(snapshot))});
  `);
  await ui.run("getData().then(result => { data = result; })");
  assert.equal(ui.run("data.sessions.map(s => s.id).join(',')"), "second,first,fourth,third");
  await ui.run("getData().then(result => { data = result; })");
  assert.equal(ui.run("data.sessions.map(s => s.id).join(',')"), "second,first,fourth,third");
  ui.run(`snapshot.sessions = snapshot.sessions.filter(s => s.id !== "second")`);
  await ui.run("getData().then(result => { data = result; })");
  assert.equal(ui.run("data.sessions.map(s => s.id).join(',')"), "first,fourth,third");
});

test("review actions follow all evidence, and role/identity appear before trace metadata", () => {
  const ui = app();
  const html = ui.run("renderTrace(data.sessions[0].traces[0])");
  const footer = html.indexOf('<footer class="toolbar trace-actions"');
  assert(footer > html.indexOf("Complete raw evidence:"));
  for (const action of ["data-note=", "data-clean=", "data-next-trace="]) {
    assert(html.indexOf(action) > footer, `${action} belongs in the footer`);
  }
  assert(html.indexOf("User 17") < html.indexOf('<h3'));
  assert(html.includes('class="trace-role">shopper</span>'));
  assert(!html.includes("Last trace in dataset"));
});

test("missing identity is explicit and recorded identity is HTML-escaped", () => {
  const ui = app();
  assert(ui.run("renderTrace(data.sessions[0].traces[1])").includes("Identity not recorded"));
  ui.run('data.sessions[0].traces[0].metadata["cartwheel.user_id"] = "<img onerror=alert(1)>";');
  const html = ui.run("renderTrace(data.sessions[0].traces[0])");
  assert(html.includes("User &lt;img onerror=alert(1)&gt;"));
  assert(!html.includes("<img"));
});

test("Next trace advances within a conversation, then to the following conversation without saving", async () => {
  const ui = app();
  await ui.click({nextTrace: "a"});
  assert.equal(ui.run("currentSession"), "first");
  assert.equal(ui.moves.at(-1), "trace-b");
  await ui.click({nextTrace: "b"});
  assert.equal(ui.run("currentSession"), "second");
  assert.equal(ui.moves.at(-1), "trace-c");
  assert.equal(ui.saves.length, 0);
});

test("the final trace disables Next and does not wrap after a clean save", async () => {
  const ui = app();
  const html = ui.run("renderTrace(data.sessions[1].traces[0])");
  assert(html.includes('data-next-trace="c" disabled'));
  assert(html.includes("Last trace in dataset"));
  ui.run('currentSession = "second"');
  await ui.click({clean: "c"});
  assert.equal(ui.run("currentSession"), "second");
  assert.equal(ui.moves.length, 0);
  assert.equal(ui.saves.length, 1);
  assert.equal(ui.saves[0].note, "no failure observed");
});

test("No failure observed waits for a successful save before advancing, without selecting the trace", async () => {
  const ui = app();
  ui.run(`
    let finishSave;
    act = async payload => {
      saves.push(payload); busy = true;
      await new Promise(resolve => { finishSave = () => { busy = false; resolve(); }; });
    };
  `);
  const saving = ui.click({clean: "b"});
  assert.equal(ui.moves.length, 0);
  assert.equal(ui.run("currentSession"), "first");
  ui.run("finishSave()");
  await saving;
  assert.equal(ui.moves.at(-1), "trace-c");
  assert.equal(ui.run("currentSession"), "second");
  assert.equal(ui.saves.length, 1);
  assert.equal(ui.saves[0].action, "note");
  assert.equal(ui.saves[0].trace_id, "b");
  assert.equal(ui.run("data.manifest.picks.length"), 0);
});

test("a failed clean save leaves the reviewer on the current trace and shows the error", async () => {
  const ui = app();
  ui.run('act = async () => { throw new Error("Could not save annotation"); }');
  await ui.click({clean: "a"});
  assert.equal(ui.moves.length, 0);
  assert.equal(ui.run("currentSession"), "first");
  assert.equal(ui.elements.get("#error").hidden, false);
  assert.equal(ui.elements.get("#error").textContent, "Could not save annotation");
});

test("Next does not move while a save is in progress", async () => {
  const ui = app();
  ui.run("busy = true");
  await ui.click({nextTrace: "a"});
  assert.equal(ui.moves.length, 0);
  assert(ui.elements.get("#error").textContent.includes("Wait for the current save"));
});

test("a completed save does not undo a later manual conversation selection", async () => {
  const ui = app();
  ui.run(`
    let finishSave;
    act = () => new Promise(resolve => { finishSave = resolve; });
  `);
  const saving = ui.click({clean: "a"});
  ui.run('currentSession = "second"; finishSave()');
  await saving;
  assert.equal(ui.run("currentSession"), "second");
  assert.equal(ui.moves.length, 0);
});

test("the sidebar filter hides fully annotated conversations and targets the first remaining turn", () => {
  const ui = app();
  ui.run('data.annotations = [{trace_id: "a"}, {trace_id: "c"}]');
  ui.change({id: "hide-noted", checked: true});
  ui.run("renderNavigator()");
  const html = ui.elements.get("#sessions").innerHTML;
  assert(html.includes('data-session="first" data-start-trace="b"'));
  assert(!html.includes('data-session="second"'));
  assert.equal(ui.run("data.sessions[0].traces.length"), 2);
  assert.equal(ui.saves.length, 0);
  ui.change({id: "hide-noted", checked: false});
  ui.run("renderNavigator()");
  assert(ui.elements.get("#sessions").innerHTML.includes('data-session="second"'));
});

test("filtered Next skips annotated turns and can advance from a just-annotated trace", async () => {
  const ui = app();
  ui.run('hideNoted = true; data.annotations = [{trace_id: "a"}, {trace_id: "b"}]');
  await ui.click({nextTrace: "a"});
  assert.equal(ui.moves.at(-1), "trace-c");
  assert.equal(ui.saves.length, 0);
  ui.run('data.annotations.push({trace_id: "c"})');
  const html = ui.run("renderTrace(data.sessions[0].traces[0])");
  assert(html.includes('data-next-trace="a" disabled'));
  assert(html.includes("No more traces without notes"));
  ui.run("renderNavigator()");
  assert(ui.elements.get("#sessions").innerHTML.includes("No conversations match this filter"));
});

test("deleted notes do not hide a trace, and a filtered sidebar click opens the remaining turn", async () => {
  const ui = app();
  ui.run('hideNoted = true; data.annotations = [{trace_id: "a"}, {trace_id: "b", deleted: true}]');
  ui.run("renderNavigator()");
  assert(ui.elements.get("#sessions").innerHTML.includes('data-start-trace="b"'));
  await ui.click({session: "first", startTrace: "b"});
  assert.equal(ui.moves.at(-1), "trace-b");
});

test("annotation count tracks distinct traces with active notes, independently of navigation filters", () => {
  const ui = app();
  ui.run("renderNavigator()");
  assert.equal(ui.elements.get("#annotation-count").textContent, "0 of 3 traces annotated");
  ui.run(`
    data.annotations = [
      {trace_id: "a", note: "First observation"},
      {trace_id: "a", note: "Another observation"},
      {trace_id: "b", note: "no failure observed"},
      {trace_id: "c", note: "Removed observation", deleted: true},
      {trace_id: "outside-dataset", note: "Not in this export"}
    ];
    hideNoted = true;
    document.querySelector("#search").value = "no matching conversation";
    renderNavigator();
  `);
  assert.equal(ui.elements.get("#annotation-count").textContent, "2 of 3 traces annotated");
  ui.run('data.annotations.forEach(a => { a.deleted = true; }); renderNavigator();');
  assert.equal(ui.elements.get("#annotation-count").textContent, "0 of 3 traces annotated");
});

test("sampling exclusions are reversible and remain separate from existing membership and notes", async () => {
  const ui = app();
  assert(ui.run("renderTrace(data.sessions[0].traces[0])").includes("Don't include in the review set"));
  await ui.click({excludeTrace: "a", excluded: "true"});
  assert.equal(ui.saves.at(-1).action, "exclude");
  assert.equal(ui.saves.at(-1).trace_id, "a");
  assert.equal(ui.saves.at(-1).excluded, true);
  ui.run(`
    data.excluded_trace_ids = ["a"];
    data.manifest.picks = [{trace_id: "a"}];
  `);
  const html = ui.run("renderTrace(data.sessions[0].traces[0])");
  assert(html.includes("In review set"));
  assert(html.includes("Excluded from automatic picks"));
  assert(html.includes('data-excluded="false"'));
  assert(html.includes("Allow future automatic picks"));
  await ui.click({excludeTrace: "a", excluded: "false"});
  assert.equal(ui.saves.at(-1).excluded, false);
  assert.equal(ui.saves.length, 2);
  assert.equal(ui.run("selected('a')"), true);
  assert.equal(ui.run("data.annotations.length"), 0);
});

test("line-edge selections retain text when an endpoint is the surrounding element", () => {
  const ui = app();
  const result = ui.run(`
    globalThis.Node = {TEXT_NODE: 3};
    globalThis.NodeFilter = {SHOW_TEXT: 4};
    const body = {};
    const first = {textContent: "First\\n"};
    const last = {textContent: "Second"};
    const nodes = [first, last];
    document.createTreeWalker = () => {
      let index = 0;
      return {nextNode: () => nodes[index++] || null};
    };
    const summarize = range => textSegments(range).map(s => ({
      text: s.node.textContent.slice(s.start, s.end), start: s.start, end: s.end
    }));
    JSON.stringify([
      summarize({commonAncestorContainer: body, startContainer: first, startOffset: 2,
        endContainer: body, endOffset: 2, intersectsNode: () => true}),
      summarize({commonAncestorContainer: body, startContainer: body, startOffset: 0,
        endContainer: last, endOffset: 3, intersectsNode: () => true})
    ]);
  `);
  assert.deepEqual(JSON.parse(result), [
    [{text: "rst\n", start: 2, end: 6}, {text: "Second", start: 0, end: 6}],
    [{text: "First\n", start: 0, end: 6}, {text: "Sec", start: 0, end: 3}],
  ]);
});

test("treemap rectangles fill the container without overlap and match their exact weights", () => {
  const ui = app();
  const rectangles = JSON.parse(ui.run(`JSON.stringify(treemapRectangles([
    {name: "a", weight: 3}, {name: "b", weight: 2}, {name: "c", weight: 1}
  ]))`));
  assert(Math.abs(rectangles.reduce((sum, r) => sum + r.width * r.height, 0) - 10000) < 1e-8);
  for (const r of rectangles) {
    assert(Math.abs(r.width * r.height - 10000 * r.weight / 6) < 1e-8);
    assert(r.x >= 0 && r.y >= 0 && r.x + r.width <= 100 + 1e-8 && r.y + r.height <= 100 + 1e-8);
  }
  for (let i = 0; i < rectangles.length; i++) {
    for (const b of rectangles.slice(i + 1)) {
      const a = rectangles[i];
      assert(a.x + a.width <= b.x + 1e-8 || b.x + b.width <= a.x + 1e-8
        || a.y + a.height <= b.y + 1e-8 || b.y + b.height <= a.y + 1e-8);
    }
  }
});

test("treemap support counts only distinct accepted current-definition positives and excludes skipped traces", () => {
  const ui = app();
  ui.run(`
    data.annotations = [
      {id: "n1", trace_id: "a", note: "Observation"}, {id: "n2", trace_id: "a", note: "Another observation"},
      {id: "n3", trace_id: "b", note: "Boundary example"}
    ];
    const proposal = {name: "candidate", definition: "Rule", examples: [
      {id: "p1", trace_id: "a", annotation_id: "n1", kind: "positive", status: "pending"},
      {id: "p2", trace_id: "a", annotation_id: "n2", kind: "positive", status: "pending"},
      {id: "n", trace_id: "b", annotation_id: "n3", kind: "close_negative", status: "accepted", reviewed_definition: "Rule"}
    ]};
  `);
  assert.equal(ui.run("taxonomyCounts(proposal).positive"), 0);
  ui.run('proposal.examples.slice(0, 2).forEach(e => { e.status = "accepted"; e.reviewed_definition = "Rule"; })');
  assert.equal(ui.run("taxonomyCounts(proposal).positive"), 1);
  assert.equal(ui.run("taxonomyCounts(proposal).negative"), 1);
  ui.run('data.excluded_trace_ids = ["a"]');
  assert.equal(ui.run("taxonomyCounts(proposal).positive"), 0);
  assert.equal(ui.run("taxonomyExamples(proposal).length"), 1);
  ui.run('data.excluded_trace_ids = []; proposal.definition = "Revised rule"');
  assert.equal(ui.run("taxonomyCounts(proposal).positive"), 0);
  assert.equal(ui.run("taxonomyCounts(proposal).negative"), 0);
});

test("taxonomy review controls send one explicit decision without creating notes or binary labels", async () => {
  const ui = app();
  await ui.click({taxonomyExample: "p1", mode: "candidate", decision: "accepted"});
  assert.equal(ui.saves.length, 1);
  assert.equal(ui.saves[0].action, "taxonomy_example");
  await ui.click({taxonomyMode: "candidate", decision: "accepted"});
  assert.equal(ui.saves.length, 2);
  assert.equal(ui.saves[1].action, "taxonomy_mode");
  assert.equal(ui.run("data.annotations.length"), 0);
  assert.equal(ui.run("data.manifest.picks.length"), 0);
});

test("selection ending at the start of another text node excludes its empty fragment", () => {
  const ui = app();
  const result = ui.run(`
    globalThis.Node = {TEXT_NODE: 3};
    globalThis.NodeFilter = {SHOW_TEXT: 4};
    const first = {textContent: "Selected line"};
    const next = {textContent: "Not selected"};
    const nodes = [first, next];
    document.createTreeWalker = () => {
      let index = 0;
      return {nextNode: () => nodes[index++] || null};
    };
    JSON.stringify(textSegments({
      commonAncestorContainer: {}, startContainer: first, startOffset: 0,
      endContainer: next, endOffset: 0, intersectsNode: () => true
    }).map(s => s.node.textContent.slice(s.start, s.end)));
  `);
  assert.deepEqual(JSON.parse(result), ["Selected line"]);
});
