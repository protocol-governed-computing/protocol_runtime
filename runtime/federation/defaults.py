"""Deployment defaults for the federated roles — the only defaults the runtime carries.

Ruled deployment configuration, not behaviour (v5 ruling C2/C3). The sealed placement decides
*whether* execution is federated and how a unit is admitted, executed and evidenced; nothing here
changes any of that. These say where a coordinator listens and how long a node waits to *connect*
to one — facts about a deployment, which a snapshot sealed once and run on many deployments cannot
know. Each is overridable by its CLI option or environment variable, and they are gathered here so
that "which constants does the runtime hold" has one answer.

`COORDINATOR_BIND` is loopback on purpose: a coordinator started without being told where to listen
is reachable from this host only. Exposing one is a decision the operator states.
"""

COORDINATOR_BIND = "127.0.0.1"   # --bind / PGC_COORDINATOR_BIND
COORDINATOR_PORT = 8100          # --port / PGC_COORDINATOR_PORT

# Bounds connecting only. Once connected a node waits without limit: a coordinator blocked on the
# evidence store may still admit the unit, and a caller that gave up would misreport it as failed.
CONNECT_TIMEOUT_S = 10.0

# How often a submitter asks whether an admitted unit has an outcome yet.
OUTCOME_POLL_S = 0.1
