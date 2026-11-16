import http.client
import json
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phone import relay  # noqa: E402

TOKEN = "t" * 40
GOOD = {"system": "s", "user": "u", "schema": None, "max_tokens": 20}


class RelayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = relay.make_server("127.0.0.1", 0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.calls = []

        def gen(*args):
            self.calls.append(args)
            return "hello"

        for p in (patch.object(relay, "TOKEN", TOKEN), patch.object(relay, "generate", gen)):
            p.start()
            self.addCleanup(p.stop)
        relay.hits.clear()

    def post(self, body=GOOD, path="/chat", auth="Bearer " + TOKEN, client=None, raw=None):
        data = raw if raw is not None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if auth is not None:
            headers["Authorization"] = auth
        if client:
            headers["X-Client"] = client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            c.request("POST", path, data, headers)
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    def test_auth(self):
        for auth in (None, "Bearer " + "x" * 40, TOKEN):
            with self.subTest(auth=auth):
                self.assertEqual(self.post(auth=auth)[0], 401)
        self.assertEqual(self.calls, [])

    def test_ok(self):
        status, body = self.post()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"text": "hello"})
        self.assertEqual(self.calls, [("s", "u", None, 20)])

    def test_wrong_path(self):
        self.assertEqual(self.post(path="/other")[0], 404)

    def test_max_tokens_capped(self):
        self.assertEqual(self.post(dict(GOOD, max_tokens=5000))[0], 200)
        self.assertEqual(self.calls[0][3], relay.MAX_TOKENS)

    def test_bad_bodies(self):
        no_user = {k: v for k, v in GOOD.items() if k != "user"}
        cases = {
            "invalid json": dict(raw=b"{nope"),
            "missing user": dict(body=no_user),
            "system not str": dict(body=dict(GOOD, system=1)),
            "schema str": dict(body=dict(GOOD, schema="x")),
            "max_tokens 0": dict(body=dict(GOOD, max_tokens=0)),
            "max_tokens str": dict(body=dict(GOOD, max_tokens="10")),
            "max_tokens bool": dict(raw=b'{"system":"s","user":"u","max_tokens":true}'),
        }
        for name, kw in cases.items():
            with self.subTest(name):
                self.assertEqual(self.post(**kw)[0], 400)
        self.assertEqual(self.calls, [])

    def test_body_too_large(self):
        self.assertEqual(self.post(raw=b"x" * (relay.MAX_BODY + 1))[0], 413)
        self.assertEqual(self.calls, [])

    def test_rate_limit(self):
        for i in range(relay.PER_MINUTE):
            self.assertEqual(self.post(client="a")[0], 200, i)
        self.assertEqual(self.post(client="a")[0], 429)
        self.assertEqual(self.post(client="b")[0], 200)

    def test_busy(self):
        self.assertTrue(relay.busy.acquire(timeout=1))
        try:
            with patch.object(relay, "BUSY_WAIT", 0.05):
                self.assertEqual(self.post()[0], 503)
        finally:
            relay.busy.release()
        self.assertEqual(self.calls, [])

    def test_generate_failure_releases_lock(self):
        with patch.object(relay, "generate", side_effect=OSError("down")):
            self.assertEqual(self.post()[0], 502)
        self.assertFalse(relay.busy.locked())
        self.assertEqual(self.post()[0], 200)

    def test_get_refused(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            c.request("GET", "/chat", headers={"Authorization": "Bearer " + TOKEN})
            self.assertNotEqual(c.getresponse().status, 200)
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
