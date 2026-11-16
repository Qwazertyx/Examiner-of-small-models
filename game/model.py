"""Client for the phone relay (phone/relay.py)."""
import hashlib
import http.client
import json
import os
import time
import urllib.error
import urllib.request

from .rules import ModelDown

TIMEOUT = 25  # seconds per call; a question makes two calls and the function has 60 s
HEALTH_TIMEOUT = 4
HEALTH_CACHE = 5  # seconds; keeps a crowd of waiting pages from pinging the phone on every poll

_FAILURES = (OSError, http.client.HTTPException, ValueError, KeyError, TypeError, AttributeError)
_health = (float("-inf"), False)  # (time checked, result)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A redirect would carry the bearer token to another host.
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _config():
    """(url, token) of the relay, or (None, None) when it is missing or unsafe."""
    url = os.environ.get("PHONE_URL", "").rstrip("/")
    token = os.environ.get("PHONE_TOKEN", "")
    local = url.startswith(("http://127.0.0.1:", "http://localhost:"))
    if not token or not (url.startswith("https://") or local):
        return None, None  # never send the token over plain http to a remote host
    return url, token


def _fetch(req, timeout, limit):
    try:
        with _opener.open(req, timeout=timeout) as res:
            return json.loads(res.read(limit))
    except _FAILURES as e:
        if isinstance(e, urllib.error.HTTPError):
            e.close()
        raise ModelDown(str(e)) from e


def phone_ready():
    """True when the relay answers and says its model server is up."""
    global _health
    now = time.monotonic()
    if now - _health[0] < HEALTH_CACHE:
        return _health[1]
    url, token = _config()
    ready = False
    if url:
        req = urllib.request.Request(url + "/health", headers={"Authorization": "Bearer " + token})
        try:
            ready = _fetch(req, HEALTH_TIMEOUT, 4096).get("ok") is True
        except (ModelDown, AttributeError):
            ready = False
    _health = (now, ready)
    return ready


def phone_model(client_ip):
    """Return model(system, user, schema, max_tokens) -> str, or None when no phone is configured."""
    url, token = _config()
    if not url:
        return None

    # The relay rate-limits per player; it gets a hash, not the IP address.
    client = hashlib.sha256((token + client_ip).encode()).hexdigest()[:16]

    def model(system, user, schema, max_tokens):
        body = json.dumps({"system": system, "user": user, "schema": schema, "max_tokens": max_tokens})
        req = urllib.request.Request(url + "/chat", data=body.encode(), headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "X-Client": client,
        })
        try:
            return _fetch(req, TIMEOUT, 65536)["text"]
        except _FAILURES as e:
            raise ModelDown(str(e)) from e

    return model
