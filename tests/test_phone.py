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

    def test_lock_second_fails_while_held(self):
        m = Memory()
        self.assertTrue(m.lock("l", 10))
        self.assertFalse(m.lock("l", 10))

    def test_lock_after_delete(self):
        m = Memory()
        m.lock("l", 10)
        m.delete("l")
        self.assertTrue(m.lock("l", 10))

    def test_lock_after_expiry(self):
        m = Memory()
        with patch.object(store_mod.time, "monotonic", return_value=100.0):
            self.assertTrue(m.lock("l", 10))
            self.assertFalse(m.lock("l", 10))
        with patch.object(store_mod.time, "monotonic", return_value=111.0):
            self.assertTrue(m.lock("l", 10))

    def test_delete_several_keys(self):
        m = Memory()
        for k in "abc":
            m.set(k, "1", 10)
        m.delete("a", "b", "missing")
        self.assertEqual([m.get(k) for k in "abc"], [None, None, "1"])

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
        self.data, self.calls, self.auth, self.mode, self.ttl = {}, [], [], "ok", {}
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
            opts = [str(x) for x in a[1:]]
            if "NX" in opts and key in d:
                return None
            d[key] = a[0]
            if "EX" in opts:
                self.ttl[key] = int(opts[opts.index("EX") + 1])
            else:
                self.ttl.pop(key, None)  # a plain SET drops the expiry, as in Redis
            return "OK"
        if op == "DEL":
            gone = [k for k in [key, *a] if d.pop(k, None) is not None]
            for k in gone:
                self.ttl.pop(k, None)
            return len(gone)
        if op == "INCR":
            d[key] = str(int(d.get(key, 0)) + 1)  # keeps the key's expiry
            return int(d[key])
        if op == "EXPIRE":
            return 1
        if op == "RPUSH":
            d.setdefault(key, []).append(a[0])
            return len(d[key])
        if op == "LPOP":
            return d[key].pop(0) if d.get(key) else None
        raise AssertionError(cmd)

    def expire_now(self, key):
        self.data.pop(key, None)
        self.ttl.pop(key, None)

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

    def test_incr_sends_set_nx_then_incr_and_keeps_counting(self):
        self.assertEqual([self.up.incr("n", 9) for _ in range(3)], [1, 2, 3])
        self.assertEqual(self.stub.calls[:2], [["SET", "n", "0", "EX", "9", "NX"], ["INCR", "n"]])
        self.assertEqual([c[0] for c in self.stub.calls], ["SET", "INCR"] * 3)
        self.assertEqual(self.stub.count("EXPIRE"), 0)
        self.assertEqual(self.stub.ttl["n"], 9)  # INCR did not drop the expiry

    def test_lock_second_fails_while_held(self):
        self.assertTrue(self.up.lock("l", 7))
        self.assertFalse(self.up.lock("l", 7))
        self.assertEqual(self.stub.calls[0], ["SET", "l", "1", "EX", "7", "NX"])

    def test_lock_after_delete_and_after_expiry(self):
        self.up.lock("l", 7)
        self.up.delete("l")
        self.assertTrue(self.up.lock("l", 7))
        self.stub.expire_now("l")
        self.assertTrue(self.up.lock("l", 7))

    def test_delete_several_keys(self):
        for k in "abc":
            self.up.set(k, "1", 5)
        self.up.delete("a", "b")
        self.assertEqual(self.stub.calls[-1], ["DEL", "a", "b"])
        self.assertEqual([self.up.get(k) for k in "abc"], [None, None, "1"])

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


PART = "A" * 24
CODE = PART + phone._sign(PART)  # validly signed, never paired


