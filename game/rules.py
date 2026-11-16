"""Game rules.

The scenario file holds the truth. The model only does two things: pick which
topic a question is about, and put a fact the rules already chose into the
suspect's voice. A secret that is never in the prompt cannot leak.
"""
import json
import re
from pathlib import Path

MAX_QUESTION = 200
MAX_REPLY = 300

SCENARIO = json.loads((Path(__file__).parent / "scenario.json").read_text(encoding="utf-8"))


class BadRequest(Exception):
    """The browser sent something the rules refuse. The message is shown to the player."""


class ModelDown(Exception):
    """The model is unreachable or answered nonsense; fall back to scripted lines."""


def unlocked(item, clues):
    """Topics and map links may stay hidden until the player holds a clue."""
    return item.get("needs") is None or item["needs"] in clues


def open_topics(suspect, clues):
    return {tid: t for tid, t in suspect["topics"].items() if unlocked(t, clues)}


def clue_view(sc, clue_id):
    return {"id": clue_id, **sc["clues"][clue_id]}


def leads(sc, clues):
    """Subjects the player can raise with each suspect, with a ready-made question for each."""
    return {
        sid: [{"label": t["label"], "sample": t["sample"]} for t in open_topics(s, clues).values()]
        for sid, s in sc["suspects"].items()
    }


def map_view(sc, clues):
    plan = sc["map"]
    links = [{k: v for k, v in link.items() if k != "needs"} for link in plan["links"] if unlocked(link, clues)]
    return {"rooms": plan["rooms"], "links": links, "legend": plan.get("legend", "")}


def public_view(sc):
    """What the browser may see before the end: no culprit, no answers, no hidden clues."""
    start = sc["start_clues"]
    return {
        "title": sc["title"],
        "intro": sc["intro"],
        "max_questions": sc["max_questions"],
        "suspects": [
            {"id": k, "name": s["name"], "role": s["role"], "place": s.get("place"), "face": s.get("face")}
            for k, s in sc["suspects"].items()
        ],
        "clues": [clue_view(sc, c) for c in start],
        "leads": leads(sc, start),
        "map": map_view(sc, start),
    }


def clean(text):
    return " ".join("".join(ch for ch in text if ch.isprintable() or ch.isspace()).split())


def parse_suspect(sc, req):
    suspect_id = req.get("suspect")
    if not isinstance(suspect_id, str) or suspect_id not in sc["suspects"]:
        raise BadRequest("Unknown suspect.")
    return suspect_id


def parse_state(sc, req):
    """The game state travels through the browser, so it is checked like any other input."""
    state = req.get("state")
    if not isinstance(state, dict):
        raise BadRequest("Missing game state.")
    clues, asked = state.get("clues"), state.get("asked")
    if (not isinstance(clues, list) or len(clues) > len(sc["clues"])
            or not all(isinstance(c, str) and c in sc["clues"] for c in clues)):
        raise BadRequest("Invalid clues.")
    if type(asked) is not int or not 0 <= asked <= sc["max_questions"]:
        raise BadRequest("Invalid question count.")
    return {"clues": list(dict.fromkeys(clues)), "asked": asked}


def parse_ask(sc, req):
    suspect_id = parse_suspect(sc, req)

    question = req.get("question")
    if not isinstance(question, str):
        raise BadRequest("Ask a question.")
    question = clean(question)
    if not question:
        raise BadRequest("Ask a question.")
    if len(question) > MAX_QUESTION:
        raise BadRequest(f"Keep the question under {MAX_QUESTION} characters.")

    state = parse_state(sc, req)
    shown = req.get("show")
    if shown is not None and shown not in state["clues"]:
        raise BadRequest("You do not have that clue.")

    return suspect_id, question, shown, state


def keyword_topic(topics, question):
    """Scripted fallback: the topic sharing the most keywords with the question."""
    words = set(re.findall(r"\w+", question.lower()))
    best, best_score = None, 0
    for topic_id, topic in topics.items():
        score = len(words & set(topic["keywords"]))
        if score > best_score:
            best, best_score = topic_id, score
    return best


def model_topic(topics, question, model):
    ids = list(topics)
    listing = "\n".join(f"- {tid}: {t['about']}" for tid, t in topics.items())
    system = ("Pick the one topic the detective's question is about.\nTopics:\n"
              f"{listing}\n- none: anything else")
    schema = {
        "type": "object",
        "properties": {"topic": {"type": "string", "enum": ids + ["none"]}},
        "required": ["topic"],
    }
    try:
        topic = json.loads(model(system, question, schema, 12))["topic"]
    except (ValueError, KeyError, TypeError) as e:
        raise ModelDown("unreadable topic") from e
    return topic if topic in ids else None


def model_reply(suspect, question, fact, model):
    system = (f"You are {suspect['name']}, {suspect['role']}. {suspect['voice']} "
              "A detective is questioning you. Answer in character, in one or two short sentences. "
              f"Say only this, in your own words, and add no other facts: {fact}")
    text = model(system, question, None, 60)
    if not isinstance(text, str) or not clean(text):
        raise ModelDown("empty reply")
    return clean(text)[:MAX_REPLY]


