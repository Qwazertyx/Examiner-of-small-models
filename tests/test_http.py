import http.client
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dev  # noqa: E402


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = patch.dict(os.environ)
        cls.env.start()
        os.environ.pop("PHONE_URL", None)
        os.environ.pop("PHONE_TOKEN", None)
        os.environ["SCRIPTED"] = "1"
        cls.srv = dev.make_server(0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.env.stop()

    def call(self, method, path, body=None, ctype=None):
        headers = {"Content-Type": ctype} if ctype else {}
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            c.request(method, path, body, headers)
            r = c.getresponse()
            return r.status, r, r.read()
        finally:
            c.close()

    def ask_body(self):
        view = json.loads(self.call("GET", "/api/game")[2])
        return json.dumps({
            "action": "ask", "suspect": view["suspects"][0]["id"],
            "question": "Where were you this morning?",
            "state": {"clues": [c["id"] for c in view["clues"]], "asked": 0},
        }).encode()

    def test_get(self):
        status, r, body = self.call("GET", "/api/game")
        self.assertEqual(status, 200)
        self.assertNotIn("culprit", json.loads(body))
        self.assertNotIn(b"culprit", body)
        self.assertEqual(r.getheader("Cache-Control"), "no-store")
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")

    def test_post_ok(self):
        for ctype in ("application/json", "application/json; charset=utf-8"):
            with self.subTest(ctype):
                status, _, body = self.call("POST", "/api/game", self.ask_body(), ctype)
                self.assertEqual(status, 200)
                self.assertIn("reply", json.loads(body))

    def test_wrong_content_type(self):
        for ctype in ("text/plain", "application/x-www-form-urlencoded"):
            with self.subTest(ctype):
                self.assertEqual(self.call("POST", "/api/game", self.ask_body(), ctype)[0], 415)

    def test_body_too_large(self):
        self.assertEqual(self.call("POST", "/api/game", b" " * 5000, "application/json")[0], 413)

    def test_secrets_not_downloadable(self):
        for path in ("/game/scenario.json", "/game/rules.py", "/../game/scenario.json",
                     "/%2e%2e/game/scenario.json", "/tests/case.json"):
            with self.subTest(path):
                status, _, body = self.call("GET", path)
                self.assertEqual(status, 404)
                self.assertNotIn(b"culprit", body)


if __name__ == "__main__":
    unittest.main()
