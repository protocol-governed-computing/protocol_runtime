"""
Capability side effects under concurrent writers in separate processes.

`LOCAL_MULTI_WORKER` and `FEDERATED_NODE` both place several writers on one store — processes on one
host, or workers on several hosts sharing it. A capability that reads, decides and writes back is
correct there only if its writers exclude each other across processes. Each case below starts
writers as separate processes, so a lock held inside one process cannot pass it.

Skips when the governance surface's capability implementations are not importable.
"""

import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path

try:
    from capability_side_effects.implementation.CS_APPENDONLY_JSONL_V0.impl.executor import AppendOnlyJsonlEngine
    from capability_side_effects.implementation.CS_MUTABLE_JSON_V0.impl.executor import MutableJsonEngine
    from capability_side_effects.implementation.CS_REGISTRY_V0.errors import RegistryKeyExists
    from capability_side_effects.implementation.CS_REGISTRY_V0.impl.backend import RegistryBackend
    _HAVE_CAPABILITIES = True
except ImportError:
    _HAVE_CAPABILITIES = False

WRITERS, EACH = 8, 25


def _config(root: str, entity: str, subpath: str) -> dict:
    return {"module_data_root": root,
            "storage_structure_artifact": {"frontmatter": {"core": {"entity_stores": {entity: {"path": subpath}}}}}}


def _write_records(root: str, writer: int, start) -> None:
    start.wait()
    engine = MutableJsonEngine(_config(root, "RECORD", "records.json"))
    for i in range(EACH):
        engine.write({"__pgs_store_entity__": "RECORD", "key": f"w{writer}-{i}", "value": {"n": i}})


def _claim_keys(root: str, writer: int, start, claimed) -> None:
    start.wait()
    backend = RegistryBackend({"path": str(Path(root) / "registry.jsonl")})
    for i in range(EACH):
        try:
            backend.register(f"key-{i}")
            claimed.put(i)
        except RegistryKeyExists:
            pass


def _append_records(root: str, writer: int, start) -> None:
    start.wait()
    engine = AppendOnlyJsonlEngine(_config(root, "LOG", "log.jsonl"))
    for i in range(EACH):
        engine.append({"__pgs_store_entity__": "LOG", "record": {"writer": writer, "i": i}})


@unittest.skipUnless(_HAVE_CAPABILITIES, "capability implementations not importable")
class CapabilityConcurrencyTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="pgc_cs_race_")
        self.ctx = multiprocessing.get_context("spawn")

    def _race(self, target, *extra) -> None:
        start = self.ctx.Event()
        procs = [self.ctx.Process(target=target, args=(self.root, w, start, *extra)) for w in range(WRITERS)]
        for p in procs:
            p.start()
        start.set()
        for p in procs:
            p.join(timeout=120)
            self.assertEqual(p.exitcode, 0, "a writer process failed")

    def test_mutable_json_loses_no_update(self):
        self._race(_write_records)
        records = json.loads((Path(self.root) / "records.json").read_text())
        self.assertEqual(len(records), WRITERS * EACH, "an update was overwritten by another writer")

    def test_registry_records_each_claim_once(self):
        claimed = self.ctx.Queue()
        self._race(_claim_keys, claimed)
        wins = []
        while not claimed.empty():
            wins.append(claimed.get())
        lines = [json.loads(l)["key"] for l in (Path(self.root) / "registry.jsonl").read_text().splitlines() if l]
        self.assertEqual(sorted(wins), list(range(EACH)), "a key was claimed more than once, or not at all")
        self.assertEqual(sorted(lines), sorted(f"key-{i}" for i in range(EACH)))

    def test_append_log_numbers_each_record_once(self):
        self._race(_append_records)
        entries = [json.loads(l) for l in (Path(self.root) / "log.jsonl").read_text().splitlines() if l]
        self.assertEqual(len(entries), WRITERS * EACH, "an append was lost or interleaved")
        self.assertEqual(sorted(e["sequence_number"] for e in entries), list(range(1, WRITERS * EACH + 1)),
                         "two appends took the same sequence number")


if __name__ == "__main__":
    unittest.main()
