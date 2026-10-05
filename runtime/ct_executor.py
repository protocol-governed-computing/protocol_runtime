import importlib
from typing import Any, Callable

from runtime.ct_errors import StructuredError

# importlib carve-out: permitted here for compile-time-sealed handler_ref execution.
# This is NOT discovery — the module path is embedded at compile time by materialize.py.
# Discovery via importlib is forbidden; execution of a sealed handler_ref is not.


# An atom declaring this purity gives a result not determined by its inputs; its result is recorded
# when produced and substituted on replay (capability_transforms::CONSTITUTION_NONDETERMINISTIC_ATOMS_V0).
NONDETERMINISTIC_PURITY = "ct_impure"


class CTExecutionError(StructuredError):
    def __init__(self, message: str):
        super().__init__(
            error_code="CT_EXECUTION_FAILED",
            node_category="CT",
            message=message,
        )


# The name an atom's refusal is raised under. See `_execute_handler_ref`.
REFUSAL_SIGNAL = "CTExecutionError"


class CTFault(StructuredError):
    """A transform failed in a way its declaration does not answer for.

    Distinct from `CTExecutionError`, which an atom raises to refuse: that is a declared outcome, and
    the contract routes on it as VIOLATION. A fault is not an outcome at all. The module named by
    the snapshot did not load, the sealed CT-IR was malformed, or the atom broke instead of
    answering. Routing on it would be routing on an error class (`3a` §4.1), so execution refuses
    there instead (`3c` §7).
    """

    def __init__(self, message: str, cause: Exception | None = None,
                 error_code: str = "CT_EXECUTION_FAILED"):
        super().__init__(
            error_code=error_code,
            node_category="CT",
            message=message,
            cause=cause,
        )


class CTArtifactNotFound(CTFault):
    """The sealed handler_ref names something that is not present.

    Distinct from CT_EXECUTION_FAILED: nothing was executed and nothing could be. The snapshot
    named a module or callable, and resolution of that name failed.
    """

    def __init__(self, message: str, cause: Exception | None = None):
        super().__init__(message, cause=cause, error_code="CT_ARTIFACT_NOT_FOUND")


