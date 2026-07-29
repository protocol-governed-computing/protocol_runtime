#!/usr/bin/env bash
#
# PGC runtime runner — no PYTHONPATH / env fuss.
#
# Usage:
#   ./run.sh                                  # warm-boot the sibling assembled snapshot
#   ./run.sh boot --snapshot /abs/snapshot    # explicit boot
#   ./run.sh run --wf <domain>::WF_... --data-root /abs/instance   # execute (needs a domain WF)
#   ./run.sh examine /abs/trace.jsonl
#
# Env overrides:
#   PGC_SNAPSHOT_ROOT   assembled snapshot dir (default: sibling ../snapshot)
#   PGC_IMPL_ROOTS      colon-separated roots on PYTHONPATH for domain CT/CS impl imports
#                       (default: ../software_governance:../conformance_workloads)
#   PYTHON              (default: python)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"     # protocol_runtime/ = the `runtime` package root
UMBRELLA="$(cd "$SCRIPT_DIR/.." && pwd)"                       # protocol-governed-computing/
PYTHON="${PYTHON:-python}"

# Domain CT/CS implementations are imported by handler_ref module path at execution; their roots go
# on PYTHONPATH (env-provisioned — the runtime never manipulates sys.path). Default: both source
# repos — software_governance (capability_side_effects.*, capability_transforms.*) and
# conformance_workloads (workloads.collatz.implementation.*).
IMPL_ROOTS="${PGC_IMPL_ROOTS:-$UMBRELLA/software_governance:$UMBRELLA/conformance_workloads}"

export PYTHONPATH="$SCRIPT_DIR:$IMPL_ROOTS${PYTHONPATH:+:$PYTHONPATH}"
export PGC_SNAPSHOT_ROOT="${PGC_SNAPSHOT_ROOT:-$UMBRELLA/snapshot}"

if [[ $# -gt 0 ]]; then
  exec "$PYTHON" -m runtime.cli "$@"
fi

echo "PGC runtime — warm reboot"
echo "  runtime  : $SCRIPT_DIR (package: runtime)"
echo "  snapshot : $PGC_SNAPSHOT_ROOT"
echo

exec "$PYTHON" -m runtime.cli boot
