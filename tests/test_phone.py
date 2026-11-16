import http.client
import http.server
import json
import os
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api.game  # noqa: E402
import dev  # noqa: E402
from game import phone, rules, store as store_mod  # noqa: E402
from game.store import Memory, StoreDown, Upstash, make_store  # noqa: E402


def later(delay, fn):
    t = threading.Thread(target=lambda: (time.sleep(delay), fn()), daemon=True)
    t.start()
    return t


class MemoryTest(unittest.TestCase):
    def test_get_set(self):
        m = Memory()
        self.assertIsNone(m.get("a"))
        m.set("a", "1", 10)
        self.assertEqual(m.get("a"), "1")

    def test_set_expires(self):
        m = Memory()
        with patch.object(store_mod.time, "monotonic", return_value=100.0):
            m.set("a", "1", 10)
            self.assertEqual(m.get("a"), "1")
        with patch.object(store_mod.time, "monotonic", return_value=111.0):
            self.assertIsNone(m.get("a"))

    def test_incr_keeps_first_expiry(self):
        m = Memory()
        with patch.object(store_mod.time, "monotonic", return_value=100.0):
            self.assertEqual(m.incr("n", 10), 1)
        with patch.object(store_mod.time, "monotonic", return_value=105.0):
            self.assertEqual(m.incr("n", 10), 2)
        with patch.object(store_mod.time, "monotonic", return_value=111.0):
            self.assertEqual(m.incr("n", 10), 1)  # expired at 110, not extended to 115

    def test_fifo(self):
        m = Memory()
        for v in "abc":
            m.push("l", v, 10)
        self.assertEqual([m.pop("l", 0.05) for _ in range(3)], ["a", "b", "c"])

    def test_pop_empty_times_out(self):
        t = time.monotonic()
        self.assertIsNone(Memory().pop("l", wait=0.05))
        self.assertLess(time.monotonic() - t, 0.5)

    def test_pop_wakes_on_push(self):
        m = Memory()
        later(0.05, lambda: m.push("l", "x", 10))
        t = time.monotonic()
        self.assertEqual(m.pop("l", wait=2), "x")
        self.assertLess(time.monotonic() - t, 1)

    def test_expired_list_not_returned(self):
        m = Memory()
        with patch.object(store_mod.time, "monotonic", return_value=100.0):
            m.push("l", "x", 10)
        with patch.object(store_mod.time, "monotonic", return_value=200.0):
            self.assertIsNone(m.pop("l", 0))