class CTExecutor:
    def __init__(self):
        pass

    # ---------------------------------------------------------
    # Public entrypoint
    # ---------------------------------------------------------

    def execute(
        self,
        *,
        ct_ir: dict[str, Any],
        inputs: dict[str, Any],
        observer: "Callable[[dict[str, Any]], None] | None" = None,
        recorded: "Callable[[str], Any] | None" = None,
    ) -> dict[str, Any]:
        """
        Execute a CT-IR program.

        Assumptions:
        - ct_ir is already validated for host invariants
        - atom_stream is structurally valid

        `observer`, when given, receives one record per atom run, at any depth of a molecule: its
        path, its identity, its declared purity and the names of its results — and, for an atom
        declared not deterministic, the result's values, which are determining evidence.

        `recorded`, when given, is a replay: an atom declared not deterministic is never run, and
        its recorded result for the same path is used instead. A path with no recorded result is
        refused rather than run, because running it would make the replay a new execution.
        """
        ctx = _CTContext(inputs=inputs, input_types=ct_ir.get("input_types", {}))

        steps: list[dict] = ct_ir.get("atom_stream")
        if not steps:
            raise CTFault("CT-IR missing atom_stream")

        self._run_stream(ctx, steps, "", observer, recorded)
        return ctx._vars

    # ---------------------------------------------------------
    # A sealed stream: atoms, molecules and loops, in declared order
    # ---------------------------------------------------------

    def _run_stream(self, ctx, steps, prefix, observer, recorded) -> None:
        for idx, step in enumerate(steps):
            if not step.get("atom"):
                raise CTFault(f"Missing atom at index {idx}")
            symbol = step.get("out") or step.get("as") or f"#{idx}"
            if "molecule" in step and "loop" in step:
                self._run_loop_body(ctx, step, f"{prefix}{symbol}", observer, recorded)
            elif "molecule" in step:
                args = {k: (ctx.resolve(v) if isinstance(v, str) and v.startswith("$.") else v)
                        for k, v in (step.get("args") or {}).items()}
                result = self._run_molecule(step["molecule"], args, f"{prefix}{symbol}/", observer, recorded)
                if step.get("out"):
                    ctx.set_value(step["out"], result)
            elif "loop" in step:
                self._execute_loop(ctx, step)
            else:
                invocation = {
                    **step,
                    **step.get("args", {}),
                    "as": step.get("out"),
                }
                self._execute_handler_ref(ctx, invocation, path=f"{prefix}{symbol}",
                                          observer=observer, recorded=recorded)

    def _run_molecule(self, body, inputs, prefix, observer, recorded) -> Any:
        """Run a sealed molecule body with its own inputs; return the one value it emits."""
        child = _CTContext(inputs=inputs)
        self._run_stream(child, body.get("atom_stream") or [], prefix, observer, recorded)
        outputs = body.get("outputs") or {}
        if len(outputs) != 1:
            raise CTFault(f"A molecule emits exactly one value; this one declares {len(outputs)}")
        (spec,) = outputs.values()
        if not child.has_value(spec["from"]):
            raise CTFault(f"Molecule emission '{spec['from']}' was not produced")
        return child.get_value(spec["from"])

    def _run_loop_body(self, ctx, step, path, observer, recorded) -> None:
        """Run a loop whose body is a molecule: once per member of the collection, every pass."""
        spec = step["loop"]
        collection = ctx.resolve(spec.get("over")) if spec.get("over") else []
        if not isinstance(collection, (list, tuple)):
            raise CTExecutionError(f"Loop 'over' must resolve to a list: {spec.get('over')}")
        accumulator = self._initial_accumulator(ctx, spec.get("accumulator", {}))
        last_result = None
        for n, item in enumerate(collection):
            loop_ctx = _LoopContext(ctx, accumulator, spec.get("iterator") or "item", item)
            inputs = {k: (loop_ctx.resolve(v) if isinstance(v, str) and v.startswith("$.") else v)
                      for k, v in (spec.get("inputs") or {}).items()}
            last_result = self._run_molecule(step["molecule"], inputs, f"{path}[{n}]/", observer, recorded)
            for acc_key, result_path in (spec.get("update_accumulator") or {}).items():
                if isinstance(result_path, str) and result_path.startswith("$.results."):
                    accumulator[acc_key] = _walk(last_result, result_path[10:].split("."), result_path)
        if step.get("out") and last_result is not None:
            ctx.set_value(step["out"], last_result)

    @staticmethod
    def _initial_accumulator(ctx, accumulator_spec) -> dict[str, Any]:
        accumulator = {}
        for key, value in accumulator_spec.items():
            accumulator[key] = (ctx.resolve(value)
                                if isinstance(value, str) and value.startswith("$.") else value)
        return accumulator

    def _execute_handler_ref(self, ctx: "_CTContext | _LoopContext", step: dict[str, Any],
                             path: str = "", observer=None, recorded=None) -> None:
        """
        Execute an atom step by dispatching to its compile-time-sealed handler_ref.

        handler_ref is embedded in CT-IR at compile time by materialize.py.
        No registry lookup. No discovery. Sealed module path only.
        """
        handler_ref = step.get("handler_ref")
        if not handler_ref:
            raise CTFault(f"CT-IR step missing handler_ref: {step.get('atom')}")
        purity = step.get("purity")
        out_key = step.get("as") or step.get("out")
        if recorded is not None and purity == NONDETERMINISTIC_PURITY:
            # Replay: the atom is not run. Its recorded result stands in for it, which is what makes
            # the replay reproduce the determination rather than draw a new one.
            result = recorded(path)
            if out_key:
                ctx.set_value(out_key, result)
            self._observe(observer, path, step, purity, result, replayed=True)
            return
        module_path = handler_ref.get("module")
        callable_name = handler_ref.get("callable")
        if not module_path or not callable_name:
            raise CTFault(f"Incomplete handler_ref on step: {step.get('atom')}")

        # Importing a sealed handler_ref is a resolution step, and it fails in two ways that mean
        # different things. The module named by the snapshot may be absent — a closure failure. Or
        # the module may be present and one of *its* imports absent, which is what happens when a
        # domain's optional dependency is not installed. Neither may escape as a bare
        # ModuleNotFoundError: an ungoverned crash is not a declared outcome.
        try:
            mod = importlib.import_module(module_path)
        except ModuleNotFoundError as exc:
            missing = exc.name or ""
            if missing == module_path or module_path.startswith(missing + "."):
                raise CTArtifactNotFound(
                    f"handler_ref names module {module_path!r} for atom "
                    f"{step.get('atom')!r} and it is not importable",
                    cause=exc,
                ) from exc
            raise CTFault(
                f"handler_ref module {module_path!r} for atom {step.get('atom')!r} requires "
                f"{missing!r}, which is not installed — the domain's optional dependency is "
                f"missing, not the transform"
            ) from exc
        except ImportError as exc:
            raise CTFault(
                f"handler_ref module {module_path!r} for atom {step.get('atom')!r} "
                f"failed to import: {exc}", cause=exc
            ) from exc

        try:
            execute_fn = getattr(mod, callable_name)
        except AttributeError as exc:
            raise CTArtifactNotFound(
                f"handler_ref names callable {callable_name!r} in {module_path!r} for atom "
                f"{step.get('atom')!r} and the module does not define it",
                cause=exc,
            ) from exc

        # Adapter logic (migrated from atom_registry._register_execute_atom):
        # Resolve $.path references; skip reserved and metadata keys.
        RESERVED_KEYS = {"atom", "molecule", "kind", "as", "out", "loop", "args", "handler_ref",
                         "input_types", "purity"}
        # A step that declares its arguments is handed exactly those. Reading them back out of the
        # flattened step dropped any argument whose name is also a step key: a transform taking
        # `kind` was handed nothing for it, because `kind` is how a step says what it is.
        declared = step.get("args")
        source = declared.items() if isinstance(declared, dict) else (
            (key, value) for key, value in step.items() if key not in RESERVED_KEYS)
        resolved_inputs: dict[str, Any] = {}
        for key, value in source:
            if isinstance(value, str) and value.startswith("$."):
                resolved_inputs[key] = ctx.resolve(value)
            else:
                resolved_inputs[key] = value

        # An atom refuses by raising `CTExecutionError`; that is its declared outcome and passes
        # through. Anything else it raises is a defect, not an answer, and is a fault.
        #
        # The refusal is recognised by the class's name, not its identity. Atoms are implementations
        # outside this package, and most cannot import it: fourteen define their own class of that
        # name, and the platform's reference transforms raise a vendored one. The name is the
        # convention every one of them follows, so the name is the signal.
        try:
            result = execute_fn(inputs=resolved_inputs)
        except (CTExecutionError, CTFault):
            raise
        except Exception as exc:
            if type(exc).__name__ == REFUSAL_SIGNAL:
                raise CTExecutionError(str(exc)) from exc
            raise CTFault(
                f"Atom raised exception: {step.get('atom')}: {type(exc).__name__}: {exc}", cause=exc
            ) from exc
        if result is None:
            raise CTFault(f"Atom returned None: {step.get('atom')}")
        if out_key:
            ctx.set_value(out_key, result)
        self._observe(observer, path, step, purity, result, replayed=False)

    @staticmethod
    def _observe(observer, path, step, purity, result, replayed) -> None:
        """One evidence record per atom run: result names always, values only where they determine."""
        if observer is None:
            return
        record = {
            "path": path,
            "step_fqdn": step.get("atom"),
            "purity": purity,
            "result_keys": sorted(result.keys()) if isinstance(result, dict) else [],
        }
        if purity == NONDETERMINISTIC_PURITY:
            record["outcome"] = result
            record["replayed"] = replayed
        observer(record)

    def _execute_loop(
        self,
        ctx: "_CTContext",
        step: dict[str, Any],
    ) -> None:
        """Execute a loop construct."""
        loop_spec = step["loop"]
        out_key = step.get("out")

        # Resolve the collection to iterate over
        over_path = loop_spec.get("over")
        collection = ctx.resolve(over_path) if over_path else []
        if not isinstance(collection, (list, tuple)):
            raise CTExecutionError(f"Loop 'over' must resolve to a list: {over_path}")

        iterator_name = loop_spec.get("iterator", "item")

        # Initialize accumulator
        accumulator = self._initial_accumulator(ctx, loop_spec.get("accumulator", {}))

        loop_inputs_spec = loop_spec.get("inputs", {})
        update_spec = loop_spec.get("update_accumulator", {})

        last_result = None

        for item in collection:
            # Build inputs for this iteration
            loop_ctx = _LoopContext(ctx, accumulator, iterator_name, item)

            # Resolve loop inputs
            resolved_inputs = {}
            for key, value in loop_inputs_spec.items():
                if isinstance(value, str) and value.startswith("$."):
                    resolved_inputs[key] = loop_ctx.resolve(value)
                else:
                    resolved_inputs[key] = value

            # Build invocation (preserve ALL step metadata: input_types, output_types, etc.)
            # Step from IR has nested args, adapter expects flattened
            invocation = {
                **step,  # Include all metadata (input_types, output_types, loop, etc.)
                **resolved_inputs,  # Flatten resolved args to top level
                "as": "__loop_result__"  # Override output key for loop
            }

            self._execute_handler_ref(loop_ctx, invocation)
            last_result = loop_ctx.get_value("__loop_result__")

            # Update accumulator from results
            for acc_key, result_path in update_spec.items():
                if isinstance(result_path, str) and result_path.startswith("$.results."):
                    accumulator[acc_key] = _walk(last_result, result_path[10:].split("."), result_path)

        # Store final result
        if out_key and last_result is not None:
            ctx.set_value(out_key, last_result)


