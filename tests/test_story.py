import copy
import http.client
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from game import rules  # noqa: E402
import dev  # noqa: E402

SC = json.loads((Path(__file__).parent / "case.json").read_text(encoding="utf-8"))
DOOR_Q = "Which door did you hear twice?"
DOOR_SAYS = "It was the break room door. It squeaks."
CHLOE = SC["suspects"]["chloe"]


def req(suspect="bruno", question="What did you see of the others?", clues=("crumbs",), asked=0):
    return {"action": "ask", "suspect": suspect, "question": question,
            "state": {"clues": list(clues), "asked": asked}}


def ask(r, model=None):
    return rules.ask(SC, r, model)


def all_says(sc):
    out = []
    for s in sc["suspects"].values():
        for t in s["topics"].values():
            out.append(t["says"])
            if "cracks" in t:
                out.append(t["cracks"]["says"])
    return out


def labels(lead_list):
    return [x["label"] for x in lead_list]


NOMATCH = "Tell me something."  # matches no keyword: forces the model classification


class Gating(unittest.TestCase):
    def test_scripted_hidden_then_open(self):
        r = ask(req("chloe", DOOR_Q, ["crumbs"]))
        self.assertNotEqual(r["reply"], DOOR_SAYS)
        self.assertNotEqual(r["note"], DOOR_SAYS)
        r = ask(req("chloe", DOOR_Q, ["crumbs", "mug"]))
        self.assertEqual(r["reply"], DOOR_SAYS)

    def fake(self, answer):
        calls = []

        def model(system, user, schema, max_tokens):
            calls.append((system, schema))
            return answer if schema is not None else "ok"
        return model, calls

    def test_model_prompt_hides_topic(self):
        model, calls = self.fake('{"topic": "none"}')
        ask(req("chloe", NOMATCH, ["crumbs"]), model)
        system, schema = next(c for c in calls if c[1] is not None)
        self.assertNotIn("door", schema["properties"]["topic"]["enum"])
        self.assertNotIn("the door she heard", system)

    def test_model_forced_hidden_topic_dodges(self):
        model, _ = self.fake('{"topic": "door"}')
        r = ask(req("chloe", NOMATCH, ["crumbs"]), model)
        self.assertIsNone(r["note"])
        self.assertEqual(r["note"], None)
        self.assertEqual(CHLOE["dodge"], rules.ask(SC, req("chloe", "zzz"), None)["reply"])
        self.assertNotIn(DOOR_SAYS, r["reply"])

    def test_model_forced_dodge_fact_is_dodge(self):
        seen = []

        def model(system, user, schema, max_tokens):
            if schema is not None:
                return '{"topic": "door"}'
            seen.append(system)
            return "dodging"
        r = ask(req("chloe", NOMATCH, ["crumbs"]), model)
        self.assertIsNone(r["note"])
        self.assertIn(CHLOE["dodge"], seen[0])

    def test_model_open_topic_in_enum(self):
        model, calls = self.fake('{"topic": "none"}')
        ask(req("chloe", NOMATCH, ["crumbs", "mug"]), model)
        _, schema = next(c for c in calls if c[1] is not None)
        self.assertIn("door", schema["properties"]["topic"]["enum"])


class Leads(unittest.TestCase):
    def test_public_view_leads(self):
        v = rules.public_view(SC)
        self.assertEqual(set(v["leads"]), set(SC["suspects"]))
        for lst in v["leads"].values():
            for lead in lst:
                self.assertEqual(set(lead), {"label", "sample"})
        self.assertNotIn("The door", labels(v["leads"]["chloe"]))

    def test_ask_unlocks_lead(self):
        r = ask(req())
        self.assertEqual(r["new_clue"]["id"], "mug")
        self.assertIn("The door", labels(r["leads"]["chloe"]))

    def test_leads_leak_nothing(self):
        for held in (SC["start_clues"], list(SC["clues"])):
            dump = json.dumps(rules.leads(SC, held))
            for text in all_says(SC):
                self.assertNotIn(text, dump)
            for s in SC["suspects"].values():
                for t in s["topics"].values():
                    self.assertNotIn(t["about"], dump)
            for word in ("keywords", "cracks", "gives"):
                self.assertNotIn(word, dump)


class MapView(unittest.TestCase):
    def pairs(self, m):
        return {(link["a"], link["b"]) for link in m["links"]}

    def test_start(self):
        m = rules.map_view(SC, SC["start_clues"])
        self.assertIn(("breakroom", "openspace"), self.pairs(m))
        self.assertNotIn(("openspace", "office"), self.pairs(m))
        self.assertEqual(m["rooms"], SC["map"]["rooms"])

    def test_mug(self):
        m = rules.map_view(SC, ["crumbs", "mug"])
        self.assertEqual(self.pairs(m), {("breakroom", "openspace"), ("openspace", "office")})
        self.assertEqual(m["rooms"], SC["map"]["rooms"])

    def test_no_needs_key(self):
        for held in (SC["start_clues"], list(SC["clues"])):
            for link in rules.map_view(SC, held)["links"]:
                self.assertNotIn("needs", link)

    def test_ask_carries_map(self):
        r = ask(req())
        self.assertEqual(r["map"], rules.map_view(SC, ["crumbs", "mug"]))
        self.assertIn(("openspace", "office"), self.pairs(r["map"]))

    def test_public_view_map(self):
        self.assertEqual(rules.public_view(SC)["map"], rules.map_view(SC, SC["start_clues"]))


class PublicViewSuspects(unittest.TestCase):
    def test_keys(self):
        v = rules.public_view(SC)
        for s in v["suspects"]:
            self.assertEqual(set(s), {"id", "name", "role", "place", "face"})
        dump = json.dumps(v)
        for word in ("topics", "voice", "dodge"):
            self.assertNotIn(word, dump)