class PhoneTest(unittest.TestCase):
    def setUp(self):
        self.s = Memory()

    def paired(self):
        return phone.pair(self.s, "c")["code"]

    def test_pair_and_status(self):
        code = self.paired()
        self.assertRegex(code, phone.CODE)
        self.assertEqual(phone.status(self.s, {"code": code})["status"], "waiting")
        with self.assertRaises(phone.Gone):
            phone.status(self.s, {"code": CODE})
        with self.assertRaises(rules.BadRequest):
            phone.status(self.s, {"code": "bad"})

    def test_status_has_no_ends_and_has_last_ms(self):
        info = phone.status(self.s, {"code": self.paired()})
        self.assertNotIn("ends", info)
        self.assertIn("last_ms", info)

    def test_made_up_strings_are_bad_request(self):
        unpaired_but_wellformed = "x" * 24 + "0" * 16
        for bad in ("x" * 20, "", "bad", unpaired_but_wellformed, None, 5, ["a"], CODE + "0", CODE[:-1]):
            for fn in (phone.status, phone.bye, phone.hello, phone.next_job, phone.done):
                with self.subTest(code=bad, fn=fn.__name__), self.assertRaises(rules.BadRequest):
                    fn(self.s, {"code": bad, "status": "ready", "id": "a" * 32, "text": "x"})
        with self.assertRaises(rules.BadRequest):
            phone.status(self.s, {})

    def test_bad_code_never_touches_store(self):
        with self.assertRaises(rules.BadRequest):
            phone.status(FakeStoreDown(), {"code": "x" * 20})

    def test_changed_last_hex_char_is_rejected(self):
        code = self.paired()
        self.assertTrue(phone.valid(code))
        forged = code[:-1] + ("0" if code[-1] != "0" else "1")
        self.assertFalse(phone.valid(forged))
        with self.assertRaises(rules.BadRequest):
            phone.status(self.s, {"code": forged})

    def test_valid_is_false_for_non_strings(self):
        for x in (None, 5, 1.5, b"A" * 40, ["A" * 40], {"code": 1}, True):
            self.assertFalse(phone.valid(x))
        self.assertTrue(phone.valid(CODE))

    def test_bye_deletes_pairing(self):
        code = self.paired()
        phone.hello(self.s, {"code": code, "status": "ready"})
        self.assertEqual(phone.bye(self.s, {"code": code}), {"ok": True})
        self.assertIsNone(self.s.get("pair:" + code))
        calls = (phone.status, phone.hello, phone.next_job, phone.done)
        for fn in calls:
            with self.subTest(fn=fn.__name__), self.assertRaises(phone.Gone):
                fn(self.s, {"code": code, "status": "ready", "id": "a" * 32, "text": "x"})

    def test_bye_bad_code_and_unknown_code(self):
        with self.assertRaises(rules.BadRequest):
            phone.bye(self.s, {"code": "bad"})
        self.assertEqual(phone.bye(self.s, {"code": CODE}), {"ok": True})

    def test_pair_writes_end_time(self):
        with patch.object(phone.time, "time", return_value=1000.0):
            code = self.paired()
        self.assertEqual(json.loads(self.s.get("pair:" + code))["ends"], 1000.0 + phone.PAIR_TTL)

    def test_pairing_gone_after_end_even_if_key_exists(self):
        code = self.paired()
        ends = json.loads(self.s.get("pair:" + code))["ends"]
        self.assertIsNotNone(self.s.get("pair:" + code))
        with patch.object(phone.time, "time", return_value=ends + 1):
            for fn in (phone.status, phone.hello, phone.next_job, phone.done):
                with self.subTest(fn=fn.__name__), self.assertRaises(phone.Gone):
                    fn(self.s, {"code": code, "status": "ready", "id": "a" * 32, "text": "x"})
        self.assertEqual(phone.status(self.s, {"code": code})["status"], "waiting")

    def test_hello_does_not_extend_end_time(self):
        code = self.paired()
        ends = json.loads(self.s.get("pair:" + code))["ends"]
        with patch.object(phone.time, "time", return_value=ends - 100):
            phone.hello(self.s, {"code": code, "status": "ready"})
        self.assertEqual(json.loads(self.s.get("pair:" + code))["ends"], ends)

    def test_pairs_per_hour_is_ten(self):
        self.assertEqual(phone.PAIRS_PER_HOUR, 10)

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
        self.assertEqual(phone.status(self.s, {"code": code})["detail"], "no webgpu")

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
        info = phone.status(self.s, {"code": code})
        self.assertEqual((len(info["model"]), len(info["detail"])), (60, 120))

    def test_hello_gone_and_malformed(self):
        with self.assertRaises(phone.Gone):
            phone.hello(self.s, {"code": CODE, "status": "ready"})
        with self.assertRaises(rules.BadRequest):
            phone.hello(self.s, {"code": "bad", "status": "ready"})

    def test_status_alive(self):
        code = self.paired()
        phone.hello(self.s, {"code": code, "status": "ready"})
        self.assertEqual(phone.status(self.s, {"code": code})["status"], "asleep")
        with patch.object(phone, "PHONE_WAIT", 0.05):
            phone.next_job(self.s, {"code": code})
        self.assertEqual(phone.status(self.s, {"code": code})["status"], "ready")
        self.s.data.pop("alive:" + code)
        self.assertEqual(phone.status(self.s, {"code": code})["status"], "asleep")

    def test_next_job(self):
        code = self.paired()
        with patch.object(phone, "PHONE_WAIT", 0.05):
            self.assertEqual(phone.next_job(self.s, {"code": code}), {"job": None})
            self.assertIsNotNone(self.s.get("alive:" + code))
            self.s.push("jobs:" + code, json.dumps({"id": "x", "deadline": time.time() + 30}), 10)
            self.assertEqual(phone.next_job(self.s, {"code": code}), {"job": {"id": "x"}})
            with self.assertRaises(phone.Gone):
                phone.next_job(self.s, {"code": CODE})

    def test_second_concurrent_poll_sleeps_and_returns_none(self):
        code = self.paired()
        self.push_job(code, "x", 30)
        self.assertTrue(self.s.lock("poll:" + code, 30))
        with patch.object(phone.time, "sleep") as sl:
            self.assertEqual(phone.next_job(self.s, {"code": code}), {"job": None})
        sl.assert_called_once_with(2)
        self.assertIsNotNone(self.s.get("jobs:" + code))  # the job was left for the poll that holds the lock
        self.assertIsNotNone(self.s.get("poll:" + code))  # and the holder's lock is untouched

    def test_poll_lock_released_when_poll_ends(self):
        code = self.paired()
        with patch.object(phone, "PHONE_WAIT", 0.05):
            phone.next_job(self.s, {"code": code})
            self.assertIsNone(self.s.get("poll:" + code))
            self.push_job(code, "x", 30)
            self.assertEqual(phone.next_job(self.s, {"code": code}), {"job": {"id": "x"}})
            self.assertIsNone(self.s.get("poll:" + code))

    def push_job(self, code, id, ahead):
        self.s.push("jobs:" + code, json.dumps({"id": id, "deadline": time.time() + ahead}), 10)

    def test_next_job_skips_stale_and_returns_fresh(self):
        code = self.paired()
        self.push_job(code, "old", -5)
        self.push_job(code, "new", 30)
        with patch.object(phone, "PHONE_WAIT", 0.05):
            self.assertEqual(phone.next_job(self.s, {"code": code}), {"job": {"id": "new"}})

    def test_next_job_skips_job_with_less_than_min_left(self):
        code = self.paired()
        self.push_job(code, "tight", 1)
        with patch.object(phone, "PHONE_WAIT", 0.05), patch.object(phone, "MIN_LEFT", 4):
            self.assertEqual(phone.next_job(self.s, {"code": code}), {"job": None})

    def test_next_job_has_no_deadline_key(self):
        code = self.paired()
        self.push_job(code, "x", 30)
        with patch.object(phone, "PHONE_WAIT", 0.05):
            self.assertNotIn("deadline", phone.next_job(self.s, {"code": code})["job"])

    def test_done_ms_stored_in_status(self):
        code = self.paired()
        self.assertIsNone(phone.status(self.s, {"code": code})["last_ms"])
        phone.done(self.s, {"code": code, "id": "a" * 32, "text": "x", "ms": 1234})
        self.assertEqual(phone.status(self.s, {"code": code})["last_ms"], 1234)

    def test_done_bad_ms(self):
        code = self.paired()
        for ms in (-1, 700000, "12", True, 1.5):
            with self.subTest(ms=ms), self.assertRaises(rules.BadRequest):
                phone.done(self.s, {"code": code, "id": "a" * 32, "text": "x", "ms": ms})

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

    def test_calls_share_one_deadline(self):
        self.s.set("alive:" + self.code, "1", 30)
        fp = FakePhone(self.s, self.code, lambda j: "pong")
        self.addCleanup(fp.close)
        with patch.object(phone, "QUESTION_BUDGET", 0.4), patch.object(phone, "MIN_LEFT", 0.3):
            model = phone.phone_model(self.s, self.code)
            time.sleep(0.2)  # first call's share of the budget
            t = time.monotonic()
            with self.assertRaises(rules.ModelDown) as c:
                model("s", "u", None, 1)
            self.assertEqual(str(c.exception), "slow")
            self.assertLess(time.monotonic() - t, 0.2)

    def test_not_alive_is_immediate(self):
        t = time.monotonic()
        with self.assertRaises(rules.ModelDown):
            phone.phone_model(self.s, self.code)("s", "u", None, 1)
        self.assertLess(time.monotonic() - t, 0.2)
        self.assertIsNone(self.s.pop("jobs:" + self.code, 0))

    def test_no_answer(self):
        self.s.set("alive:" + self.code, "1", 30)
        with patch.object(phone, "QUESTION_BUDGET", 0.3), patch.object(phone, "MIN_LEFT", 0.05), self.assertRaises(rules.ModelDown):
            phone.phone_model(self.s, self.code)("s", "u", None, 1)

    def alive(self):
        self.s.set("alive:" + self.code, "1", 30)

    def test_busy_when_another_question_holds_the_lock(self):
        self.alive()
        with patch.object(phone, "QUESTION_BUDGET", 30), patch.object(phone, "MIN_LEFT", 60):
            first = phone.phone_model(self.s, self.code)
            second = phone.phone_model(self.s, self.code)
            with self.assertRaises(rules.ModelDown):
                first("s", "u", None, 1)  # takes the lock, then fails as too slow
        with self.assertRaises(rules.ModelDown) as c:
            second("s", "u", None, 1)
        self.assertEqual(str(c.exception), "busy")
        self.assertIsNotNone(self.s.get("busy:" + self.code))

    def test_release_frees_the_code(self):
        self.alive()
        fp = FakePhone(self.s, self.code, lambda j: "pong")
        self.addCleanup(fp.close)
        first = phone.phone_model(self.s, self.code)
        self.assertEqual(first("s", "u", None, 1), "pong")
        busy = phone.phone_model(self.s, self.code)
        with self.assertRaises(rules.ModelDown) as c:
            busy("s", "u", None, 1)
        self.assertEqual(str(c.exception), "busy")
        first.release()
        self.assertIsNone(self.s.get("busy:" + self.code))
        self.assertEqual(phone.phone_model(self.s, self.code)("s", "u", None, 1), "pong")

    def test_lock_taken_once_for_several_calls(self):
        self.alive()
        fp = FakePhone(self.s, self.code, lambda j: "pong")
        self.addCleanup(fp.close)
        model = phone.phone_model(self.s, self.code)
        self.assertEqual([model("s", "u", None, 1), model("s", "u", None, 1)], ["pong", "pong"])
        model.release()

    def test_release_is_noop_if_nothing_locked(self):
        self.s.set("busy:" + self.code, "1", 30)  # someone else's lock
        phone.phone_model(self.s, self.code).release()
        self.assertIsNotNone(self.s.get("busy:" + self.code))

    def test_release_swallows_store_down(self):
        self.alive()
        with patch.object(phone, "QUESTION_BUDGET", 30), patch.object(phone, "MIN_LEFT", 60):
            model = phone.phone_model(self.s, self.code)
            with self.assertRaises(rules.ModelDown):
                model("s", "u", None, 1)
        self.assertIsNotNone(self.s.get("busy:" + self.code))  # locked now; make the store fail on delete
        with patch.object(self.s, "delete", side_effect=StoreDown("x")):
            model.release()

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
        with patch.object(phone, "QUESTION_BUDGET", 0.3), patch.object(phone, "MIN_LEFT", 0.05), patch.object(phone.secrets, "token_hex", return_value="a" * 32):
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