class _LoopContext:
    """Context wrapper for loop iterations with accumulator and iterator."""

    def __init__(self, parent_ctx: "_CTContext", accumulator: dict, iterator_name: str, iterator_value: Any):
        self._parent = parent_ctx
        self._accumulator = accumulator
        self._iterator_name = iterator_name
        self._iterator_value = iterator_value
        self._vars: dict[str, Any] = {}

    def get_input(self, name: str) -> Any:
        return self._parent.get_input(name)

    def set_value(self, name: str, value: Any) -> None:
        self._vars[name] = value

    def get_value(self, name: str) -> Any:
        return self._vars.get(name)

    def has_value(self, name: str) -> bool:
        return name in self._vars

    def resolve(self, path: str) -> Any:
        if not path.startswith("$."):
            raise CTFault(f"CT-IR path {path!r} is not a path")
        parts = path[2:].split(".")
        root = parts[0]
        if root == "accumulator":
            return _walk(self._accumulator, parts[1:], path)
        if root == "iterator":
            return _walk(self._iterator_value, parts[1:], path)
        if root == "inputs":
            return _walk(self._parent._inputs, parts[1:], path)
        if root == "results" and len(parts) > 1:
            if parts[1] in self._vars:
                return _walk(self._vars[parts[1]], parts[2:], path)
            if self._parent.has_value(parts[1]):
                return _walk(self._parent.get_value(parts[1]), parts[2:], path)
            raise CTFault(f"CT-IR path {path!r} names a result no step has produced")
        raise CTFault(f"CT-IR path {path!r} has no root this context resolves")


