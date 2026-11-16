# Examiner of Small Models

An interrogation game. You question three suspects, collect evidence, confront them
with what you found, and accuse one. The suspects can be voiced by a one-billion-parameter
language model running in the browser of your own phone.

![The case file, mid-game](design/screenshot.png)

The first case, *Three Small Models*: the night before a release review, someone
erased the exam scores at Halden Lab. Three small models spent the night there:
Pip, a support model that never says no; Grit, a terse code model; Ode, a poetry
model. Each one lies about its night. A floor plan fills in as you learn the lab's
wiring, each suspect lists the subjects you can raise, and new subjects open as you
find evidence. Naming the right model is not enough: the board wants a confession.

Work in progress: the in-browser model has not run on a real phone yet.

## Two ways to play

- **Written lines**: starts at once, the suspects answer with lines written for the case.
- **Lend your phone**: the computer shows a QR code. The phone opens `phone.html`, downloads
  Llama 3.2 1B (about 700 MB the first time, then cached) and runs it on its GPU with
  [WebLLM](https://github.com/mlc-ai/web-llm). It needs WebGPU: Chrome on a recent Android
  phone, or Safari on iOS 26. If the phone sleeps or is too slow, that answer falls back to
  the written line and the game goes on.

## How it works

```
computer ──ask──> Vercel function ──job──> Upstash Redis <──poll── phone.html (WebLLM)
           <─answer─  rules, scenario    <──answer──            one game per pairing code
```

A small model keeps secrets badly, so it never gets one. For each question:

1. the model picks which topic the question is about, from a fixed list, with constrained JSON output;
2. the rules decide what the suspect says: a lie, a dodge, or the truth if you showed the right evidence;
3. the model rewrites that one fact in the suspect's voice.

The culprit and the hidden truths are never in a prompt. The notebook in the interface
records the scripted fact, so a model that embellishes cannot mislead the player.

The computer and the phone share a random pairing code, carried in the QR code's URL
fragment, which browsers never send to servers. A phone only ever serves the game whose
code it scanned, so a tampered phone can only spoil its owner's own game.

## Run it

Python 3.12 or later, standard library only.

```bash
python3 dev.py                          # http://127.0.0.1:8000
python3 -m unittest discover tests
```

Locally the pairing lives in memory. A phone cannot run the model against the dev server:
WebGPU needs https, so the phone mode is tested on the deployed site.

## Layout

| Path | Role |
|---|---|
| `game/rules.py` | The rules: input checks, topic choice, what a suspect reveals |
| `game/phone.py` | Pairing, job queue and status for the lending phone |
| `game/store.py` | Upstash Redis over REST on Vercel, a dict locally |
| `game/scenario.json` | The case: suspects, topics, lies, clues, floor plan, culprit |
| `api/game.py` | Vercel function for `/api/game` |
| `public/index.html`, `app.js` | The game, on the computer |
| `public/phone.html`, `phone.js` | The lending phone |
| `dev.py` | Local server running the same code as Vercel |

## Deploy

1. Import the repository in Vercel and deploy. Written lines work with nothing else.
2. For the phone mode, add an Upstash Redis store to the project (Storage tab, free plan)
   and redeploy. Its `KV_REST_API_URL` and `KV_REST_API_TOKEN` (or `UPSTASH_REDIS_REST_*`)
   variables are picked up automatically.
3. Check that `https://<site>/game/scenario.json` answers 404, and add one rate-limit rule
   on `/api/game` in the Vercel Firewall.

Upstash's REST API cannot block while waiting, so the server polls. A phone waiting for
questions costs about 3,500 commands an hour; the free plan has 500,000 a month. A pairing
ends when the game ends, or after three hours at most.

## Security

- The model never sees the culprit, and the server checks the phone's output: the topic must be one
  of the listed ones, and the text is cut to 300 characters and shown as plain text.
- Pairing codes are random (144 bits) and signed, so a guessed code is refused without touching
  the store. They end with the game, or after three hours at most, and are limited to 10 an hour
  per player.
- A code serves one poll and one question at a time, and every player address is limited to
  90 phone requests a minute, so a script cannot multiply the store and function costs.
- The phone page runs a self-hosted copy of WebLLM 0.2.88 (checked against two CDNs), under its
  own Content-Security-Policy. It still downloads the model weights from Hugging Face and the
  model's GPU library from MLC's GitHub repository, which it has to trust.
- Everything the browser sends is validated; questions are capped at 200 characters and requests at 4 KB.
- Model output is inserted as text, never as HTML, under a strict Content-Security-Policy.
- Rate limits key on a hash of the player's address, never the address itself.

The scenario lives in this public repository, so the solution can be read here.
Hiding it from the page only keeps the game honest for people who play it.

## Writing a case

A case is one JSON file. `rules.check_scenario` refuses a case with an unknown clue or
room, a clue nobody can reach, or a suggested question that does not lead to its own
topic. The tests then play the case through the engine and check that every clue and
the proof can be obtained.

## Credits

Typeface: Special Elite by Astigmatic, Apache License 2.0.
QR codes: qrcode-generator 1.4.4 by Kazuhiko Arase, MIT License (`public/vendor/`).
In-browser model: WebLLM by MLC, Apache License 2.0; Llama 3.2 1B, Llama 3.2 Community License.