class ModelRecursionTest(unittest.TestCase):
    def test_deeply_nested_model_output_falls_back(self):
        sc = rules.SCENARIO
        sid = next(iter(sc["suspects"]))
        r = {"action": "ask", "suspect": sid, "question": "zzz qqq",
             "state": {"clues": list(sc["start_clues"]), "asked": 0}}
        out = rules.ask(sc, r, lambda *a: "[" * 100000 + "]" * 100000)
        self.assertEqual(out["source"], "script")
        topics = sc["suspects"][sid]["topics"]
        with self.assertRaises(rules.ModelDown):
            rules.model_topic(topics, "q", lambda *a: "[" * 100000)


PART = "A" * 24
PCODE = PART + phone._sign(PART)
J1, J2 = "1" * 32, "2" * 32


class PartPeekTest(unittest.TestCase):
    def setUp(self):
        self.s = Memory()

    def req(self, **k):
        return {"code": PCODE, **k}

    def peek(self):
        return phone.peek(self.s, {"code": PCODE})

    def test_current_job_stored_other_ignored(self):
        self.assertEqual(self.peek(), {"text": None})
        self.s.set("cur:" + PCODE, J1, 60)
        self.assertEqual(phone.part(self.s, self.req(id=J1, text="Hel")), {"ok": True})
        self.assertEqual(self.peek(), {"text": "Hel"})
        self.assertEqual(phone.part(self.s, self.req(id=J2, text="other")), {"ok": True})
        self.assertEqual(self.peek(), {"text": "Hel"})

    def test_other_job_without_previous_text_is_none(self):
        self.s.set("cur:" + PCODE, J1, 60)
        phone.part(self.s, self.req(id=J2, text="x"))
        self.assertEqual(self.peek(), {"text": None})

    def test_truncates(self):
        self.s.set("cur:" + PCODE, J1, 60)
        phone.part(self.s, self.req(id=J1, text="x" * (phone.MAX_PART + 50)))
        self.assertEqual(self.peek(), {"text": "x" * phone.MAX_PART})

    def test_part_validation(self):
        bad = [self.req(id="A" * 32, text="x"), self.req(id="1" * 31, text="x"), self.req(id=5, text="x"),
               self.req(text="x"), self.req(id=J1, text=5), self.req(id=J1), {"code": "bad", "id": J1, "text": "x"},
               {"id": J1, "text": "x"}]
        for r in bad:
            with self.subTest(r=r), self.assertRaises(rules.BadRequest):
                phone.part(self.s, r)

    def test_peek_invalid_code(self):
        for c in ("bad", None, PART + "0" * 16):
            with self.subTest(c=c), self.assertRaises(rules.BadRequest):
                phone.peek(self.s, {"code": c})

    def run_model(self, schema, act):
        """Run one model call while `act(job)` plays the phone; returns the model's result."""
        self.s.set("pair:" + PCODE, json.dumps({"ends": time.time() + 99}), 99)
        self.s.set("alive:" + PCODE, "1", 30)

        def phone_side():
            act(phone.next_job(self.s, {"code": PCODE})["job"])

        with patch.object(phone, "PHONE_WAIT", 2), patch.object(phone, "QUESTION_BUDGET", 5):
            t = threading.Thread(target=phone_side, daemon=True)
            t.start()
            model = phone.phone_model(self.s, PCODE)
            self.addCleanup(model.release)
            out = model("s", "u", schema, 9)
            t.join(2)
        return out

    def test_model_schema_job_leaves_cur_and_part(self):
        self.s.set("part:" + PCODE, "old", 60)
        seen = []

        def act(job):
            seen.extend([self.s.get("cur:" + PCODE), self.peek()["text"]])
            phone.done(self.s, self.req(id=job["id"], text="{}"))

        self.assertEqual(self.run_model({"type": "object"}, act), "{}")
        self.assertEqual(seen, [None, "old"])

    def test_model_reply_job_sets_cur_clears_part_end_to_end(self):
        self.s.set("part:" + PCODE, "old question", 60)
        seen = []

        def act(job):
            seen.append(self.s.get("cur:" + PCODE) == job["id"])
            seen.append(self.peek()["text"])
            phone.part(self.s, self.req(id=job["id"], text="He"))
            seen.append(self.peek()["text"])
            phone.part(self.s, self.req(id=job["id"], text="Hello wor"))
            seen.append(self.peek()["text"])
            phone.done(self.s, self.req(id=job["id"], text="Hello world"))

        self.assertEqual(self.run_model(None, act), "Hello world")
        self.assertEqual(seen, [True, None, "He", "Hello wor"])

    def test_stale_part_after_new_reply_job(self):
        self.s.set("alive:" + PCODE, "1", 30)
        ids = []
        for _ in range(2):
            with patch.object(phone, "QUESTION_BUDGET", 0.3), patch.object(phone, "MIN_LEFT", 0.05):
                model = phone.phone_model(self.s, PCODE)
                with self.assertRaises(rules.ModelDown):
                    model("s", "u", None, 5)
                model.release()
            ids.append(json.loads(self.s.pop("jobs:" + PCODE, 0))["id"])
        old, new = ids
        self.assertNotEqual(old, new)
        phone.part(self.s, self.req(id=new, text="new text"))
        phone.part(self.s, self.req(id=old, text="stale"))
        self.assertEqual(self.peek(), {"text": "new text"})


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
        st, body = self.post({"action": "phone_status", "code": code})
        self.assertEqual((st, body["status"]), (200, "waiting"))
        self.assertNotIn("ends", body)
        self.assertEqual(self.post({"action": "phone_status", "code": CODE})[0], 410)
        self.assertEqual(self.post({"action": "phone_status", "code": "bad"})[0], 400)

    def test_get_phone_query_is_just_the_public_view(self):
        code = self.pair()
        self.assertEqual(self.call("GET", "/api/game?phone=x"), self.call("GET", "/api/game"))
        st, body = self.call("GET", "/api/game?phone=" + code)
        self.assertEqual(st, 200)
        self.assertNotIn("status", body)
        self.assertIn("suspects", body)

    def test_phone_bye(self):
        code = self.pair()
        self.assertEqual(self.post({"action": "phone_bye", "code": code}), (200, {"ok": True}))
        for action in ("phone_status", "phone_hello", "phone_next", "phone_done"):
            with self.subTest(action=action):
                st = self.post({"action": action, "code": code, "status": "ready", "id": "a" * 32, "text": "x"})[0]
                self.assertEqual(st, 410)

    def test_invalid_code_is_400_before_rate_counter(self):
        for action in ("phone_status", "phone_bye", "phone_hello", "phone_next", "phone_done"):
            with self.subTest(action=action):
                self.assertEqual(self.post({"action": action, "code": "x" * 20})[0], 400)
        self.assertFalse([k for k in self.s.data if k.startswith("rate:req:")])

    def test_request_limit_returns_429(self):
        code = self.pair()  # 1st counted request
        with patch.object(api.game, "REQUESTS_PER_MINUTE", 2):
            self.assertEqual(self.post({"action": "phone_status", "code": code})[0], 200)
            st, body = self.post({"action": "phone_status", "code": code})
            self.assertEqual(st, 429)
            self.assertIn("error", body)

    def test_request_limit_counts_pair_and_phone_asks(self):
        with patch.object(api.game, "REQUESTS_PER_MINUTE", 2):
            code = self.pair()
            self.assertEqual(self.post(self.ask_body(mode="phone", code=code))[0], 200)
            self.assertEqual(self.post(self.ask_body(mode="phone", code=code))[0], 429)

    def test_scripted_ask_never_touches_store(self):
        class Raises:
            def __getattr__(self, name):
                raise AssertionError("store touched: " + name)
        with patch.object(api.game, "STORE", Raises()):
            for extra in ({"mode": "scripted"}, {}, {"mode": "phone", "code": "bad"}):
                with self.subTest(extra=extra):
                    st, body = self.post(self.ask_body(**extra))
                    self.assertEqual((st, body["source"]), (200, "script"))

    def test_deeply_nested_json_is_400(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            raw = "[" * 2000 + "]" * 2000
            c.request("POST", "/api/game", raw, {"Content-Type": "application/json"})
            r = c.getresponse()
            self.assertEqual(r.status, 400)
            self.assertIn("error", json.loads(r.read()))
        finally:
            c.close()

    def test_phone_actions(self):
        code = self.pair()
        self.assertEqual(self.post({"action": "phone_hello", "code": code, "status": "ready"}), (200, {"ok": True}))
        self.assertEqual(self.post({"action": "phone_next", "code": code}), (200, {"job": None}))
        self.assertEqual(self.post({"action": "phone_status", "code": code})[1]["status"], "ready")
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
            self.assertEqual(self.post({"action": "phone_status", "code": CODE})[0], 503)
            self.assertEqual(self.post(self.ask_body())[0], 200)

    def test_store_down(self):
        with patch.object(api.game, "STORE", FakeStoreDown()):
            st, body = self.post({"action": "pair"})
            self.assertEqual(st, 503)
            self.assertIn("error", body)

    def test_code_not_in_public_view(self):
        code = self.pair()
        self.assertNotIn(code, json.dumps(self.call("GET", "/api/game")[1]))

    def test_part_peek_http(self):
        self.s.set("cur:" + PCODE, J1, 60)
        self.assertEqual(self.post({"action": "phone_part", "code": PCODE, "id": J1, "text": "hi"}), (200, {"ok": True}))
        self.assertEqual(self.post({"action": "phone_peek", "code": PCODE}), (200, {"text": "hi"}))
        for body in ({"action": "phone_part", "id": J1, "text": "x"}, {"action": "phone_peek"}):
            with self.subTest(body=body):
                self.assertEqual(self.post({**body, "code": "x" * 20})[0], 400)

    def test_part_peek_count_against_limit(self):
        with patch.object(api.game, "REQUESTS_PER_MINUTE", 2):
            self.assertEqual(self.post({"action": "phone_part", "code": PCODE, "id": J1, "text": "x"})[0], 200)
            self.assertEqual(self.post({"action": "phone_peek", "code": PCODE})[0], 200)
            self.assertEqual(self.post({"action": "phone_peek", "code": PCODE})[0], 429)
            self.assertEqual(self.post({"action": "phone_part", "code": PCODE, "id": J1, "text": "x"})[0], 429)


if __name__ == "__main__":
    unittest.main()