def _walk(current: Any, parts: list[str], path: str) -> Any:
    """Follow a path into a value; a path that reaches nothing refuses, never None.

    A value present as null is a value, and is returned. An absent key, or a step into something
    that is not a mapping, reaches nothing: handing the atom None in its place would supply a
    default the composition never declared (`3a` RT-6).
    """
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            raise CTFault(f"CT-IR path {path!r} reaches nothing at {part!r}")
        current = current[part]
    return current


# ---------------------------------------------------------
# Internal execution context (CT-local)
# ---------------------------------------------------------

class _CTContext:
    """_CTContext — isolated CT execution state."""

    def __init__(self, *, inputs: dict[str, Any], input_types: dict[str, str] | None = None):
        self._inputs = dict(inputs)
        self._vars: dict[str, Any] = {}
        self.input_types = input_types or {}

    def get_input(self, name: str) -> Any:
        return self._inputs.get(name)

    def set_value(self, name: str, value: Any) -> None:
        self._vars[name] = value

    def get_value(self, name: str) -> Any:
        return self._vars.get(name)

    def has_value(self, name: str) -> bool:
        return name in self._vars

    def resolve(self, path: str) -> Any:
        """Resolve a JSONPath-like string (e.g. "$.inputs.foo.bar" or "$.results.var.field")."""
        if not path.startswith("$."):
            raise CTFault(f"CT-IR path {path!r} is not a path")
        parts = path[2:].split(".")
        root = parts[0]
        if root == "inputs":
            return _walk(self._inputs, parts[1:], path)
        if root == "results" and len(parts) > 1:
            if parts[1] not in self._vars:
                raise CTFault(f"CT-IR path {path!r} names a result no step has produced")
            return _walk(self._vars[parts[1]], parts[2:], path)
        if root in self._vars:
            return _walk(self._vars[root], parts[1:], path)
        raise CTFault(f"CT-IR path {path!r} has no root this context resolves")
