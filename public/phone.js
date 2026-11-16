// Lends this phone's browser to one game: the model runs here on WebGPU and
// answers the jobs the server queues for the code in the URL hash.
// Self-hosted copy of @mlc-ai/web-llm 0.2.88 (lib/index.js), so no third party serves code to this page.
const LIB = "./vendor/web-llm-0.2.88.js";
const MODEL = "Llama 3.2 1B";
const HELLO_EVERY = 2000; // ms between progress reports while loading
const RETRY = 2000;       // ms before retrying the server after a network error
const MAX_TEXT = 1000;    // keeps the answer under the server's 4 KB request cap even when escaped

const $ = (id) => document.getElementById(id);
const code = location.hash.slice(1);
let status = "loading";
let progress = 0;
let detail;
let answered = 0;

function show(id) {
  for (const s of document.querySelectorAll("main > section")) s.hidden = s.id !== id;
}

function fail(text, help) {
  status = "failed";
  detail = text.slice(0, 120);
  $("error-text").textContent = text;
  $("error-help").textContent = help || "";
  show("error");
}

async function api(body) {
  const res = await fetch("/api/game", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...body, code }),
  });
  if (res.status === 404 || res.status === 410) throw new Error("ended");
  if (!res.ok) throw new Error("server " + res.status);
  return res.json();
}

function hello() {
  return api({ action: "phone_hello", status, progress, model: MODEL, detail }).catch(() => {});
}

function ended() {
  fail("This game has ended. Scan a new code on the computer.");
  status = "ended"; // not reported: the server already knows
}

async function keepAwake() {
  if (!navigator.wakeLock) {
    $("screen").hidden = false;
    return;
  }
  const request = () => navigator.wakeLock.request("screen").catch(() => { $("screen").hidden = false; });
  await request();
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") request();
  });
}

async function generate(engine, job) {
  const res = await engine.chat.completions.create({
    messages: [{ role: "system", content: job.system }, { role: "user", content: job.user }],
    temperature: job.schema ? 0 : 0.7,
    max_tokens: job.max_tokens,
    response_format: job.schema ? { type: "json_object", schema: JSON.stringify(job.schema) } : undefined,
  });
  return (res.choices[0].message.content || "").slice(0, MAX_TEXT);
}

async function serve(engine) {
  for (;;) {
    let job;
    try {
      ({ job } = await api({ action: "phone_next" }));
    } catch (e) {
      if (e.message === "ended") return ended();
      $("live").textContent = "Lost the server, retrying…";
      await new Promise((r) => setTimeout(r, RETRY));
      continue;
    }
    if (!job) {
      $("live").textContent = "Waiting for a question…";
      continue;
    }
    $("live").textContent = "Answering a question…";
    let text;
    const t0 = performance.now();
    try {
      text = await generate(engine, job);
    } catch (e) {
      fail("The model stopped working: " + e.message,
        "Reload this page to try again. If it happens again, this phone may be out of memory.");
      return hello();
    }
    try {
      await api({ action: "phone_done", id: job.id, text, ms: Math.round(performance.now() - t0) });
    } catch (e) {
      if (e.message === "ended") return ended();
    }
    $("count").textContent = `${++answered} question${answered === 1 ? "" : "s"} answered`;
  }
}

async function start(lib, modelId) {
  show("loading");
  const timer = setInterval(hello, HELLO_EVERY);
  let engine;
  try {
    engine = await lib.CreateMLCEngine(modelId, {
      logLevel: "ERROR",
      initProgressCallback: (r) => {
        progress = r.progress;
        $("bar").value = r.progress;
        $("percent").textContent = `${Math.round(r.progress * 100)} %`;
        // "Fetching param cache", "Loading model from cache", "Loading GPU shader modules"
        $("loading-text").textContent = r.text.includes("Fetching") ? "Downloading the model…"
          : r.text.includes("shader") ? "Compiling for this phone's GPU…" : "Loading the model…";
      },
    });
  } catch (e) {
    clearInterval(timer);
    fail("Could not load the model: " + e.message,
      "Check the connection, close other apps and tabs, then reload this page.");
    return hello();
  }
  // The first generation compiles the GPU kernels; pay that here, not on the player's first question.
  $("loading-text").textContent = "Warming up…";
  try {
    await generate(engine, { system: "Reply with one word.", user: "Ready?", schema: null, max_tokens: 4 });
    await generate(engine, { system: "Pick a topic.", user: "Hello", max_tokens: 8,
      schema: { type: "object", properties: { topic: { type: "string", enum: ["a", "none"] } }, required: ["topic"] } });
  } catch (e) {
    clearInterval(timer);
    fail("The model stopped working: " + e.message,
      "Reload this page to try again. If it happens again, this phone may be out of memory.");
    return hello();
  }
  clearInterval(timer);
  status = "ready";
  progress = 1;
  show("ready");
  await hello();
  keepAwake();
  serve(engine);
}

async function main() {
  if (!/^[A-Za-z0-9_-]{16,64}$/.test(code)) {
    return fail("This link is incomplete.", "Scan the code shown on the computer again.");
  }
  hello();
  const adapter = navigator.gpu && await navigator.gpu.requestAdapter().catch(() => null);
  if (!adapter) {
    fail("This browser has no WebGPU, which the model needs.",
      "On Android, open this link in Chrome. On iPhone, Safari on iOS 26 or later.");
    return hello();
  }
  let lib;
  try {
    lib = await import(LIB);
  } catch {
    fail("Could not download the program that runs the model.", "Check the connection and reload this page.");
    return hello();
  }
  // f16 halves the GPU memory; the f32 build is for GPUs without that extension.
  const modelId = `Llama-3.2-1B-Instruct-${adapter.features.has("shader-f16") ? "q4f16" : "q4f32"}_1-MLC`;
  const cached = await lib.hasModelInCache(modelId).catch(() => false);
  $("cache").textContent = cached
    ? "The model is already on this phone from an earlier game. No download needed."
    : "About 700 MB will be downloaded and kept in this browser for next time.";
  $("start").textContent = cached ? "Start" : "Start the download";
  $("start").onclick = () => start(lib, modelId);
  show("setup");
}

main();
