"use strict";

const $ = (id) => document.getElementById(id);
const SVG = "http://www.w3.org/2000/svg";
const PAIR_POLL = 2500;  // ms between phone checks while pairing
const GAME_POLL = 10000; // ms between phone checks during the game
const PEEK_EVERY = 800;  // ms between looks at the reply the phone is typing

let game;      // public case data from the server
let suspect;   // suspect currently being questioned
let state;     // { clues: [ids], asked: n }, sent back with every question
let leads;     // subjects each suspect can be asked about, given the clues held
let plan;      // floor plan: rooms, and the links known so far
const clues = new Map(); // in the order found; the position gives the exhibit letter

let mode = null;  // "scripted" or "phone", chosen on the start screen
let code = null;  // pairing code shared with the lending phone
let busy = false; // a question is on its way
let over = false; // an accusation was made
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

async function api(body) {
  let res, data;
  try {
    res = await fetch("/api/game", body && {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    data = await res.json();
  } catch {
    throw new Error("Could not reach the game server. Check your connection and try again.");
  }
  if (!res.ok) throw new Error(data.error || "The game server refused the request.");
  return data;
}

// ---- Start screen and the lending phone ----

const WHY = {
  asleep: "Written line: your phone is not connected. Keep its page open with the screen on.",
  slow: "Written line: your phone took too long to answer.",
  busy: "Written line: your phone was still on the previous question.",
  store: "Written line: the link to your phone is down.",
};

const PHONE_STATES = {
  waiting: "Waiting for your phone.",
  loading: "Your phone is downloading the model.",
  ready: "Your phone is ready.",
  asleep: "Your phone stopped answering. Open the phone page again and keep its screen on.",
  failed: "Your phone cannot run the model.",
};

function drawQr(text) {
  const qr = qrcode(0, "M");
  qr.addData(text);
  qr.make();
  const n = qr.getModuleCount();
  const box = $("qr");
  box.setAttribute("viewBox", `-4 -4 ${n + 8} ${n + 8}`);
  box.replaceChildren(svg("rect", { x: -4, y: -4, width: n + 8, height: n + 8, class: "qr-paper" }));
  for (let r = 0; r < n; r++) {
    for (let c = 0; c < n; c++) {
      if (qr.isDark(r, c)) box.append(svg("rect", { x: c, y: r, width: 1, height: 1 }));
    }
  }
}

async function checkPhone() {
  clearTimeout(pollTimer);
  if (over || mode !== "phone") return;
  let info;
  try {
    // A POST keeps the code out of request logs, where a query string would land.
    info = await api({ action: "phone_status", code });
  } catch (error) {
    info = { status: "waiting", detail: error.message };
  }
  const line = PHONE_STATES[info.status] || PHONE_STATES.waiting;
  const detail = info.status === "failed" && info.detail ? " " + info.detail : "";
  if (!$("game").hidden) {
    const took = info.last_ms ? " Last answer took " + (info.last_ms / 1000).toFixed(1) + " s." : "";
    $("voice").textContent = (info.status === "ready"
      ? "Voiced by your phone" + (info.model ? ", " + info.model : "") + "."
      : line + " Until then the suspects use written lines.") + took;
  } else {
    $("phone-state").textContent = line + detail;
    const loading = info.status === "loading";
    $("phone-progress").hidden = !loading;
    if (loading) {
      $("phone-progress").value = info.progress || 0;
      $("phone-state").textContent = line + " " + Math.round((info.progress || 0) * 100) + "%.";
    }
    if (info.status === "ready") begin();
  }
  if (!document.hidden) pollTimer = setTimeout(checkPhone, $("game").hidden ? PAIR_POLL : GAME_POLL);
}

$("play-written").addEventListener("click", () => {
  mode = "scripted";
  begin();
});

$("play-phone").addEventListener("click", async () => {
  $("start-error").textContent = "";
  try {
    code = (await api({ action: "pair" })).code;
  } catch (error) {
    $("start-error").textContent = error.message;
    return;
  }
  mode = "phone";
  // The code travels in the fragment, which browsers never send to a server or a referrer.
  const url = location.origin + "/phone.html#" + code;
  drawQr(url);
  $("phone-link").href = url;
  $("phone-link").textContent = url;
  $("play-written").disabled = true;
  $("play-phone").disabled = true;
  $("pairing").hidden = false;
  checkPhone();
});

// Ends the pairing, so a phone left open stops polling at once instead of for three hours.
function releasePhone() {
  if (code) api({ action: "phone_bye", code }).catch(() => {});
  code = null;
}

$("give-up").addEventListener("click", () => {
  clearTimeout(pollTimer);
  releasePhone();
  mode = "scripted";
  begin();
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && mode === "phone") checkPhone();
});

function begin() {
  $("start").hidden = true;
  $("game").hidden = false;
  $("voice").textContent = mode === "phone" ? "Voiced by your phone." : "Written lines.";
  pick(suspect || game.suspects[0]);
  $("question").focus();
}

// ---- The case ----

// The one place that decides whether the player can ask.
function refresh() {
  const locked = busy || over || remaining === 0;
  for (const field of $("ask").elements) field.disabled = locked;
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
  if (!question || busy || over || remaining === 0) return;
  const asked = suspect;
  const shown = $("show").value;
  busy = true;
  refresh();
  $("status").textContent = asked.name + (mode === "phone"
    ? " is thinking. Your phone can take a few seconds." : " is thinking.");
  // In phone mode the reply is shown while the phone types it, then replaced by the final text.
  let typing, peekTimer;
  const peek = async () => {
    try {
      const { text } = await api({ action: "phone_peek", code });
      if (text && typing) typing.lastChild.textContent = text + "…";
    } catch {
      // only a preview; the final answer still arrives
    }
    if (typing) peekTimer = setTimeout(peek, PEEK_EVERY);
  };
  if (mode === "phone") {
    typing = record("q typing", "Q, to " + asked.name + ".", question);
    typing = record("a typing", "A.", "…");
    peekTimer = setTimeout(peek, PEEK_EVERY);
  }
  try {
    let turn;
    try {
      turn = await api({ action: "ask", suspect: asked.id, question, show: shown || null, state, mode, code });
    } finally {
      clearTimeout(peekTimer);
      typing = null;
      for (const line of $("log").querySelectorAll(".typing")) line.remove();
    }
    state = turn.state;
    leads = turn.leads;
    plan = turn.map;
    record("q", "Q, to " + asked.name + (shown ? ", showing exhibit " + letter(shown) : "") + ".", question);
    const answer = record("a", "A.", turn.reply);
    if (turn.cracked) answer.append(" ", el("span", "Statement changed", "stamp new"));
    if (mode === "phone") {
      answer.append(el("small", turn.source === "phone" ? "Answered by your phone."
        : WHY[turn.why] || "Written line."));
      if (turn.why) checkPhone();
    }
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
    releasePhone();
    refresh();
    $("accuse").disabled = true;
    $("verdict").hidden = false;
    $("again").focus();
  } catch (error) {
    $("status").textContent = error.message;
  }
});

$("again").addEventListener("click", () => location.reload());

(async () => {
  try {
    game = await api();
  } catch (error) {
    $("title").textContent = "The case could not be opened";
    $("intro").textContent = error.message;
    $("start").hidden = true;
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
  $("play-written").disabled = false;
  $("play-phone").disabled = false;
})();
