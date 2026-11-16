"""Local dev server: `python3 dev.py`, then open http://127.0.0.1:8000

Serves public/ and routes /api/* to the same code Vercel runs. Without Upstash
variables the phone pairing lives in memory, which is enough on one machine.
"""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from api.game import serve


class Dev(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/"):
            serve(self)
        else:
            super().do_GET()

    def do_POST(self):
        serve(self)


def make_server(port=8000):
    public = str(Path(__file__).parent / "public")
    return ThreadingHTTPServer(("127.0.0.1", port), partial(Dev, directory=public))


if __name__ == "__main__":
    print("http://127.0.0.1:8000")
    make_server().serve_forever()
