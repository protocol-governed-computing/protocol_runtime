"""
A composed step's outcome selects a declared continuation, and the trace records both.

The dispatcher read a step's continuation with `on_result.get(outcome, "continue")`, so an outcome
the contract declared nothing for proceeded to the next step. A failed lookup carried on that way,
and a person was accepted (SoSyM study, case O3). It now refuses there (`3a` EX-18). The step record
carried only the names of the step's results, so nothing in the trace showed the decision; it now
records the outcome and the continuation it selected (`3e` EV-19).

Runs AI Licensing's reclaim against the assembled snapshot. The refusal case has the registry's
removal answer an outcome the reclaim contract declares nothing for, in that run only.
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[3]
SNAPSHOT = WORKSPACE / "snapshot"
DOMAIN = WORKSPACE / "business_domains" / "ai_governance"
STORE = "ai_governance/ai_licensing"

# Test-only environment provisioning, as `run.sh` provides on PYTHONPATH: the platform's side effects
# and the domain's transforms. The runtime itself never manipulates sys.path.
for _root in (WORKSPACE / "software_governance", WORKSPACE / "business_domains"):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

from capability_side_effects.implementation.CS_REGISTRY_V0.impl.executor import RegistryExecutor  # noqa: E402
from runtime import api  # noqa: E402
from runtime.dispatcher import UnlistedStepOutcomeError  # noqa: E402

RECLAIM = {"license_id": "lic-7526", "threshold_days": 30,
           "context": {"employee_id": "e-7526", "last_active_date": "2026-01-01T00:00:00Z",
                       "evaluation_date": "2026-06-01T00:00:00Z", "days_inactive": 151}}


def provisioned() -> Path:
    root = Path(tempfile.mkdtemp(prefix="pgc_step_outcome_"))
    (root / STORE).mkdir(parents=True)
    shutil.copy(DOMAIN / "testbed" / "agent_governance" / "seed_data" / "license_facts.json",
                root / STORE)
    payload = json.loads((DOMAIN / "testbed" / "ai_licensing" / "test_payloads"
                          / "provision_ai_licensing_payload.json").read_text())
    api.run_workflow(wf_fqdn="ai_governance::WF_PROVISION_AI_LICENSING_V0", payload=payload,
                     snapshot_root=str(SNAPSHOT), data_root=str(root))
    return root


def events(root: Path, wf: str) -> list[dict]:
    traces = sorted((root / "traces" / "ai_governance" / wf).rglob("*.jsonl"))
    return [json.loads(line) for line in traces[-1].read_text().splitlines() if line.strip()]


def test_every_step_record_carries_its_outcome_and_continuation():
    root = provisioned()
    try:
        api.run_workflow(wf_fqdn="ai_governance::WF_AUTO_RECLAIM_V0", payload=RECLAIM,
                         snapshot_root=str(SNAPSHOT), data_root=str(root))
        steps = [e["detail"] for e in events(root, "WF_AUTO_RECLAIM_V0")
                 if e["event_type"] == "CC_STEP" and e.get("step_op") != "ADMIT"]
        assert steps and all(d.get("outcome") and d.get("continuation") in ("continue", "exit")
                             for d in steps), steps
        removal = [d for d in steps if d["step_fqdn"].endswith("CS_REGISTRY_V0")]
        assert removal and removal[0]["outcome"] == "SUCCESS" and removal[0]["continuation"] == "exit", removal
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_an_admission_record_names_the_route_as_its_continuation():
    root = provisioned()
    try:
        admits = [e["detail"] for e in events(root, "WF_PROVISION_AI_LICENSING_V0")
                  if e["event_type"] == "CC_STEP" and e.get("step_op") == "ADMIT"]
        assert admits and all(d["outcome"] == "ACK" and d["continuation"] == "route"
                              for d in admits), admits
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_an_outcome_with_no_declared_continuation_refuses_and_runs_nothing_after_it():
    root = provisioned()
    real = RegistryExecutor.deregister
    RegistryExecutor.deregister = lambda self, payload: {"result_status": "ALREADY_EXISTS"}
    try:
        try:
            api.run_workflow(wf_fqdn="ai_governance::WF_AUTO_RECLAIM_V0", payload=RECLAIM,
                             snapshot_root=str(SNAPSHOT), data_root=str(root))
        except UnlistedStepOutcomeError:
            pass
        else:
            raise AssertionError("an unlisted step outcome did not refuse")
        ev = events(root, "WF_AUTO_RECLAIM_V0")
        removal = [e["detail"] for e in ev if e["event_type"] == "CC_STEP"
                   and e["detail"]["step_fqdn"].endswith("CS_REGISTRY_V0")]
        assert removal == [dict(removal[0], outcome="ALREADY_EXISTS", continuation=None)], removal
        assert any(e["event_type"] == "ERROR" and e["detail"].get("outcome") == "ALREADY_EXISTS"
                   for e in ev), "no ERROR records the refusal"
        assert not any(e["event_type"] == "EVENT" for e in ev), "an announcement followed the refusal"
        assert sum(e["event_type"] == "CC_START" for e in ev) == 1, "a contract ran after the refusal"
        assert [e for e in ev if e["event_type"] == "WF_COMPLETE"][-1]["result_status"] == "VIOLATION"
    finally:
        RegistryExecutor.deregister = real
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    if not (SNAPSHOT / "manifest.json").is_file():
        print("SKIP  no assembled snapshot")
        sys.exit(0)
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as exc:  # report every test, then fail the run
            failed += 1
            print(f"  FAIL  {name}: {exc!r}"[:1500])
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
