// Lends this phone's browser to one game: the model runs here on WebGPU and
// answers the jobs the server queues for the code in the URL hash.
// Self-hosted copy of @mlc-ai/web-llm 0.2.88 (lib/index.js), so no third party serves code to this page.
const LIB = "./vendor/web-llm-0.2.88.js";
// Fast is the default: half the parameters of Llama 1B, so about twice the speed on the same GPU.
const VOICES = {
  fast: { name: "Qwen 2.5 0.5B", id: "Qwen2.5-0.5B-Instruct", mb: 290 },
  rich: { name: "Llama 3.2 1B", id: "Llama-3.2-1B-Instruct", mb: 705 },
};
const CONTEXT = 1024; // prompts stay under 400 tokens; a small window saves GPU memory
const PART_EVERY = 700; // ms between partial replies sent while typing
let MODEL = "";
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

async function generate(engine, job, onPart) {
  const request = {
    messages: [{ role: "system", content: job.system }, { role: "user", content: job.user }],
    temperature: job.schema ? 0 : 0.7,
    max_tokens: job.max_tokens,
  };
  if (job.schema) {
    request.response_format = { type: "json_object", schema: JSON.stringify(job.schema) };
    const res = await engine.chat.completions.create(request);
    return (res.choices[0].message.content || "").slice(0, MAX_TEXT);
  }
  // A reply streams, so the computer can show the words as they come instead of after the last one.
  let text = "", sent = performance.now();
  for await (const chunk of await engine.chat.completions.create({ ...request, stream: true })) {
    text += chunk.choices[0]?.delta?.content || "";
    if (onPart && performance.now() - sent > PART_EVERY) {
      sent = performance.now();
      onPart(text);
    }
  }
  return text.slice(0, MAX_TEXT);
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
    let text, sending = false;
    const t0 = performance.now();
    // At most one partial reply in flight: a slow network must not pile up requests.
    const onPart = (part) => {
      if (sending) return;
      sending = true;
      api({ action: "phone_part", id: job.id, text: part.slice(0, 300) }).catch(() => {}).finally(() => { sending = false; });
    };
    try {
      text = await generate(engine, job, onPart);
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

async function start(lib, modelId, voice) {
  MODEL = voice.name;
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
    }, { context_window_size: CONTEXT });
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
  const quant = adapter.features.has("shader-f16") ? "q4f16" : "q4f32";
  for (const [key, voice] of Object.entries(VOICES)) {
    const modelId = `${voice.id}-${quant}_1-MLC`;
    const cached = await lib.hasModelInCache(modelId).catch(() => false);
    $(key + "-note").textContent = voice.name + ". "
      + (cached ? "Already on this phone, no download." : `About ${voice.mb} MB, kept in this browser for next time.`);
    $("start-" + key).onclick = () => start(lib, modelId, voice);
  }
  show("setup");
}

main();
