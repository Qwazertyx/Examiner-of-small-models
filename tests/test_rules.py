import copy
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from game import rules  # noqa: E402

SC = json.loads((Path(__file__).parent / "case.json").read_text(encoding="utf-8"))
MUG_FACT = SC["suspects"]["bruno"]["topics"]["others"]["says"]
ADA_LIE = SC["suspects"]["ada"]["topics"]["morning"]["says"]
ADA_TRUTH = SC["suspects"]["ada"]["topics"]["morning"]["cracks"]["says"]


def req(suspect="bruno", question="What did you see of the others?", clues=("crumbs",), asked=0, **extra):
    r = {"action": "ask", "suspect": suspect, "question": question,
         "state": {"clues": list(clues), "asked": asked}}
    r.update(extra)
    return r


def ask(r, model=None):
    return rules.ask(SC, r, model)


class PublicView(unittest.TestCase):
    def test_no_secrets(self):
        v = rules.public_view(SC)
        dump = json.dumps(v)
        for word in ("culprit", "says", "solution"):
            self.assertNotIn(word, dump)
        for s in SC["suspects"].values():
            for t in s["topics"].values():
                self.assertNotIn(t["about"], dump)
                self.assertNotIn(t["says"], dump)
        self.assertEqual([s["id"] for s in v["suspects"]], ["ada", "bruno", "chloe"])
        self.assertEqual([c["id"] for c in v["clues"]], SC["start_clues"])


class Ask(unittest.TestCase):
    def test_scripted_path(self):
        r = ask(req())
        self.assertEqual(r["reply"], MUG_FACT)
        self.assertEqual(r["new_clue"]["id"], "mug")
        self.assertEqual(r["source"], "script")
        self.assertEqual(r["remaining"], 11)
        self.assertEqual(r["state"], {"clues": ["crumbs", "mug"], "asked": 1})

    def test_lie_holds_without_clue(self):
        r = ask(req("ada", "Where were you this morning?"))
        self.assertEqual(r["reply"], ADA_LIE)
        self.assertFalse(r["cracked"])
        self.assertIsNone(r["new_clue"])

    def test_cracking(self):
        r = ask(req("ada", "Where were you this morning?", clues=("crumbs", "mug"), show="mug"))
        self.assertEqual(r["reply"], ADA_TRUTH)
        self.assertTrue(r["cracked"])
        self.assertEqual(r["new_clue"]["id"], "bruno_seen")

    def test_show_unowned_clue(self):
        with self.assertRaises(rules.BadRequest):
            ask(req("ada", "Where were you this morning?", show="mug"))

    def test_question_limit(self):
        with self.assertRaises(rules.BadRequest):
            ask(req(asked=SC["max_questions"]))

    def test_validation(self):
        def without(key):
            r = req()
            del r[key]
            return r

        def st(**kw):
            r = req()
            r["state"].update(kw)
            return r

        bad = {
            "unknown suspect": req("zed"),
            "suspect not string": req(5),
            "suspect missing": without("suspect"),
            "question missing": without("question"),
            "question int": req(question=5),
            "question empty": req(question=""),
            "question blank": req(question="  \t\n "),
            "question 201": req(question="a" * 201),
            "state missing": without("state"),
            "clues not list": st(clues="crumbs"),
            "clues unknown id": st(clues=["crumbs", "nope"]),
            "clues too many": st(clues=["crumbs"] * (len(SC["clues"]) + 1)),
            "asked negative": st(asked=-1),
            "asked float": st(asked=1.0),
            "asked bool": st(asked=True),
            "asked too high": st(asked=SC["max_questions"] + 1),
        }
        for name, r in bad.items():
            with self.subTest(name), self.assertRaises(rules.BadRequest):
                ask(r)

    def test_question_200_ok(self):
        ask(req(question="a" * 200))

    def test_question_cleaned(self):
        seen = []

        def model(system, user, schema, max_tokens):
            seen.append(user)
            return '{"topic": "morning"}' if schema else "ok"

        ask(req("ada", "Where\x00 were\x07 you   this\t morning?"), model)
        self.assertTrue(seen)
        self.assertTrue(all(u == "Where were you this morning?" for u in seen))

    def test_unmatched_question(self):
        r = ask(req("ada", "Tell me a joke"))
        self.assertEqual(r["reply"], SC["suspects"]["ada"]["dodge"])
        self.assertIsNone(r["note"])
        self.assertEqual(r["state"]["asked"], 1)

    def test_clue_not_given_twice(self):
        r = ask(req(clues=("crumbs", "mug")))
        self.assertIsNone(r["new_clue"])
        self.assertEqual(r["state"]["clues"], ["crumbs", "mug"])


