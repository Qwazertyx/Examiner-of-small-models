"""Vercel function for /api/game. dev.py reuses serve() locally."""
import hashlib
import json
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from game import phone, rules  # noqa: E402
from game.store import StoreDown, make_store  # noqa: E402

MAX_BODY = 4096
REQUESTS_PER_MINUTE = 90  # per address, on actions that touch the store; a real game needs about 30
STORE = make_store()
PHONE_ACTIONS = {
    "phone_status": phone.status, "phone_bye": phone.bye,
    "phone_hello": phone.hello, "phone_next": phone.next_job, "phone_done": phone.done,
}


class TooMany(Exception):
    pass


def client_id(h):
    # Vercel overwrites these headers itself, so a client cannot forge them.
    ip = h.headers.get("x-vercel-forwarded-for") or h.headers.get("x-real-ip") or h.client_address[0]
    return hashlib.sha256(ip.encode()).hexdigest()[:16]  # rate limits key on a hash, not the address


def post(h, raw):
    req = json.loads(raw)
    if not isinstance(req, dict):
        raise rules.BadRequest("Expected a JSON object.")
    action = req.get("action")
    phone_mode = req.get("mode") == "phone" and phone.valid(req.get("code"))
    if action == "pair" or action in PHONE_ACTIONS or phone_mode:
        if STORE is None:
            return 503, {"error": "Phone mode is not set up on this site."}
        if action in PHONE_ACTIONS:
            phone._code(req)  # a guessed code is refused here, before it costs anything
        if STORE.incr("rate:req:" + client_id(h), 60) > REQUESTS_PER_MINUTE:
            raise TooMany()
    if action == "pair":
        return 200, phone.pair(STORE, client_id(h))
    if action in PHONE_ACTIONS:
        return 200, PHONE_ACTIONS[action](STORE, req)
    model = phone.phone_model(STORE, req["code"]) if phone_mode and STORE is not None else None
    try:
        # A phone that is asleep or too slow costs a written line, never the question.
        return rules.handle("POST", raw, model)
    finally:
        if model is not None:
            model.release()


def serve(h):
    try:
        if h.command == "GET":
            status, payload = rules.handle("GET", b"")
        else:
            length = h.headers.get("Content-Length", "")
            # Requiring JSON keeps other sites from posting here with a plain HTML form.
            if h.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                status, payload = 415, {"error": "Send JSON."}
            elif not length.isdigit() or int(length) > MAX_BODY:
                status, payload = 413, {"error": "Request too large."}
            else:
                status, payload = post(h, h.rfile.read(int(length)))
    except (ValueError, RecursionError):
        status, payload = 400, {"error": "Invalid JSON."}
    except rules.BadRequest as e:
        status, payload = 400, {"error": str(e)}
    except TooMany:
        status, payload = 429, {"error": "Too many requests. Wait a minute."}
    except phone.Gone:
        status, payload = 410, {"error": "This game has ended. Scan a new code on the computer."}
    except StoreDown:
        status, payload = 503, {"error": "The phone link is unavailable. Play with written lines for now."}

    body = json.dumps(payload).encode()
    h.send_response(status)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "no-store")
    h.send_header("X-Content-Type-Options", "nosniff")
    h.end_headers()
    h.wfile.write(body)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        serve(self)

    def do_POST(self):
        serve(self)
