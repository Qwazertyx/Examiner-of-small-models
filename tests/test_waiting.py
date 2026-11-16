import http.client
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dev  # noqa: E402
import game.model as model  # noqa: E402
from game import rules  # noqa: E402
from phone import relay  # noqa: E402

CASE = json.loads((Path(__file__).parent / "case.json").read_text(encoding="utf-8"))
TOKEN = "t" * 40
ASK = {"action": "ask", "suspect": "bruno", "question": "What did you see of the others?",
       "state": {"clues": ["crumbs"], "asked": 0}}


def raw(req):
    return json.dumps(req).encode()


def fake(topic='{"topic": "others"}', reply="Sure."):
    return lambda system, user, schema, max_tokens: topic if schema is not None else reply


def env(**kw):
    """Patch the environment: None removes a variable."""
    p = patch.dict(os.environ, {k: v for k, v in kw.items() if v is not None}, clear=False)
    p.start()
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
    return p


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def stop(srv):
    srv.shutdown()
    srv.server_close()


def call(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        c.request(method, path, body, headers or {})
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


class RulesWaitingTest(unittest.TestCase):
    def test_model_none_raises(self):
        with self.assertRaises(rules.ModelDown):
            rules.ask(CASE, ASK, None, fallback=False)

    def test_handle_503(self):
        status, payload = rules.handle("POST", raw(ASK), None, sc=CASE, fallback=False)
        self.assertEqual(status, 503)
        self.assertIn("error", payload)
        self.assertNotIn("reply", payload)
        self.assertNotIn("state", payload)

    def test_classification_failure(self):
        def down(system, user, schema, max_tokens):
            raise rules.ModelDown("x")
        with self.assertRaises(rules.ModelDown):
            rules.ask(CASE, ASK, down, fallback=False)

    def test_empty_reply_is_not_half_scripted(self):
        with self.assertRaises(rules.ModelDown):
            rules.ask(CASE, ASK, fake(reply=""), fallback=False)

    def test_working_model(self):
        out = rules.ask(CASE, ASK, fake(), fallback=False)
        self.assertEqual(out["source"], "phone")
        self.assertEqual(out["reply"], "Sure.")

    def test_validation_first(self):
        for name, req in {"suspect": dict(ASK, suspect="nobody"),
                          "limit": dict(ASK, state={"clues": ["crumbs"], "asked": CASE["max_questions"]})}.items():
            with self.subTest(name):
                self.assertEqual(rules.handle("POST", raw(req), None, sc=CASE, fallback=False)[0], 400)

    def test_accuse_without_model(self):
        req = {"action": "accuse", "suspect": "bruno", "state": {"clues": ["crumbs"], "asked": 0}}
        self.assertEqual(rules.handle("POST", raw(req), None, sc=CASE, fallback=False)[0], 200)

    def test_default_falls_back(self):
        status, payload = rules.handle("POST", raw(ASK), None, sc=CASE)
        self.assertEqual(status, 200)
        self.assertEqual(payload["source"], "script")


class Stub(BaseHTTPRequestHandler):
    """Answers every request with the owner's `status`/`body`/`location` and records it."""
    def log_message(self, *a):
        pass

    def do_GET(self):
        o = self.server.owner
        o.seen.append((self.path, self.headers.get("Authorization")))
        body = o.body.encode()
        self.send_response(o.status)
        if o.location:
            self.send_header("Location", o.location)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class StubServer:
    def __init__(self, status=200, body='{"ok": true}', location=None):
        self.status, self.body, self.location, self.seen = status, body, location, []
        self.srv = serve(Stub)
        self.srv.owner = self
        self.port = self.srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"


class PhoneReadyTest(unittest.TestCase):
    def setUp(self):
        model._health = (float("-inf"), False)
        self.stub = StubServer()
        self.addCleanup(stop, self.stub.srv)

    def ready(self, url="stub", token=TOKEN):
        p = env(PHONE_URL=self.stub.url if url == "stub" else url, PHONE_TOKEN=token)
        self.addCleanup(p.stop)
        return model.phone_ready()

    def test_unconfigured(self):
        for url, token in ((None, TOKEN), (self.stub.url, None), ("http://example.com", TOKEN)):
            with self.subTest(url=url, token=token):
                model._health = (float("-inf"), False)
                p = env(PHONE_URL=url, PHONE_TOKEN=token)
                try:
                    self.assertFalse(model.phone_ready())
                finally:
                    p.stop()
        self.assertEqual(self.stub.seen, [])

    def test_ok(self):
        self.assertTrue(self.ready())
        self.assertEqual(self.stub.seen, [("/health", "Bearer " + TOKEN)])

    def test_not_ready(self):
        other = StubServer()
        self.addCleanup(stop, other.srv)
        cases = {"ok false": (200, '{"ok": false}', None), "ok string": (200, '{"ok": "yes"}', None),
                 "503": (503, '{"ok": true}', None), "not json": (200, "nope", None),
                 "list": (200, "[true]", None), "redirect": (302, "", other.url + "/health")}
        for name, (status, body, loc) in cases.items():
            with self.subTest(name):
                model._health = (float("-inf"), False)
                self.stub.status, self.stub.body, self.stub.location = status, body, loc
                self.assertFalse(self.ready())
        self.assertEqual(other.seen, [])

    def test_cache(self):
        self.assertTrue(self.ready())
        self.stub.status = 503
        self.assertTrue(model.phone_ready())
        self.assertEqual(len(self.stub.seen), 1)
        model._health = (float("-inf"), False)
        self.assertFalse(model.phone_ready())
        self.assertEqual(len(self.stub.seen), 2)


class RelayHealthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = relay.make_server("127.0.0.1", 0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        stop(cls.srv)

    def setUp(self):
        self.up = True
        p = patch.object(relay, "TOKEN", TOKEN)
        q = patch.object(relay, "ollama_up", lambda: self.up)
        for x in (p, q):
            x.start()
            self.addCleanup(x.stop)
        relay.hits.clear()

    def get(self, path="/health", auth="Bearer " + TOKEN):
        status, body = call(self.port, "GET", path, headers={"Authorization": auth} if auth else {})
        return status, json.loads(body)

    def test_auth(self):
        self.assertEqual(self.get(auth=None)[0], 401)
        self.assertEqual(self.get(auth="Bearer " + "x" * 40)[0], 401)

    def test_ok(self):
        self.assertEqual(self.get(), (200, {"ok": True}))
        self.up = False
        self.assertEqual(self.get(), (200, {"ok": False}))

    def test_other_path(self):
        self.assertEqual(self.get("/other")[0], 404)

    def test_ignores_busy_and_hits(self):
        with relay.busy:
            self.assertEqual(self.get()[0], 200)
        self.assertEqual(relay.hits, {})


class PhoneStub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.send(200, {"ok": True}) if self.path == "/health" else self.send(404, {})

    def do_POST(self):
        if self.server.chat_status != 200:
            return self.send(self.server.chat_status, {"error": "x"})
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if req["schema"] is not None:
            topic = req["schema"]["properties"]["topic"]["enum"][0]
            return self.send(200, {"text": json.dumps({"topic": topic})})
        self.send(200, {"text": "Hello."})


class HttpWaitingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = dev.make_server(0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        stop(cls.srv)

    def setUp(self):
        model._health = (float("-inf"), False)

    def use(self, **kw):
        p = env(**{"SCRIPTED": None, "PHONE_URL": None, "PHONE_TOKEN": None, **kw})
        self.addCleanup(p.stop)

    def view(self):
        return json.loads(call(self.port, "GET", "/api/game")[1])

    def post(self, body):
        status, data = call(self.port, "POST", "/api/game", json.dumps(body).encode(),
                            {"Content-Type": "application/json"})
        return status, json.loads(data)

    def ask(self):
        v = self.view()
        sid = v["suspects"][0]["id"]
        return {"action": "ask", "suspect": sid, "question": v["leads"][sid][0]["sample"],
                "state": {"clues": [c["id"] for c in v["clues"]], "asked": 0}}

    def status(self):
        return json.loads(call(self.port, "GET", "/api/game?status")[1])

    def test_waiting(self):
        self.use()
        self.assertEqual(self.status(), {"phone": "waiting"})
        status, payload = self.post(self.ask())
        self.assertEqual(status, 503)
        self.assertIn("error", payload)
        acc = self.ask()
        acc["action"] = "accuse"
        self.assertEqual(self.post(acc)[0], 200)
        status, body = call(self.port, "GET", "/api/game")
        self.assertEqual(status, 200)
        self.assertIn("title", json.loads(body))

    def test_scripted(self):
        self.use(SCRIPTED="1")
        self.assertEqual(self.status(), {"phone": "scripted"})
        status, payload = self.post(self.ask())
        self.assertEqual(status, 200)
        self.assertEqual(payload["source"], "script")

    def with_phone(self, chat_status):
        phone = serve(PhoneStub)
        phone.chat_status = chat_status
        self.addCleanup(stop, phone)
        self.use(PHONE_URL=f"http://127.0.0.1:{phone.server_address[1]}", PHONE_TOKEN=TOKEN)

    def test_phone_ready(self):
        self.with_phone(200)
        self.assertEqual(self.status(), {"phone": "ready"})
        status, payload = self.post(self.ask())
        self.assertEqual(status, 200)
        self.assertEqual(payload["source"], "phone")
        self.assertEqual(payload["reply"], "Hello.")

    def test_phone_failing(self):
        self.with_phone(503)
        self.assertEqual(self.post(self.ask())[0], 503)


if __name__ == "__main__":
    unittest.main()
