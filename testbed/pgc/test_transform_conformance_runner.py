"""
The transform conformance runner — the runtime's half of software_governance/dossiers/transform_conformance.

A compiled domain is laid out as its build leaves it: a build manifest, its transforms, its vectors and
the runnable cases the compiler wrote from them. Shown here: a molecule with a non-deterministic step is
proven from its recorded results and the step never runs; a case missing a record, or carrying one no
step used, refuses its transform; a transform no vector tests is named unproven and never counted as
passing; a vector nothing was compiled from refuses; and a domain with no vectors is reported, not passed.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_molecule_execution import CALLS, _atom, _write_ct_ir  # noqa: E402  (registers the probe atoms)

from runtime.conformance import run_domain, write_result

DOMAIN = "probe"
WRITE = f"{DOMAIN}::CT_WRITE_V0"
CHOOSE = f"{DOMAIN}::CT_PURE_CHOOSE_V0"
IDLE = f"{DOMAIN}::CT_PURE_IDLE_V0"
POSITIONS = [1, 2, 3, 4]
FINISHED = {"text": "w w w", "done": True, "blocked": []}


def _sealed_write() -> dict:
    ct_ir = _write_ct_ir("finish_at_three")
    ct_ir["outputs"] = {"response": {"from": "written"}}
    return ct_ir


def _choose_ir() -> dict:
    step = _atom("CT_PURE_CHOOSE_V0", "choose", "ct_pure", {"text": "$.inputs.text", "done": "$.inputs.done",
                 "candidates": "$.inputs.candidates", "forbidden": "$.inputs.forbidden",
                 "seed": "$.inputs.seed", "position": "$.inputs.position"}, "chosen")
    return {"atom_stream": [step], "outputs": {"text": {"from": "chosen"}}}


def _recorded(passes=POSITIONS) -> dict:
    return {f"written[{i}]/offered": {"candidates": ["fine"]} for i in range(len(passes))}


def _case(target, case_id, ct_ir, bindings, outcome="SUCCESS", expected=None, recorded=None) -> dict:
    ct_ir = {**ct_ir, "inputs": bindings}
    out = {"artifact_type": "CT_CONFORMANCE", "ct_fqdn": target, "ct_ir": ct_ir,
           "expected": expected or {}, "expected_outcome": outcome, "fqdn": f"{target}::{case_id}"}
    if recorded:
        out["recorded"] = recorded
    return out


def _domain(root: Path, cases: list[dict], vectors: list[str], declare=True) -> Path:
    compiled = root / "snapshot" / "compiled"
    canonical = compiled / "canonical"
    for folder in ("structures", "capability_transforms", "test_data"):
        (canonical / folder).mkdir(parents=True)
    frontmatter = {"structure_scope": DOMAIN}
    if declare:
        frontmatter["output_configuration"] = {"conformance": {"subpath": "compiled/transform_conformance"}}
    (canonical / "structures" / "m.json").write_text(json.dumps(
        {"fqdn_id": f"{DOMAIN}::STRUCTURE_BUILD_PROBE_CONFIG_V0", "frontmatter": frontmatter}))
    for fqdn in (WRITE, CHOOSE, IDLE, "capability_transforms::CT_PURE_LOOKUP_V0"):
        (canonical / "capability_transforms" / f"{fqdn.replace('::', '__')}.json").write_text(
            json.dumps({"artifact_type": "CT", "fqdn_id": fqdn}))
    for target in vectors:
        (canonical / "test_data" / f"{target.replace('::', '__')}.json").write_text(
            json.dumps({"artifact_type": "TEST_DATA", "frontmatter": {"target": target}}))
    case_dir = compiled / "transform_conformance"
    case_dir.mkdir()
    for case in cases:
        (case_dir / f"{case['fqdn'].replace('::', '__')}.json").write_text(json.dumps(case))
    return root


GOOD = [
    _case(WRITE, "finishes_at_three", _sealed_write(),
          {"positions": POSITIONS, "forbidden": "x", "seed": 1},
          expected={"response": FINISHED}, recorded=_recorded()),
    _case(CHOOSE, "refuses_without_inputs", _choose_ir(), {}, outcome="VIOLATION"),
]


class RunnerTest(unittest.TestCase):

    def _run(self, cases, vectors=(WRITE, CHOOSE), declare=True):
        with tempfile.TemporaryDirectory() as tmp:
            root = _domain(Path(tmp), cases, list(vectors), declare)
            result = run_domain(root)
            written = json.loads(write_result(root, result).read_text())
            return result, written

    def test_a_molecule_is_proven_from_its_recorded_results_and_the_step_never_runs(self):
        CALLS["offer"] = 0
        result, _ = self._run(GOOD)
        self.assertEqual(result.proven, [CHOOSE, WRITE], [c for c in result.cases if not c.passed])
        self.assertEqual(CALLS["offer"], 0)

    def test_an_untested_transform_is_named_unproven_and_a_carried_one_is_named_carried(self):
        result, written = self._run(GOOD)
        self.assertEqual(result.unproven, [IDLE])
        self.assertEqual(result.carried, ["capability_transforms::CT_PURE_LOOKUP_V0"])
        self.assertTrue(result.admitted)
        self.assertEqual(written["counts"], {"proven": 2, "unproven": 1, "refused": 0,
                                             "cases": 2, "cases_failed": 0})

    def test_a_missing_record_refuses_rather_than_running_the_step(self):
        CALLS["offer"] = 0
        short = _case(WRITE, "short", _sealed_write(), {"positions": POSITIONS, "forbidden": "x", "seed": 1},
                      expected={"response": FINISHED}, recorded=_recorded(POSITIONS[:2]))
        result, _ = self._run([short, GOOD[1]])
        self.assertIn(WRITE, result.refused)
        self.assertEqual(CALLS["offer"], 0)
        self.assertIn("no recorded result", next(c.error for c in result.cases if not c.passed))

    def test_a_record_no_step_used_refuses(self):
        extra = {**_recorded(), "written[9]/offered": {"candidates": ["fine"]}}
        case = _case(WRITE, "extra", _sealed_write(), {"positions": POSITIONS, "forbidden": "x", "seed": 1},
                     expected={"response": FINISHED}, recorded=extra)
        result, _ = self._run([case, GOOD[1]])
        self.assertIn(WRITE, result.refused)
        self.assertIn("no step used", next(c.error for c in result.cases if not c.passed))

    def test_a_wrong_expectation_refuses(self):
        wrong = _case(WRITE, "wrong", _sealed_write(), {"positions": POSITIONS, "forbidden": "x", "seed": 1},
                      expected={"response": {**FINISHED, "text": "w w"}}, recorded=_recorded())
        result, _ = self._run([wrong, GOOD[1]])
        self.assertEqual(result.refused, [WRITE])
        self.assertFalse(result.admitted)

    def test_a_vector_with_no_compiled_case_refuses(self):
        result, _ = self._run(GOOD, vectors=(WRITE, CHOOSE, IDLE))
        self.assertIn(IDLE, result.refused)

    def test_a_domain_with_no_vectors_is_reported_never_passed(self):
        result, written = self._run([], vectors=(), declare=False)
        self.assertEqual(result.unproven, [CHOOSE, IDLE, WRITE])
        self.assertEqual(result.proven, [])
        self.assertTrue(result.admitted)
        self.assertEqual(written["counts"]["proven"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
