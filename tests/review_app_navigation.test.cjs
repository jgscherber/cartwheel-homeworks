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
