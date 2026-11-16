"""Tiny key-value store shared by the player's computer and the lending phone.

On Vercel every request may run in a different instance, so the store is Upstash
Redis over its REST API. Locally (dev.py, tests) it is an in-process dict.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request


class StoreDown(Exception):
    pass


class Upstash:
    def __init__(self, url, token):
        self.url, self.token = url.rstrip("/"), token

    def _call(self, *command, wait=0):
        req = urllib.request.Request(self.url, data=json.dumps([str(c) for c in command]).encode(), headers={
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=wait + 5) as res:
                body = json.loads(res.read(1_000_000))
        except (OSError, ValueError) as e:
            if isinstance(e, urllib.error.HTTPError):
                e.close()
            raise StoreDown(str(e)) from e
        if not isinstance(body, dict) or "error" in body:
            raise StoreDown(str(body))
        return body.get("result")

    def get(self, key):
        return self._call("GET", key)

    def set(self, key, value, ttl):
        self._call("SET", key, value, "EX", ttl)

    def incr(self, key, ttl):
        # Created with its expiry first: a counter can never outlive its window if a call fails.
        self._call("SET", key, 0, "EX", ttl, "NX")
        return self._call("INCR", key)

    def lock(self, key, ttl):
        """True if the key was free; it is then held for `ttl` seconds."""
        return self._call("SET", key, 1, "EX", ttl, "NX") is not None

    def delete(self, *keys):
        self._call("DEL", *keys)

    def push(self, key, value, ttl):
        self._call("RPUSH", key, value)
        self._call("EXPIRE", key, ttl)

    def pop(self, key, wait, every=1.0):
        """Wait up to `wait` seconds for a value, checking every `every` seconds; None on timeout.

        The REST API has no blocking pop, so this polls; each check is one billed command.
        """
        end = time.monotonic() + wait
        while True:
            value = self._call("LPOP", key)
            if value is not None or time.monotonic() + every > end:
                return value
            time.sleep(every)


class Memory:
    """Same interface for a single process. Values expire like Redis keys."""

    def __init__(self):
        self.data = {}  # key -> (expires at, value or list)
        self.cond = threading.Condition()

    def _live(self, key):
        item = self.data.get(key)
        if item and item[0] < time.monotonic():
            del self.data[key]
            return None
        return item

    def get(self, key):
        with self.cond:
            item = self._live(key)
            return item[1] if item else None

    def set(self, key, value, ttl):
        with self.cond:
            self.data[key] = (time.monotonic() + ttl, value)

    def incr(self, key, ttl):
        with self.cond:
            item = self._live(key)
            n = (int(item[1]) if item else 0) + 1
            self.data[key] = (item[0] if item else time.monotonic() + ttl, str(n))
            return n

    def lock(self, key, ttl):
        with self.cond:
            if self._live(key):
                return False
            self.data[key] = (time.monotonic() + ttl, "1")
            return True

    def delete(self, *keys):
        with self.cond:
            for key in keys:
                self.data.pop(key, None)

    def push(self, key, value, ttl):
        with self.cond:
            item = self._live(key)
            items = item[1] if item else []
            self.data[key] = (time.monotonic() + ttl, items + [value])
            self.cond.notify_all()

    def pop(self, key, wait, every=None):
        end = time.monotonic() + wait
        with self.cond:
            while True:
                item = self._live(key)
                if item and item[1]:
                    return item[1].pop(0)
                left = end - time.monotonic()
                if left <= 0:
                    return None
                self.cond.wait(left)


def make_store():
    """Upstash when configured, a local dict otherwise, None on Vercel without a store."""
    url = os.environ.get("KV_REST_API_URL") or os.environ.get("UPSTASH_REDIS_REST_URL")
    token = os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    if url and token:
        return Upstash(url, token)
    if os.environ.get("VERCEL"):
        return None  # each Vercel instance would have its own dict: the phone mode cannot work
    return Memory()
