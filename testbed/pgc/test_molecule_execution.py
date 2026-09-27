"""
Molecule execution — the runtime's half of software_governance/dossiers/molecule_composition.

A loop whose body is a molecule of two atoms is run the way the compiler seals it: an atom that
offers candidate words and is declared not deterministic (it draws at random), and an atom that
chooses one permitted word and is deterministic given what was offered, the rules and a seed.

Shown here: a loop runs one pass per member, every pass; a finished loop's remaining passes change
nothing; molecules nest; every atom leaves its own record; a non-deterministic atom's outcomes are
recorded with their values; and a replay reproduces the execution from those records without
running the atom, while a replay missing a record is refused rather than run.
"""

import json
import random
import sys
import tempfile
import types
import unittest
from pathlib import Path

from runtime.ct_executor import CTExecutor, CTExecutionError
from runtime.ct_execute import execute_ct
from runtime.evidence import TraceWriter
from runtime.replay import compare, recorded_outcomes

W = Path(__file__).resolve().parents[3]
FORBIDDEN = "ACCT-999"
CALLS = {"offer": 0}


def _offer(inputs):
    CALLS["offer"] += 1
    rng = random.Random()  # deliberately unseeded: this atom is not deterministic
    words = ["balance", "is", "fine", FORBIDDEN, "today", "<end>"]
    return {"candidates": rng.sample(words, 3)}


def _choose(inputs):
    text, done = inputs["text"], inputs["done"]
    if done:
        return {"text": text, "done": True, "blocked": []}
    permitted = [w for w in inputs["candidates"] if w != inputs["forbidden"]]
    blocked = [w for w in inputs["candidates"] if w == inputs["forbidden"]]
    if not permitted:
        return {"text": text, "done": True, "blocked": blocked}
    word = random.Random(f"{inputs['seed']}:{inputs['position']}").choice(permitted)
    if word == "<end>":
        return {"text": text, "done": True, "blocked": blocked}
    return {"text": (text + " " + word).strip(), "done": False, "blocked": blocked}


def _finish_at_three(inputs):
    """Deterministic chooser that finishes on the third pass, to show later passes change nothing."""
    if inputs["done"]:
        return {"text": inputs["text"], "done": True, "blocked": []}
    text = (inputs["text"] + " w").strip()
    return {"text": text, "done": inputs["position"] >= 3, "blocked": []}


def _echo(inputs):
    return {"received": dict(inputs)}


probe = types.ModuleType("probe_molecule_atoms")
probe.offer, probe.choose, probe.finish_at_three = _offer, _choose, _finish_at_three
probe.echo = _echo
sys.modules["probe_molecule_atoms"] = probe


def _atom(name, callable_, purity, args, out):
    return {"atom": f"probe::{name}", "out": out, "purity": purity, "args": args, "input_types": {},
            "handler_ref": {"module": "probe_molecule_atoms", "callable": callable_}}


def _write_ct_ir(chooser="choose"):
    """The sealed form the compiler produces for WRITE = loop over positions of PASS = offer; choose."""
    body = {"atom_stream": [
        _atom("CT_IMPURE_OFFER_V0", "offer", "ct_impure", {"position": "$.inputs.position"}, "offered"),
        _atom("CT_PURE_CHOOSE_V0", chooser, "ct_pure", {
            "candidates": "$.results.offered.candidates", "forbidden": "$.inputs.forbidden",
            "seed": "$.inputs.seed", "position": "$.inputs.position",
            "text": "$.inputs.text", "done": "$.inputs.done"}, "chosen"),
    ], "outputs": {"result": {"from": "chosen"}}}
    loop = {"atom": "probe::CT_WRITE_V0", "out": "written", "purity": "ct_impure", "input_types": {},
            "molecule": body,
            "loop": {"over": "$.inputs.positions", "iterator": "position",
                     "accumulator": {"text": "", "done": False},
                     "inputs": {"position": "$.iterator", "text": "$.accumulator.text",
                                "done": "$.accumulator.done", "forbidden": "$.inputs.forbidden",
                                "seed": "$.inputs.seed"},
                     "update_accumulator": {"text": "$.results.text", "done": "$.results.done"}}}
    return {"atom_stream": [loop], "outputs": {"response": {"from": "written"}}}


INPUTS = {"positions": [1, 2, 3, 4, 5, 6, 7, 8], "forbidden": FORBIDDEN, "seed": 42}


