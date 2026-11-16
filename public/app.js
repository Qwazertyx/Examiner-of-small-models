"use strict";

const $ = (id) => document.getElementById(id);
const SVG = "http://www.w3.org/2000/svg";
const POLL = 6000; // ms between checks while waiting for the phone

let game;      // public case data from the server
let suspect;   // suspect currently being questioned
let state;     // { clues: [ids], asked: n }, sent back with every question
let leads;     // subjects each suspect can be asked about, given the clues held
let plan;      // floor plan: rooms, and the links known so far
const clues = new Map(); // in the order found; the position gives the exhibit letter

let busy = false;     // a question is on its way
let waiting = false;  // the phone that plays the suspects is not connected
let over = false;     // an accusation was made
let remaining = 0;
let pollTimer;

// Text from the server and the model is only ever inserted with textContent.
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function svg(tag, attrs, text) {
  const node = document.createElementNS(SVG, tag);
  for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, value);
  if (text) node.textContent = text;
  return node;
}

const letter = (id) => String.fromCharCode(65 + [...clues.keys()].indexOf(id));
const room = (id) => plan.rooms.find((r) => r.id === id);

async function api(body, query = "") {
  let res, data;
  try {
    res = await fetch("/api/game" + query, body && {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    data = await res.json();
  } catch {
    throw new Error("Could not reach the game server. Check your connection and ask again.");
  }
  if (!res.ok) {
    const error = new Error(data.error || "The game server refused the request.");
    error.status = res.status;
    throw error;
  }
  return data;
}

// The one place that decides whether the player can ask.
function refresh() {
  const locked = busy || waiting || over || remaining === 0;
  for (const field of $("ask").elements) field.disabled = locked;
  $("waiting").hidden = !waiting || over;
}

async function checkPhone() {
  clearTimeout(pollTimer);
  if (!document.hidden) {
    try {
      waiting = (await api(null, "?status")).phone === "waiting";
    } catch {
      waiting = true;
    }
    refresh();
  }
  if (waiting && !over) pollTimer = setTimeout(checkPhone, POLL);
}

function drawFace(rows) {
  const face = svg("svg", { viewBox: `0 0 ${rows[0].length} ${rows.length}`, class: "face", "aria-hidden": "true" });
  rows.forEach((row, y) => [...row].forEach((dot, x) => {
    if (dot === "#") face.append(svg("rect", { x, y, width: 1, height: 1 }));
  }));
  return face;
}

const W = 100, H = 56, GAP_X = 22, GAP_Y = 30;
const corner = (r) => ({ x: r.col * (W + GAP_X), y: r.row * (H + GAP_Y) });

function drawMap() {
  const map = $("map");
  const cols = Math.max(...plan.rooms.map((r) => r.col)) + 1;
  const rows = Math.max(...plan.rooms.map((r) => r.row)) + 1;
  map.setAttribute("viewBox", `-4 -4 ${cols * (W + GAP_X) - GAP_X + 8} ${rows * (H + GAP_Y) - GAP_Y + 8}`);
  map.replaceChildren();
  // Links first: the rooms drawn over them leave only the part that crosses the corridors.
  for (const link of plan.links) {
    const a = corner(room(link.a)), b = corner(room(link.b));
    map.append(svg("line", {
      x1: a.x + W / 2, y1: a.y + H / 2, x2: b.x + W / 2, y2: b.y + H / 2, class: "link link-" + link.kind,
    }));
  }
  for (const r of plan.rooms) {
    const { x, y } = corner(r);
    map.append(svg("rect", { x, y, width: W, height: H, class: suspect.place === r.id ? "room here" : "room" }));
    map.append(svg("text", { x: x + W / 2, y: y + 16, class: "room-name" }, r.name));
    const occupant = game.suspects.find((s) => s.place === r.id);
    if (occupant) map.append(svg("text", { x: x + W / 2, y: y + 29, class: "occupant" }, occupant.name));
    let n = 0;
    for (const clue of clues.values()) {
      if (clue.place !== r.id) continue;
      const cx = x + 12 + 17 * n++, cy = y + H - 11;
      map.append(svg("circle", { cx, cy, r: 7, class: "pin" }),
        svg("text", { x: cx, y: cy + 3, class: "pin-letter" }, letter(clue.id)));
    }
  }
}

function drawLeads() {
  $("leads").replaceChildren(...leads[suspect.id].map((lead) => {
    const button = el("button", lead.label, "lead");
    button.type = "button";
    button.addEventListener("click", () => {
      $("question").value = lead.sample;
      $("question").focus();
    });
    return button;
  }));
  refresh();
}

function record(className, label, text) {
  $("log-empty").hidden = true;
  const line = el("li", "", className);
  line.append(el("b", label + " "), el("span", text));
  $("log").append(line);
  $("log").scrollTop = $("log").scrollHeight;
  return line;
}

function addClue(clue) {
  clues.set(clue.id, clue);
  const exhibit = "Exhibit " + letter(clue.id);
  const item = el("li");
  item.append(el("b", exhibit + ". " + clue.name + ". "), el("span", clue.text));
  if (clue.place) item.append(" ", el("small", "Found: " + room(clue.place).name + "."));
  $("clues").append(item);
  $("show").append(new Option(exhibit + ", " + clue.name, clue.id));
  return exhibit;
}

function pick(next) {
  suspect = next;
  for (const button of $("suspects").children) {
    button.setAttribute("aria-pressed", String(button.dataset.id === suspect.id));
  }
  $("accuse").textContent = "Accuse " + suspect.name;
  $("question").placeholder = "Ask " + suspect.name + " something";
  drawLeads();
  drawMap();
}

function showRemaining(left) {
  remaining = left;
  $("status").textContent = left === 0 ? "No questions left. Accuse someone."
    : left === 1 ? "1 question left." : left + " questions left.";
}

$("ask").addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = $("question").value.trim();
  if (!question || busy || waiting || over || remaining === 0) return;
  const asked = suspect;
  const shown = $("show").value;
  busy = true;
  refresh();
  $("status").textContent = asked.name + " is thinking. The phone can take a few seconds.";
  try {
    const turn = await api({ action: "ask", suspect: asked.id, question, show: shown || null, state });
    state = turn.state;
    leads = turn.leads;
    plan = turn.map;
    record("q", "Q, to " + asked.name + (shown ? ", showing exhibit " + letter(shown) : "") + ".", question);
    const answer = record("a", "A.", turn.reply);
    if (turn.cracked) answer.append(" ", el("span", "Statement changed", "stamp new"));
    answer.append(el("small", turn.source === "phone" ? "Answered by the phone." : "Scripted answer."));
    if (turn.note) {
      $("notes-empty").hidden = true;
      $("notes").append(el("li", asked.name + ": " + turn.note));
    }
    if (turn.new_clue) {
      const exhibit = addClue(turn.new_clue);
      record("found", exhibit + " added.", turn.new_clue.name + ". " + turn.new_clue.text);
    }
    $("question").value = "";
    $("show").value = "";
    busy = false;
    showRemaining(turn.remaining);
    drawLeads();
    drawMap();
    if (turn.remaining > 0) $("question").focus();
  } catch (error) {
    busy = false;
    $("status").textContent = error.message;
    // 503: the phone went silent. The question stays in the box for when it is back.
    if (error.status === 503) await checkPhone();
    refresh();
  }
});

