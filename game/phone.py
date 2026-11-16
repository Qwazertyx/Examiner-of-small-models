"""Lending phone: a visitor scans a QR code and their phone runs the model in its browser.

The computer and the phone share a random pairing code. Questions go to the phone
through a job list in the store, and answers come back through a per-job list.
A phone only ever serves the one game whose code it scanned.
"""
import json
import re
import secrets

from .rules import BadRequest, ModelDown
from .store import StoreDown

PAIR_TTL = 3 * 3600   # a pairing lives for one sitting
ALIVE_TTL = 30        # the phone counts as connected for this long after its last poll
JOB_TTL = 120
PHONE_WAIT = 20       # long poll held open for the phone; Vercel function limit is 60 s
ANSWER_WAIT = 25      # per model call; a question makes two calls
PAIRS_PER_HOUR = 30   # per player address
MAX_TEXT = 1500

CODE = re.compile(r"[A-Za-z0-9_-]{16,64}")
JOB_ID = re.compile(r"[0-9a-f]{32}")
STATUSES = {"loading", "ready", "failed"}


class Gone(Exception):
    """Unknown or expired pairing code: the game this phone served has ended."""


def _code(req):
    code = req.get("code")
    if not isinstance(code, str) or not CODE.fullmatch(code):
        raise BadRequest("Invalid pairing code.")
    return code


def _pairing(store, code):
    raw = store.get("pair:" + code)
    if raw is None:
        raise Gone()
    return json.loads(raw)


def pair(store, client):
    """Computer side: open a new pairing. `client` is a hash of the player's address."""
    if store.incr("rate:pair:" + client, 3600) > PAIRS_PER_HOUR:
        raise BadRequest("Too many new phone codes. Wait a while, or play with written lines.")
    code = secrets.token_urlsafe(18)
    store.set("pair:" + code, json.dumps({"status": "waiting", "progress": 0, "model": "", "detail": ""}), PAIR_TTL)
    return {"code": code}


def status(store, code):
    """Computer side: what the phone reported, and whether it is polling right now."""
    if not CODE.fullmatch(code):
        raise BadRequest("Invalid pairing code.")
    info = _pairing(store, code)
    if info["status"] == "ready" and store.get("alive:" + code) is None:
        info["status"] = "asleep"  # was ready, has stopped polling: screen off or page closed
    return info


def hello(store, req):
    """Phone side: report download progress or readiness."""
    code = _code(req)
    _pairing(store, code)
    state, progress = req.get("status"), req.get("progress", 0)
    model, detail = req.get("model", ""), req.get("detail", "")
    if (state not in STATUSES or type(progress) not in (int, float) or not 0 <= progress <= 1
            or not isinstance(model, str) or not isinstance(detail, str)):
        raise BadRequest("Invalid phone status.")
    info = {"status": state, "progress": progress, "model": model[:60], "detail": detail[:120]}
    store.set("pair:" + code, json.dumps(info), PAIR_TTL)
    return {"ok": True}


def next_job(store, req):
    """Phone side: long poll for the next prompt to run."""
    code = _code(req)
    _pairing(store, code)
    # Idle cost on Upstash: about 14 commands per poll, so roughly 2,500 an hour per waiting phone.
    store.set("alive:" + code, "1", ALIVE_TTL + PHONE_WAIT)
    raw = store.pop("jobs:" + code, PHONE_WAIT, every=1.5)
    return {"job": json.loads(raw) if raw else None}


def done(store, req):
    """Phone side: hand back the text generated for a job."""
    code = _code(req)
    job, text = req.get("id"), req.get("text")
    if not isinstance(job, str) or not JOB_ID.fullmatch(job) or not isinstance(text, str):
        raise BadRequest("Invalid answer.")
    _pairing(store, code)
    store.push(f"res:{code}:{job}", text[:MAX_TEXT], JOB_TTL)
    return {"ok": True}


def phone_model(store, code):
    """Return model(system, user, schema, max_tokens) -> str running on the paired phone."""

    def model(system, user, schema, max_tokens):
        try:
            # A phone that stopped polling is not waited for: the player gets a written line at once.
            if store.get("alive:" + code) is None:
                raise ModelDown("phone not connected")
            job = secrets.token_hex(16)
            store.push("jobs:" + code, json.dumps(
                {"id": job, "system": system, "user": user, "schema": schema, "max_tokens": max_tokens}), JOB_TTL)
            text = store.pop(f"res:{code}:{job}", ANSWER_WAIT, every=0.5)
        except StoreDown as e:
            raise ModelDown(str(e)) from e
        if text is None:
            raise ModelDown("phone did not answer in time")
        return text

    return model
