"""`FEDERATED_NODE` placement: a coordinator and workers on separately addressable nodes.

`CONSTITUTION_EXECUTION_PLACEMENT_V1` §5a authorizes several nodes under one authority, with work
reaching a node across a network. This package realizes that arrangement and nothing more. Every
worker calls `run_workflow` exactly as a lone runtime would, against the same sealed snapshot, so a
determination does not depend on which node reached it.

The parties meet only at the evidence store, a directory every node mounts and none holds:

    queue/<unit_id>.unit.json        written once by the coordinator
    claims/<unit_id>.claim.json      created exclusively by the worker that takes the unit
    outcomes/<unit_id>.outcome.json  written once by that worker

`LOCAL_MULTI_WORKER` stays realized by `runtime.coordinator`. The two are separate because the
placement modes are: a process pool is not addressable at any host count.
"""