class Accuse(unittest.TestCase):
    def acc(self, suspect, state="absent"):
        r = {"action": "accuse", "suspect": suspect}
        if state != "absent":
            r["state"] = state
        return rules.accuse(SC, r)

    def test_proven(self):
        r = self.acc("bruno", {"clues": ["crumbs", "confession"], "asked": 0})
        self.assertIs(r["correct"], True)
        self.assertIs(r["proven"], True)

    def test_no_state(self):
        r = self.acc("bruno")
        self.assertIs(r["correct"], True)
        self.assertIs(r["proven"], False)

    def test_missing_confession(self):
        self.assertIs(self.acc("bruno", {"clues": ["crumbs"], "asked": 0})["proven"], False)

    def test_wrong_suspect(self):
        r = self.acc("ada", {"clues": ["confession"], "asked": 0})
        self.assertIs(r["correct"], False)
        self.assertIs(r["proven"], False)

    def test_bad_state(self):
        for st in ({"clues": ["nope"], "asked": 0}, {"clues": "confession", "asked": 0}):
            with self.assertRaises(rules.BadRequest):
                self.acc("bruno", st)
            status, _ = rules.handle("POST", json.dumps(
                {"action": "accuse", "suspect": "bruno", "state": st}), sc=SC)
            self.assertEqual(status, 400)


class CheckScenario(unittest.TestCase):
    def bad(self, mutate):
        sc = copy.deepcopy(SC)
        mutate(sc)
        with self.assertRaises(AssertionError):
            rules.check_scenario(sc)

    def test_fixture_passes(self):
        rules.check_scenario(copy.deepcopy(SC))

    def test_topic_needs_unknown(self):
        self.bad(lambda sc: sc["suspects"]["chloe"]["topics"]["door"].update(needs="nope"))

    def test_proof_unknown(self):
        self.bad(lambda sc: sc.update(proof=["nope"]))

    def test_clue_place_unknown(self):
        self.bad(lambda sc: sc["clues"]["mug"].update(place="nowhere"))

    def test_link_end_unknown(self):
        self.bad(lambda sc: sc["map"]["links"][0].update(b="nowhere"))

    def test_suspect_place_unknown(self):
        self.bad(lambda sc: sc["suspects"]["ada"].update(place="nowhere"))

    def test_sample_mismatch(self):
        self.bad(lambda sc: sc["suspects"]["ada"]["topics"]["morning"].update(sample="Hello there"))

    def test_face_rows(self):
        self.bad(lambda sc: sc["suspects"]["ada"].update(face=["ab", "abc"]))

    def test_unreachable_chain(self):
        def m(sc):
            sc["clues"]["ghost"] = {"name": "Ghost", "text": "x"}
            sc["clues"]["prize"] = {"name": "Prize", "text": "y"}
            sc["suspects"]["chloe"]["topics"]["door"].update(needs="ghost", gives="prize")
        self.bad(m)


class RealScenario(unittest.TestCase):
    sc = rules.SCENARIO

    def test_solvable(self):
        sc = self.sc
        clues = list(sc["start_clues"])
        grew = True
        while grew:
            grew = False
            for sid in sc["suspects"]:
                for lead in rules.leads(sc, clues)[sid]:
                    for show in [None] + list(clues):
                        r = rules.ask(sc, {"action": "ask", "suspect": sid, "question": lead["sample"],
                                           "show": show, "state": {"clues": list(clues), "asked": 0}})
                        if r["new_clue"] and r["new_clue"]["id"] not in clues:
                            clues.append(r["new_clue"]["id"])
                            grew = True
        self.assertEqual(set(clues), set(sc["clues"]))
        self.assertTrue(set(sc["proof"]) <= set(clues))
        r = rules.accuse(sc, {"suspect": sc["culprit"], "state": {"clues": clues, "asked": 0}})
        self.assertIs(r["proven"], True)
        for sid in sc["suspects"]:
            for lead in rules.leads(sc, clues)[sid]:
                r = rules.ask(sc, {"suspect": sid, "question": lead["sample"],
                                   "state": {"clues": list(clues), "asked": 0}})
                self.assertIsNotNone(r["note"], f"{sid}: {lead['sample']!r} dodges")

    def test_public_view_no_leaks(self):
        v = rules.public_view(self.sc)
        dump = json.dumps(v)
        self.assertNotIn(self.sc["solution"], dump)
        self.assertNotIn("culprit", v)
        for text in all_says(self.sc):
            self.assertNotIn(text, dump)

        def walk(o):
            if isinstance(o, dict):
                for k, val in o.items():
                    if k == "culprit":
                        self.assertNotEqual(val, self.sc["culprit"])
                    walk(val)
            elif isinstance(o, list):
                for x in o:
                    walk(x)
        walk(v)


class Http(unittest.TestCase):
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

    def call(self, method, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            c.request(method, "/api/game", body, {"Content-Type": "application/json"} if body else {})
            r = c.getresponse()
            return r.status, json.loads(r.read())
        finally:
            c.close()

    def test_get_has_leads_and_map(self):
        status, v = self.call("GET")
        self.assertEqual(status, 200)
        self.assertIn("leads", v)
        self.assertIn("map", v)

    def test_accuse(self):
        _, v = self.call("GET")
        body = json.dumps({"action": "accuse", "suspect": v["suspects"][0]["id"],
                           "state": {"clues": [], "asked": 0}}).encode()
        status, r = self.call("POST", body)
        self.assertEqual(status, 200)
        self.assertIsInstance(r["correct"], bool)
        self.assertIsInstance(r["proven"], bool)


if __name__ == "__main__":
    unittest.main()
