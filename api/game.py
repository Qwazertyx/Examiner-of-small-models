"""Vercel function for /api/game. dev.py reuses serve() locally."""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from game import rules  # noqa: E402
from game.model import phone_model, phone_ready  # noqa: E402

MAX_BODY = 4096


def serve(h):
    # SCRIPTED=1 plays without a phone, with the scenario's own lines. Otherwise the game waits for the phone.
    scripted = os.environ.get("SCRIPTED") == "1"
    if h.command == "GET":
        if urlsplit(h.path).query == "status":
            status, payload = 200, {"phone": "scripted" if scripted else "ready" if phone_ready() else "waiting"}
        else:
            status, payload = rules.handle("GET", b"")
    else:
        length = h.headers.get("Content-Length", "")
        # Requiring JSON keeps other sites from posting here with a plain HTML form.
        if h.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            status, payload = 415, {"error": "Send JSON."}
        elif not length.isdigit() or int(length) > MAX_BODY:
            status, payload = 413, {"error": "Request too large."}
        else:
            # Vercel overwrites these headers itself, so a client cannot forge them.
            ip = (h.headers.get("x-vercel-forwarded-for") or h.headers.get("x-real-ip")
                  or h.client_address[0])
            model = None if scripted else phone_model(ip)
            status, payload = rules.handle("POST", h.rfile.read(int(length)), model, fallback=scripted)

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
