"""Runs on the phone (Termux). Lets one trusted server use the local Ollama model.

    PHONE_TOKEN=<long random secret> python relay.py

Only requests carrying the token get through, one at a time, with a per-player
rate limit. Ollama itself stays on 127.0.0.1 and is never exposed.
"""
import hmac
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("PHONE_TOKEN", "")
MODEL = os.environ.get("MODEL", "gemma3:1b")
OLLAMA = "http://127.0.0.1:11434/api/chat"
MAX_BODY = 8192
MAX_TOKENS = 80
PER_MINUTE = 20  # per player; one question costs two calls
BUSY_WAIT = 10   # seconds a request may wait for the model before being turned away

busy = threading.Lock()  # a 4 GB phone runs one generation at a time
hits = {}                # player hash -> times of recent requests
hits_lock = threading.Lock()


def allowed(client):
    now = time.monotonic()
    with hits_lock:
        if len(hits) > 10_000:
            # shortcut: forgets everyone's history under a flood of new players, use an LRU if that happens
            hits.clear()
        recent = [t for t in hits.get(client, []) if now - t < 60]
        ok = len(recent) < PER_MINUTE
        if ok:
            recent.append(now)
        hits[client] = recent
        return ok


def generate(system, user, schema, max_tokens):
    body = {
        "model": MODEL,
        "stream": False,
        "keep_alive": "30m",
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "options": {"num_predict": max_tokens, "temperature": 0 if schema else 0.7},
    }
    if schema:
        body["format"] = schema
    req = urllib.request.Request(OLLAMA, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as res:
        return json.loads(res.read())["message"]["content"]


def ollama_up():
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/version", timeout=2):
            return True
    except OSError:
        return False


class Relay(BaseHTTPRequestHandler):
    timeout = 15  # drop connections that stall while sending their request

    def reply(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        auth = self.headers.get("Authorization", "")
        return hmac.compare_digest(auth.encode(), b"Bearer " + TOKEN.encode())

    def do_GET(self):
        if not self.authorized():
            return self.reply(401, {"error": "unauthorized"})
        if self.path != "/health":
            return self.reply(404, {"error": "not found"})
        self.reply(200, {"ok": ollama_up()})

    def do_POST(self):
        if not self.authorized():
            return self.reply(401, {"error": "unauthorized"})
        if self.path != "/chat":
            return self.reply(404, {"error": "not found"})
        length = self.headers.get("Content-Length", "")
        if not length.isdigit() or not 0 < int(length) <= MAX_BODY:
            return self.reply(413, {"error": "bad length"})
        try:
            req = json.loads(self.rfile.read(int(length)))
            system, user, schema = req["system"], req["user"], req.get("schema")
            max_tokens = req.get("max_tokens", MAX_TOKENS)
            if (not isinstance(system, str) or not isinstance(user, str)
                    or not (schema is None or isinstance(schema, dict))
                    or type(max_tokens) is not int or max_tokens < 1):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return self.reply(400, {"error": "bad request"})
        if not allowed(self.headers.get("X-Client", "")[:64]):
            return self.reply(429, {"error": "slow down"})
        if not busy.acquire(timeout=BUSY_WAIT):
            return self.reply(503, {"error": "busy"})
        try:
            text = generate(system, user, schema, min(max_tokens, MAX_TOKENS))
        except (OSError, ValueError, KeyError, TypeError):
            return self.reply(502, {"error": "model failed"})
        finally:
            busy.release()
        self.reply(200, {"text": text})


def make_server(host, port):
    return ThreadingHTTPServer((host, port), Relay)


if __name__ == "__main__":
    if len(TOKEN) < 32:
        sys.exit("Set PHONE_TOKEN to a random secret of at least 32 characters, e.g.\n"
                 "  export PHONE_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')")
    # 127.0.0.1: only the tunnel running on this phone can reach the relay.
    host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8787"))
    print(f"relay on {host}:{port}, model {MODEL}")
    make_server(host, port).serve_forever()
