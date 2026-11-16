"""Lending phone: a visitor scans a QR code and their phone runs the model in its browser.

The computer and the phone share a random pairing code. Questions go to the phone
through a job list in the store, and answers come back through a per-job list.
A phone only ever serves the one game whose code it scanned.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import time

from .rules import BadRequest, ModelDown
from .store import StoreDown

PAIR_TTL = 3 * 3600   # a pairing lives for one sitting, and is never extended
ALIVE_TTL = 30        # the phone counts as connected for this long after its last poll
JOB_TTL = 120
PHONE_WAIT = 20       # long poll held open for the phone; Vercel function limit is 60 s
QUESTION_BUDGET = 45  # seconds for all model calls of one question; the function limit is 60 s
MIN_LEFT = 4          # a job with less time left than this is dropped: the phone could not finish it
PAIRS_PER_HOUR = 10   # per player address; one sitting needs one
MAX_TEXT = 1500

CODE = re.compile(r"[A-Za-z0-9_-]{24}[0-9a-f]{16}")
JOB_ID = re.compile(r"[0-9a-f]{32}")
STATUSES = {"loading", "ready", "failed"}

# Codes are signed, so a guessed code is refused before it costs a store command.
_KEY = (os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_REST_TOKEN")
        or secrets.token_hex(32)).encode()


class Gone(Exception):
    """Unknown or expired pairing code: the game this phone served has ended."""


def _sign(part):
    return hmac.new(_KEY, part.encode(), hashlib.sha256).hexdigest()[:16]


def valid(code):
    return isinstance(code, str) and CODE.fullmatch(code) is not None and hmac.compare_digest(
        code[24:], _sign(code[:24]))


def _code(req):
    code = req.get("code")
    if not valid(code):
        raise BadRequest("Invalid pairing code.")
    return code


def _pairing(store, code):
    raw = store.get("pair:" + code)
    if raw is None:
        raise Gone()
    info = json.loads(raw)
    if time.time() > info["ends"]:
        raise Gone()
    return info


def pair(store, client):
    """Computer side: open a new pairing. `client` is a hash of the player's address."""
    if store.incr("rate:pair:" + client, 3600) > PAIRS_PER_HOUR:
        raise BadRequest("Too many new phone codes. Wait a while, or play with written lines.")
    part = secrets.token_urlsafe(18)
    code = part + _sign(part)
    info = {"status": "waiting", "progress": 0, "model": "", "detail": "", "ends": time.time() + PAIR_TTL}
    store.set("pair:" + code, json.dumps(info), PAIR_TTL)
    return {"code": code}


def status(store, req):
    """Computer side: what the phone reported, and whether it is polling right now."""
    code = _code(req)
    info = _pairing(store, code)
    if info["status"] == "ready" and store.get("alive:" + code) is None:
        info["status"] = "asleep"  # was ready, has stopped polling: screen off or page closed
    ms = store.get("ms:" + code)
    info["last_ms"] = int(ms) if ms else None
    del info["ends"]
    return info


def bye(store, req):
    """Computer side: the game is over; the phone gets 410 at its next poll and stops."""
    code = _code(req)
    store.delete("pair:" + code, "alive:" + code, "jobs:" + code, "ms:" + code)
    return {"ok": True}


def hello(store, req):
    """Phone side: report download progress or readiness."""
    code = _code(req)
    old = _pairing(store, code)
    state, progress = req.get("status"), req.get("progress", 0)
    model, detail = req.get("model", ""), req.get("detail", "")
    if (state not in STATUSES or type(progress) not in (int, float) or not 0 <= progress <= 1
            or not isinstance(model, str) or not isinstance(detail, str)):
        raise BadRequest("Invalid phone status.")
    info = {"status": state, "progress": progress, "model": model[:60], "detail": detail[:120], "ends": old["ends"]}
    store.set("pair:" + code, json.dumps(info), max(1, int(old["ends"] - time.time())))
    return {"ok": True}


def next_job(store, req):
    """Phone side: long poll for the next prompt to run."""
    code = _code(req)
    _pairing(store, code)
    # One poll per code at a time: parallel polls would only multiply the store's cost.
    if not store.lock("poll:" + code, PHONE_WAIT + 5):
        time.sleep(2)  # an earlier poll is still open: slow the phone down instead of letting it spin
        return {"job": None}
    # Idle cost on Upstash: about 16 commands per poll, so roughly 3,000 an hour per waiting phone.
    store.set("alive:" + code, "1", ALIVE_TTL + PHONE_WAIT)
    end = time.monotonic() + PHONE_WAIT
    try:
        while (left := end - time.monotonic()) > 0:
            raw = store.pop("jobs:" + code, left, every=1.5)
            if raw is None:
                break
            job = json.loads(raw)
            # The computer has stopped waiting for this one; working on it would delay every later question.
            if job.pop("deadline", 0) - time.time() >= MIN_LEFT:
                return {"job": job}
        return {"job": None}
    finally:
        store.delete("poll:" + code)


def done(store, req):
    """Phone side: hand back the text generated for a job."""
    code = _code(req)
    job, text, ms = req.get("id"), req.get("text"), req.get("ms")
    if (not isinstance(job, str) or not JOB_ID.fullmatch(job) or not isinstance(text, str)
            or not (ms is None or type(ms) is int and 0 <= ms <= 600_000)):
        raise BadRequest("Invalid answer.")
    _pairing(store, code)
    store.push(f"res:{code}:{job}", text[:MAX_TEXT], JOB_TTL)
    if ms is not None:
        store.set("ms:" + code, str(ms), PAIR_TTL)  # shown on the computer, to explain slow answers
    return {"ok": True}


def phone_model(store, code):
    """Return model(system, user, schema, max_tokens) -> str running on the paired phone.

    Make one per question: all its calls share one deadline. Call model.release() afterwards.
    """
    deadline = time.time() + QUESTION_BUDGET
    locked = False

    def model(system, user, schema, max_tokens):
        nonlocal locked
        try:
            # A phone that stopped polling is not waited for: the player gets a written line at once.
            if store.get("alive:" + code) is None:
                raise ModelDown("asleep")
            # One question per code at a time: the real page never sends two, a script would.
            if not locked:
                if not store.lock("busy:" + code, QUESTION_BUDGET):
                    raise ModelDown("busy")
                locked = True
            left = deadline - time.time()
            if left < MIN_LEFT:
                raise ModelDown("slow")
            job = secrets.token_hex(16)
            store.push("jobs:" + code, json.dumps({"id": job, "system": system, "user": user, "schema": schema,
                                                   "max_tokens": max_tokens, "deadline": deadline}), JOB_TTL)
            text = store.pop(f"res:{code}:{job}", left, every=0.5)
        except StoreDown as e:
            raise ModelDown("store") from e
        if text is None:
            raise ModelDown("slow")
        return text

    def release():
        if locked:
            try:
                store.delete("busy:" + code)
            except StoreDown:
                pass  # the lock expires on its own after QUESTION_BUDGET

    model.release = release
    return model