class MoleculeExecutionTest(unittest.TestCase):

    def _run(self, ct_ir=None, inputs=INPUTS, recorded=None):
        records = []
        result = CTExecutor().execute(ct_ir=ct_ir or _write_ct_ir(), inputs=inputs,
                                      observer=records.append, recorded=recorded)
        return result["written"], records

    def test_a_loop_runs_one_pass_per_member_every_time(self):
        _, records = self._run(_write_ct_ir("finish_at_three"))
        passes = {r["path"].split("/")[0] for r in records}
        self.assertEqual(len(passes), len(INPUTS["positions"]))

    def test_a_finished_loop_changes_nothing_in_its_remaining_passes(self):
        result, _ = self._run(_write_ct_ir("finish_at_three"))
        self.assertEqual(result, {"text": "w w w", "done": True, "blocked": []})

    def test_every_atom_leaves_its_own_record(self):
        _, records = self._run()
        self.assertEqual(len(records), 2 * len(INPUTS["positions"]))
        self.assertEqual(len({r["path"] for r in records}), len(records))
        self.assertTrue(all("result_keys" in r for r in records))

    def test_a_nondeterministic_outcome_is_recorded_with_its_values(self):
        _, records = self._run()
        offers = [r for r in records if r["purity"] == "ct_impure"]
        chooses = [r for r in records if r["purity"] == "ct_pure"]
        self.assertTrue(all("candidates" in r["outcome"] for r in offers))
        self.assertTrue(all("outcome" not in r for r in chooses))

    def test_the_forbidden_word_is_never_written(self):
        for _ in range(20):
            result, _ = self._run()
            self.assertNotIn(FORBIDDEN, result["text"])

    def test_a_replay_reproduces_the_result_without_running_the_atom(self):
        result, records = self._run()
        recorded = {r["path"]: r["outcome"] for r in records if r["purity"] == "ct_impure"}
        CALLS["offer"] = 0
        replayed, replay_records = self._run(recorded=recorded.__getitem__)
        self.assertEqual(CALLS["offer"], 0)
        self.assertEqual(replayed, result)
        self.assertTrue(all(r["replayed"] for r in replay_records if r["purity"] == "ct_impure"))

    def test_a_replay_missing_a_record_is_refused_not_run(self):
        def missing(path):
            raise CTExecutionError(f"no recorded outcome for {path}")
        with self.assertRaises(CTExecutionError):
            self._run(recorded=missing)

    def test_molecules_nest(self):
        inner = _write_ct_ir("finish_at_three")["atom_stream"][0]
        outer = {"atom_stream": [{"atom": "probe::CT_OUTER_V0", "out": "outer", "purity": "ct_impure",
                                  "input_types": {}, "args": {"positions": "$.inputs.positions",
                                                              "forbidden": "$.inputs.forbidden",
                                                              "seed": "$.inputs.seed"},
                                  "molecule": {"atom_stream": [inner],
                                               "outputs": {"result": {"from": "written"}}}}],
                 "outputs": {"response": {"from": "outer"}}}
        records = []
        result = CTExecutor().execute(ct_ir=outer, inputs=INPUTS, observer=records.append)
        self.assertEqual(result["outer"]["text"], "w w w")
        self.assertTrue(all(r["path"].startswith("outer/written[") for r in records))


class TraceReplayTest(unittest.TestCase):
    """The same, through the trace writer and the replay comparison the `replay` command uses."""

    def _traced(self, root: Path, replay_from=None):
        writer = TraceWriter(trace_dir=root, trace_id="t", domain="probe", wf_addr=1,
                             wf_fqdn="probe::WF_V0", snapshot_root=W / "snapshot", snapshot_id="probe")
        if replay_from is not None:
            writer.replay_from(recorded_outcomes(replay_from))
        observer = lambda r: writer.ct_step(10, 20, r)
        recorded = (lambda p: writer.recorded_outcome(10, 20, p)) if writer.replaying else None
        execute_ct(_write_ct_ir(), INPUTS, observer=observer, recorded=recorded)
        writer.close()
        return root / "t.jsonl"

    def test_a_replayed_trace_agrees_with_the_original(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            original = self._traced(Path(a))
            CALLS["offer"] = 0
            replay = self._traced(Path(b), replay_from=original)
            self.assertEqual(CALLS["offer"], 0)
            same, detail = compare(original, replay)
            self.assertTrue(same, detail)

    def test_two_fresh_executions_are_told_apart(self):
        # Without the records, two executions draw different words; comparison must say so, or it
        # would be agreeing with anything.
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            first, second = self._traced(Path(a)), self._traced(Path(b))
            outcomes = [json.loads(l)["detail"].get("outcome") for l in first.read_text().splitlines()[1:]]
            others = [json.loads(l)["detail"].get("outcome") for l in second.read_text().splitlines()[1:]]
            if outcomes != others:
                self.assertFalse(compare(first, second)[0])

    def test_an_atom_is_handed_every_argument_even_one_named_like_a_step_key(self):
        names = {"kind": "internal", "as": "a", "out": "o", "loop": "l", "purity": "p", "molecule": "m"}
        step = _atom("CT_PURE_ECHO_V0", "echo", "ct_pure", {n: f"$.inputs.{n}" for n in names}, "echoed")
        result = CTExecutor().execute(ct_ir={"atom_stream": [step]}, inputs=names)
        self.assertEqual(result["echoed"]["received"], names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