class StubRedis:
    """Imitates the Upstash REST API on 127.0.0.1."""

    def __init__(self):
        self.data, self.calls, self.auth, self.mode = {}, [], [], "ok"
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.auth.append(self.headers.get("Authorization"))
                if outer.mode == "500":
                    return self.reply(500, b"oops")
                if outer.mode == "text":
                    return self.reply(200, b"not json")
                cmd = json.loads(body)
                outer.calls.append(cmd)
                if outer.mode == "error":
                    out = {"error": "ERR bad"}
                else:
                    out = {"result": outer.run(cmd)}
                self.reply(200, json.dumps(out).encode())

            def reply(self, code, b):
                self.send_response(code)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, args=(0.01,), daemon=True).start()

    def run(self, cmd):
        op, key, *a = cmd
        d = self.data
        if op == "GET":
            return d.get(key)
        if op == "SET":
            d[key] = a[0]
            return "OK"
        if op == "INCR":
            d[key] = str(int(d.get(key, 0)) + 1)
            return int(d[key])
        if op == "EXPIRE":
            return 1
        if op == "RPUSH":
            d.setdefault(key, []).append(a[0])
            return len(d[key])
        if op == "LPOP":
            return d[key].pop(0) if d.get(key) else None
        raise AssertionError(cmd)

    def count(self, op):
        return sum(1 for c in self.calls if c[0] == op)

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class UpstashTest(unittest.TestCase):
    def setUp(self):
        self.stub = StubRedis()
        self.addCleanup(self.stub.close)
        self.up = Upstash(self.stub.url + "/", "tok")

    def test_request_shape(self):
        self.up.set("k", "v", 5)
        self.assertEqual(self.stub.auth, ["Bearer tok"])
        self.assertEqual(self.stub.calls, [["SET", "k", "v", "EX", "5"]])

    def test_end_to_end(self):
        self.assertIsNone(self.up.get("k"))
        self.up.set("k", "v", 5)
        self.assertEqual(self.up.get("k"), "v")
        self.assertEqual([self.up.incr("n", 9), self.up.incr("n", 9)], [1, 2])
        self.up.push("l", "a", 9)
        self.up.push("l", "b", 9)
        self.assertEqual([self.up.pop("l", 0, every=0.05), self.up.pop("l", 0, every=0.05)], ["a", "b"])

    def test_incr_expires_once(self):
        for _ in range(3):
            self.up.incr("n", 9)
        self.assertEqual(self.stub.count("EXPIRE"), 1)

    def test_pop_gets_value_pushed_during_wait(self):
        later(0.1, lambda: self.up.push("l", "x", 9))
        self.assertEqual(self.up.pop("l", wait=2, every=0.05), "x")

    def test_pop_timeout_polls(self):
        self.assertIsNone(self.up.pop("l", wait=0.2, every=0.05))
        self.assertGreaterEqual(self.stub.count("LPOP"), 3)

    def test_error_answer(self):
        self.stub.mode = "error"
        with self.assertRaises(StoreDown):
            self.up.get("k")

    def test_non_json(self):
        self.stub.mode = "text"
        with self.assertRaises(StoreDown):
            self.up.get("k")

    def test_http_500(self):
        self.stub.mode = "500"
        with self.assertRaises(StoreDown):
            self.up.get("k")

    def test_closed_port(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with self.assertRaises(StoreDown):
            Upstash("http://127.0.0.1:%d" % port, "t").get("k")


class MakeStoreTest(unittest.TestCase):
    def make(self, **env):
        with patch.dict(os.environ, env, clear=True):
            return make_store()

    def test_kv(self):
        self.assertIsInstance(self.make(KV_REST_API_URL="http://x", KV_REST_API_TOKEN="t"), Upstash)

    def test_upstash_names(self):
        self.assertIsInstance(self.make(UPSTASH_REDIS_REST_URL="http://x", UPSTASH_REDIS_REST_TOKEN="t"), Upstash)

    def test_vercel_without_store(self):
        self.assertIsNone(self.make(VERCEL="1"))

    def test_local(self):
        self.assertIsInstance(self.make(), Memory)


CODE = "A" * 22
OTHER = "B" * 22


class PhoneTest(unittest.TestCase):
    def setUp(self):
        self.s = Memory()

    def paired(self):
        return phone.pair(self.s, "c")["code"]

    def test_pair_and_status(self):
        code = self.paired()
        self.assertRegex(code, phone.CODE)
        self.assertEqual(phone.status(self.s, code)["status"], "waiting")
        with self.assertRaises(phone.Gone):
            phone.status(self.s, CODE)
        with self.assertRaises(rules.BadRequest):
            phone.status(self.s, "bad")

    def test_rate_limit(self):
        for _ in range(phone.PAIRS_PER_HOUR):
            phone.pair(self.s, "a")
        with self.assertRaises(rules.BadRequest):
            phone.pair(self.s, "a")
        self.assertIn("code", phone.pair(self.s, "b"))

    def test_hello_accepts(self):
        code = self.paired()
        for r in ({"status": "loading", "progress": 0.5}, {"status": "ready"},
                  {"status": "failed", "detail": "no webgpu"}):
            self.assertEqual(phone.hello(self.s, {"code": code, **r}), {"ok": True})
        self.assertEqual(phone.status(self.s, code)["detail"], "no webgpu")

    def test_hello_rejects(self):
        code = self.paired()
        bad = [{"status": "nope"}, {"status": "ready", "progress": -0.1}, {"status": "ready", "progress": 1.5},
               {"status": "ready", "progress": "0.5"}, {"status": "ready", "progress": True},
               {"status": "ready", "model": 5}, {"status": "ready", "detail": 5}]
        for r in bad:
            with self.subTest(r=r), self.assertRaises(rules.BadRequest):
                phone.hello(self.s, {"code": code, **r})

    def test_hello_truncates(self):
        code = self.paired()
        phone.hello(self.s, {"code": code, "status": "ready", "model": "m" * 100, "detail": "d" * 300})
        info = phone.status(self.s, code)
        self.assertEqual((len(info["model"]), len(info["detail"])), (60, 120))

    def test_hello_gone_and_malformed(self):
        with self.assertRaises(phone.Gone):
            phone.hello(self.s, {"code": CODE, "status": "ready"})
        with self.assertRaises(rules.BadRequest):
            phone.hello(self.s, {"code": "bad", "status": "ready"})

    def test_status_alive(self):
        code = self.paired()
        phone.hello(self.s, {"code": code, "status": "ready"})
        self.assertEqual(phone.status(self.s, code)["status"], "asleep")
        with patch.object(phone, "PHONE_WAIT", 0.05):
            phone.next_job(self.s, {"code": code})
        self.assertEqual(phone.status(self.s, code)["status"], "ready")
        self.s.data.pop("alive:" + code)
        self.assertEqual(phone.status(self.s, code)["status"], "asleep")

    def test_next_job(self):
        code = self.paired()
        with patch.object(phone, "PHONE_WAIT", 0.05):
            self.assertEqual(phone.next_job(self.s, {"code": code}), {"job": None})
            self.assertIsNotNone(self.s.get("alive:" + code))
            self.s.push("jobs:" + code, json.dumps({"id": "x"}), 10)
            self.assertEqual(phone.next_job(self.s, {"code": code}), {"job": {"id": "x"}})
            with self.assertRaises(phone.Gone):
                phone.next_job(self.s, {"code": CODE})

    def test_done(self):
        code = self.paired()
        for r in ({"id": "a" * 31}, {"id": "a" * 33}, {"id": "A" * 32}, {"id": "a" * 32, "text": 5}, {"text": "x"}):
            r.setdefault("text", "x")
            with self.subTest(r=r), self.assertRaises(rules.BadRequest):
                phone.done(self.s, {"code": code, **r})
        job = "a" * 32
        phone.done(self.s, {"code": code, "id": job, "text": "t" * (phone.MAX_TEXT + 10)})
        self.assertEqual(len(self.s.pop(f"res:{code}:{job}", 0)), phone.MAX_TEXT)
        with self.assertRaises(phone.Gone):
            phone.done(self.s, {"code": CODE, "id": job, "text": "x"})


class FakePhone:
    """Plays the phone: polls for jobs and answers them."""

    def __init__(self, s, code, answer):
        self.s, self.code, self.answer, self.jobs = s, code, answer, []
        self.stop = threading.Event()
        s.set("alive:" + code, "1", 30)
        self.t = threading.Thread(target=self.loop, daemon=True)
        self.t.start()

    def loop(self):
        while not self.stop.is_set():
            job = phone.next_job(self.s, {"code": self.code})["job"]
            if job:
                self.jobs.append(job)
                phone.done(self.s, {"code": self.code, "id": job["id"], "text": self.answer(job)})

    def close(self):
        self.stop.set()
        self.t.join(2)


def scenario_answer(job):
    return json.dumps({"topic": job["schema"]["properties"]["topic"]["enum"][0]}) if job["schema"] else "Hello."


class PhoneModelTest(unittest.TestCase):
    def setUp(self):
        self.s = Memory()
        self.code = phone.pair(self.s, "c")["code"]
        p = patch.object(phone, "PHONE_WAIT", 0.05)
        p.start()
        self.addCleanup(p.stop)

    def test_round_trip(self):
        fp = FakePhone(self.s, self.code, lambda j: "pong")
        self.addCleanup(fp.close)
        self.assertEqual(phone.phone_model(self.s, self.code)("sys", "usr", None, 7), "pong")
        job = fp.jobs[0]
        self.assertEqual(set(job), {"id", "system", "user", "schema", "max_tokens"})
        self.assertEqual((job["system"], job["user"], job["max_tokens"]), ("sys", "usr", 7))

    def test_not_alive_is_immediate(self):
        t = time.monotonic()
        with self.assertRaises(rules.ModelDown):
            phone.phone_model(self.s, self.code)("s", "u", None, 1)
        self.assertLess(time.monotonic() - t, 0.2)
        self.assertIsNone(self.s.pop("jobs:" + self.code, 0))

    def test_no_answer(self):
        self.s.set("alive:" + self.code, "1", 30)
        with patch.object(phone, "ANSWER_WAIT", 0.1), self.assertRaises(rules.ModelDown):
            phone.phone_model(self.s, self.code)("s", "u", None, 1)

    def test_store_down(self):
        class Dead:
            def __getattr__(self, name):
                def f(*a, **k):
                    raise StoreDown("x")
                return f
        with self.assertRaises(rules.ModelDown):
            phone.phone_model(Dead(), self.code)("s", "u", None, 1)

    def test_isolation(self):
        other = phone.pair(self.s, "d")["code"]
        self.s.set("alive:" + self.code, "1", 30)
        # a phone answering every job under code `other` must not satisfy a call for self.code
        self.s.push("res:" + other + ":" + "a" * 32, "leak", 10)
        with patch.object(phone, "ANSWER_WAIT", 0.1), patch.object(phone.secrets, "token_hex", return_value="a" * 32):
            with self.assertRaises(rules.ModelDown):
                phone.phone_model(self.s, self.code)("s", "u", None, 1)

    def test_through_rules_ask(self):
        sc = rules.SCENARIO
        sid = next(iter(sc["suspects"]))
        r = {"action": "ask", "suspect": sid, "question": "Where were you?",
             "state": {"clues": list(sc["start_clues"]), "asked": 0}}
        fp = FakePhone(self.s, self.code, scenario_answer)
        self.addCleanup(fp.close)
        out = rules.ask(sc, r, phone.phone_model(self.s, self.code))
        self.assertEqual((out["source"], out["reply"]), ("phone", "Hello."))
        fp.close()
        self.s.data.clear()
        out = rules.ask(sc, r, phone.phone_model(self.s, self.code))
        self.assertEqual(out["source"], "script")


class FakeStoreDown:
    def __getattr__(self, name):
        def f(*a, **k):
            raise StoreDown("x")
        return f


class HttpPhoneTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = dev.make_server(0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.s = Memory()
        for p in (patch.object(api.game, "STORE", self.s), patch.object(phone, "PHONE_WAIT", 0.05)):
            p.start()
            self.addCleanup(p.stop)

    def call(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            c.request(method, path, None if body is None else json.dumps(body),
                      {"Content-Type": "application/json"})
            r = c.getresponse()
            return r.status, json.loads(r.read())
        finally:
            c.close()

    def post(self, body):
        return self.call("POST", "/api/game", body)

    def pair(self):
        return self.post({"action": "pair"})[1]["code"]

    def ask_body(self, **extra):
        view = self.call("GET", "/api/game")[1]
        sid = view["suspects"][0]["id"]
        return {"action": "ask", "suspect": sid, "question": view["leads"][sid][0]["sample"],
                "state": {"clues": [c["id"] for c in view["clues"]], "asked": 0}, **extra}

    def test_pair_and_status(self):
        st, body = self.post({"action": "pair"})
        self.assertEqual(st, 200)
        code = body["code"]
        st, body = self.call("GET", "/api/game?phone=" + code)
        self.assertEqual((st, body["status"]), (200, "waiting"))
        self.assertEqual(self.call("GET", "/api/game?phone=" + CODE)[0], 410)
        self.assertEqual(self.call("GET", "/api/game?phone=bad")[0], 400)

    def test_phone_actions(self):
        code = self.pair()
        self.assertEqual(self.post({"action": "phone_hello", "code": code, "status": "ready"}), (200, {"ok": True}))
        self.assertEqual(self.post({"action": "phone_next", "code": code}), (200, {"job": None}))
        self.assertEqual(self.call("GET", "/api/game?phone=" + code)[1]["status"], "ready")
        self.assertEqual(self.post({"action": "phone_done", "code": code, "id": "zz", "text": "x"})[0], 400)
        self.assertEqual(self.post({"action": "phone_next", "code": CODE})[0], 410)

    def test_ask_phone(self):
        code = self.pair()
        fp = FakePhone(self.s, code, scenario_answer)
        self.addCleanup(fp.close)
        st, body = self.post(self.ask_body(mode="phone", code=code))
        self.assertEqual((st, body["source"]), (200, "phone"))

    def test_ask_fallbacks(self):
        code = self.pair()
        for extra in ({"mode": "phone", "code": code}, {"mode": "scripted"}, {"mode": "phone", "code": "bad"}):
            with self.subTest(extra=extra):
                st, body = self.post(self.ask_body(**extra))
                self.assertEqual((st, body["source"]), (200, "script"))

    def test_store_none(self):
        with patch.object(api.game, "STORE", None):
            self.assertEqual(self.post({"action": "pair"})[0], 503)
            self.assertEqual(self.call("GET", "/api/game?phone=" + CODE)[0], 503)
            self.assertEqual(self.post(self.ask_body())[0], 200)

    def test_store_down(self):
        with patch.object(api.game, "STORE", FakeStoreDown()):
            st, body = self.post({"action": "pair"})
            self.assertEqual(st, 503)
            self.assertIn("error", body)

    def test_code_not_in_public_view(self):
        code = self.pair()
        self.assertNotIn(code, json.dumps(self.call("GET", "/api/game")[1]))


if __name__ == "__main__":
    unittest.main()