$("accuse").addEventListener("click", async () => {
  if (!confirm("Accuse " + suspect.name + "? This ends the case.")) return;
  try {
    const verdict = await api({ action: "accuse", suspect: suspect.id, state });
    if (verdict.proven) {
      $("stamp").textContent = "Case closed";
      $("verdict-text").textContent = "The board has its proof. " + verdict.solution;
    } else if (verdict.correct) {
      $("stamp").textContent = "No proof";
      $("verdict-text").textContent = "You named the right one, but the board will not act on a hunch. "
        + verdict.solution;
    } else {
      $("stamp").textContent = "Wrong suspect";
      $("verdict-text").textContent = "It was " + verdict.culprit + ". " + verdict.solution;
    }
    over = true;
    clearTimeout(pollTimer);
    refresh();
    $("accuse").disabled = true;
    $("verdict").hidden = false;
    $("again").focus();
  } catch (error) {
    $("status").textContent = error.message;
  }
});

$("again").addEventListener("click", () => location.reload());

// A page left in a background tab stops pinging; it catches up when it is shown again.
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && waiting && !over) checkPhone();
});

(async () => {
  try {
    game = await api();
  } catch (error) {
    $("title").textContent = "The case could not be opened";
    $("intro").textContent = error.message;
    refresh();
    $("accuse").disabled = true;
    return;
  }
  leads = game.leads;
  plan = game.map;
  document.title = game.title;
  $("title").textContent = game.title;
  $("intro").textContent = game.intro;
  $("map-legend").textContent = plan.legend || "";
  $("show").append(new Option("Nothing", ""));
  state = { clues: [], asked: 0 };
  for (const clue of game.clues) {
    addClue(clue);
    state.clues.push(clue.id);
  }
  for (const s of game.suspects) {
    const button = el("button", "", "suspect");
    button.type = "button";
    button.dataset.id = s.id;
    if (s.face) button.append(drawFace(s.face));
    button.append(el("b", s.name), el("span", s.role));
    button.addEventListener("click", () => pick(s));
    $("suspects").append(button);
  }
  showRemaining(game.max_questions);
  pick(game.suspects[0]);
  checkPhone();
})();