def ask(sc, req, model=None):
    """Answer one question. A silent or failing model falls back to the scripted line."""
    suspect_id, question, shown, state = parse_ask(sc, req)
    if state["asked"] >= sc["max_questions"]:
        raise BadRequest("No questions left. Make your accusation.")
    suspect = sc["suspects"][suspect_id]
    topics = open_topics(suspect, state["clues"])

    try:
        if model is None:
            raise ModelDown("no model configured")
        topic_id = model_topic(topics, question, model)
    except ModelDown:
        model, topic_id = None, keyword_topic(topics, question)

    topic = topics.get(topic_id)
    cracked = False
    if topic is None:
        fact, gives = suspect["dodge"], None
    else:
        crack = topic.get("cracks")
        cracked = bool(crack) and crack["clue"] == shown
        line = crack if cracked else topic
        fact, gives = line["says"], line.get("gives")

    reply = fact
    if model is not None:
        try:
            reply = model_reply(suspect, question, fact, model)
        except ModelDown:
            model = None

    found = gives is not None and gives not in state["clues"]
    clues = state["clues"] + ([gives] if found else [])
    asked = state["asked"] + 1
    return {
        "reply": reply,
        # The notebook records the scripted fact, so a model that embellishes cannot mislead the player.
        "note": fact if topic is not None else None,
        "cracked": cracked,
        "source": "phone" if model is not None else "script",
        "new_clue": clue_view(sc, gives) if found else None,
        "state": {"clues": clues, "asked": asked},
        "remaining": sc["max_questions"] - asked,
        "leads": leads(sc, clues),
        "map": map_view(sc, clues),
    }


def accuse(sc, req):
    suspect_id = parse_suspect(sc, req)
    clues = parse_state(sc, req)["clues"] if "state" in req else []
    correct = suspect_id == sc["culprit"]
    return {
        "correct": correct,
        # Naming the right suspect is a guess until the player holds the proof.
        "proven": correct and set(sc.get("proof", [])) <= set(clues),
        "culprit": sc["suspects"][sc["culprit"]]["name"],
        "solution": sc["solution"],
    }


def handle(method, raw, model=None, sc=SCENARIO):
    """Entry point shared by the Vercel function and the dev server: (status, payload)."""
    if method == "GET":
        return 200, public_view(sc)
    try:
        req = json.loads(raw)
        if not isinstance(req, dict):
            raise BadRequest("Expected a JSON object.")
        if req.get("action") == "ask":
            return 200, ask(sc, req, model)
        if req.get("action") == "accuse":
            return 200, accuse(sc, req)
        raise BadRequest("Unknown action.")
    except ValueError:
        return 400, {"error": "Invalid JSON."}
    except BadRequest as e:
        return 400, {"error": str(e)}


def check_scenario(sc):
    """Raise AssertionError if a scenario is inconsistent or has a clue nobody can reach."""
    clues = set(sc["clues"])
    rooms = {room["id"] for room in sc["map"]["rooms"]}
    assert sc["culprit"] in sc["suspects"], "culprit is not a suspect"
    assert set(sc["start_clues"]) <= clues, "unknown start clue"
    assert set(sc.get("proof", [])) <= clues, "unknown proof clue"
    places = [c.get("place") for c in sc["clues"].values()] + [s.get("place") for s in sc["suspects"].values()]
    places += [end for link in sc["map"]["links"] for end in (link["a"], link["b"])]
    assert {p for p in places if p} <= rooms, "unknown room"

    refs = {link["needs"] for link in sc["map"]["links"] if link.get("needs")}
    lines = []  # (clues needed, clue given or None)
    for suspect in sc["suspects"].values():
        faces = suspect.get("face", [])
        assert len({len(row) for row in faces}) <= 1, f"{suspect['name']}: face rows differ in length"
        for topic_id, topic in suspect["topics"].items():
            bad = [k for k in topic["keywords"] if k != k.lower() or not k.isalnum()]
            assert not bad, f"keywords must be single lowercase words: {bad}"
            # The suggested question must work even when the phone is off.
            assert keyword_topic(suspect["topics"], topic["sample"]) == topic_id, \
                f"sample question does not lead to its own topic: {topic['sample']!r}"
            assert topic["label"], "topic without a label"
            needs = {topic["needs"]} if topic.get("needs") else set()
            lines.append((needs, topic.get("gives")))
            if "cracks" in topic:
                lines.append((needs | {topic["cracks"]["clue"]}, topic["cracks"].get("gives")))
    refs |= {c for needs, gives in lines for c in (*needs, gives) if c}
    assert refs <= clues, f"unknown clue ids: {refs - clues}"

    have = set(sc["start_clues"])
    while True:
        new = {gives for needs, gives in lines if gives and needs <= have} - have
        if not new:
            break
        have |= new
    assert have == clues, f"unreachable clues: {clues - have}"