def fake(topic='{"topic": "others"}', reply="  Sure,\n I saw  the mug. ", log=None):
    def model(system, user, schema, max_tokens):
        if log is not None:
            log.append((system, user))
        r = topic if schema is not None else reply
        if isinstance(r, Exception):
            raise r
        return r
    return model


class ModelPath(unittest.TestCase):
    def test_model_reply(self):
        r = ask(req(), fake())
        self.assertEqual(r["source"], "phone")
        self.assertEqual(r["reply"], "Sure, I saw the mug.")
        self.assertEqual(r["note"], MUG_FACT)

    def test_secret_containment(self):
        log = []
        ask(req("ada", "Where were you this morning?"), fake('{"topic": "morning"}', "ok", log))
        self.assertGreaterEqual(len(log), 2)
        for system, user in log:
            for secret in ("8:50", SC["solution"], "culprit"):
                self.assertNotIn(secret, system)
                self.assertNotIn(secret, user)

    def assertFallback(self, model, fact=MUG_FACT):
        r = ask(req(), model)
        self.assertEqual(r["source"], "script")
        self.assertEqual(r["reply"], fact)

    def test_fallbacks(self):
        self.assertFallback(fake(topic=rules.ModelDown("x")))
        self.assertFallback(fake(topic="not json"))
        self.assertFallback(fake(reply=""))
        self.assertFallback(fake(reply=None))
        self.assertFallback(fake(reply=42))

    def test_topic_wrong_type(self):
        # a non-string topic means "no topic": the dodge line, through the model, no crash
        r = ask(req(), fake(topic='{"topic": 5}', reply="Hm."))
        self.assertIsNone(r["note"])
        self.assertEqual(r["reply"], "Hm.")
        self.assertEqual(r["state"]["asked"], 1)

    def test_reply_cut(self):
        r = ask(req(), fake(reply="x" * (rules.MAX_REPLY + 50)))
        self.assertEqual(len(r["reply"]), rules.MAX_REPLY)


class Accuse(unittest.TestCase):
    def test_accuse(self):
        ok = rules.accuse(SC, {"suspect": "bruno"})
        bad = rules.accuse(SC, {"suspect": "ada"})
        self.assertTrue(ok["correct"])
        self.assertFalse(bad["correct"])
        self.assertEqual(ok["solution"], SC["solution"])
        self.assertEqual(bad["solution"], SC["solution"])
        with self.assertRaises(rules.BadRequest):
            rules.accuse(SC, {"suspect": "zed"})


class Handle(unittest.TestCase):
    def test_handle(self):
        self.assertEqual(rules.handle("GET", b"", sc=SC)[0], 200)
        for raw in (b"{nope", b"\xff\xfe", b"[1]", b'{"action": "dance"}', b"{}"):
            with self.subTest(raw=raw):
                status, payload = rules.handle("POST", raw, sc=SC)
                self.assertEqual(status, 400)
                self.assertIn("error", payload)
        status, payload = rules.handle("POST", json.dumps(req()).encode(), sc=SC)
        self.assertEqual(status, 200)
        self.assertIn("reply", payload)


class CheckScenario(unittest.TestCase):
    def test_valid(self):
        rules.check_scenario(SC)
        rules.check_scenario(rules.SCENARIO)

    def test_broken(self):
        def culprit(s): s["culprit"] = "nobody"
        def unknown_gives(s): s["suspects"]["ada"]["topics"]["others"]["gives"] = "ghost"
        def orphan(s): s["clues"]["orphan"] = {"name": "x", "text": "y"}
        def upper(s): s["suspects"]["ada"]["topics"]["others"]["keywords"].append("Morning")
        def two_words(s): s["suspects"]["ada"]["topics"]["others"]["keywords"].append("break room")
        for f in (culprit, unknown_gives, orphan, upper, two_words):
            sc = copy.deepcopy(SC)
            f(sc)
            with self.subTest(f.__name__), self.assertRaises(AssertionError):
                rules.check_scenario(sc)


if __name__ == "__main__":
    unittest.main()
