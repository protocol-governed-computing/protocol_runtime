import importlib
from typing import Any

from runtime.ct_errors import StructuredError

# importlib carve-out: permitted here for compile-time-sealed handler_ref execution.
# This is NOT discovery — the module path is embedded at compile time by materialize.py.
# Discovery via importlib is forbidden; execution of a sealed handler_ref is not.


class CTExecutionError(StructuredError):
    def __init__(self, message: str):
        super().__init__(
            error_code="CT_EXECUTION_FAILED",
            node_category="CT",
            message=message,
        )


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
    ) -> dict[str, Any]:
        """
        Execute a CT-IR program.

        Assumptions:
        - ct_ir is already validated for host invariants
        - atom_stream is structurally valid
        """
        ctx = _CTContext(inputs=inputs, input_types=ct_ir.get("input_types", {}))

        steps: list[dict] = ct_ir.get("atom_stream")
        if not steps:
            raise CTExecutionError("CT-IR missing atom_stream")

        for idx, step in enumerate(steps):
            atom = step.get("atom")
            if not atom:
                raise CTExecutionError(
                    f"Missing atom at index {idx}"
                )

            # Check for loop construct
            if "loop" in step:
                self._execute_loop(ctx, step)
            else:
                # ---- IR format execution ----
                # Pass complete step with all metadata + flattened args
                # Step from IR has nested args, adapter expects flattened
                invocation = {
                    **step,  # Include all metadata (input_types, output_types, etc.)
                    **step.get("args", {}),  # Flatten args to top level
                    "as": step.get("out")  # Normalize output key
                }

                self._execute_handler_ref(ctx, invocation)

        return ctx._vars

    def _execute_handler_ref(self, ctx: "_CTContext | _LoopContext", step: dict[str, Any]) -> None:
        """
        Execute an atom step by dispatching to its compile-time-sealed handler_ref.

        handler_ref is embedded in CT-IR at compile time by materialize.py.
        No registry lookup. No discovery. Sealed module path only.
        """
        handler_ref = step.get("handler_ref")
        if not handler_ref:
            raise CTExecutionError(f"CT-IR step missing handler_ref: {step.get('atom')}")
        module_path = handler_ref.get("module")
        callable_name = handler_ref.get("callable")
        if not module_path or not callable_name:
            raise CTExecutionError(f"Incomplete handler_ref on step: {step.get('atom')}")

        mod = importlib.import_module(module_path)
        execute_fn = getattr(mod, callable_name)

        # Adapter logic (migrated from atom_registry._register_execute_atom):
        # Resolve $.path references; skip reserved and metadata keys.
        RESERVED_KEYS = {"atom", "molecule", "kind", "as", "out", "loop", "args", "handler_ref", "input_types"}
        resolved_inputs: dict[str, Any] = {}
        for key, value in step.items():
            if key in RESERVED_KEYS:
                continue
            if isinstance(value, str) and value.startswith("$."):
                resolved_inputs[key] = ctx.resolve(value)
            else:
                resolved_inputs[key] = value

        try:
            result = execute_fn(inputs=resolved_inputs)
        except CTExecutionError:
            raise
        except Exception as exc:
            raise CTExecutionError(
                f"Atom raised exception: {step.get('atom')}: {exc}"
            ) from exc
        if result is None:
            raise CTExecutionError(f"Atom returned None: {step.get('atom')}")
        out_key = step.get("as") or step.get("out")
        if out_key:
            ctx.set_value(out_key, result)

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
        accumulator_spec = loop_spec.get("accumulator", {})
        accumulator = {}
        for key, value in accumulator_spec.items():
            if isinstance(value, str) and value.startswith("$."):
                # Resolve from results namespace
                if value.startswith("$.results."):
                    var_path = value[10:]  # Remove $.results.
                    parts = var_path.split(".", 1)
                    var_name = parts[0]
                    remaining = parts[1] if len(parts) > 1 else None
                    result = ctx.get_value(var_name)
                    if remaining and isinstance(result, dict):
                        for p in remaining.split("."):
                            result = result.get(p) if isinstance(result, dict) else None
                    accumulator[key] = result
                else:
                    accumulator[key] = ctx.resolve(value)
            else:
                accumulator[key] = value

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
                    field = result_path[10:]  # Remove $.results.
                    if isinstance(last_result, dict):
                        accumulator[acc_key] = last_result.get(field)

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
            return None

        parts = path[2:].split(".")
        if not parts:
            return None

        root = parts[0]

        if root == "accumulator":
            current = self._accumulator
        elif root == "iterator":
            return self._iterator_value
        elif root == "inputs":
            current = self._parent._inputs
        elif root == "results":
            if len(parts) > 1:
                var_name = parts[1]
                if var_name in self._vars:
                    current = self._vars[var_name]
                    parts = parts[1:]
                elif self._parent.has_value(var_name):
                    current = self._parent.get_value(var_name)
                    parts = parts[1:]
                else:
                    return None
            else:
                return None
        else:
            return None

        for part in parts[1:]:
            if isinstance(current, dict):
                current = current.get(part)
            else:
                return None

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
        """Resolve a JSONPath-like string (e.g. "$.inputs.foo.bar" or "$.results.var.field")"""
        if not path.startswith("$."):
            return None

        parts = path[2:].split(".")
        if not parts:
            return None

        root = parts[0]

        if root == "inputs":
            current = self._inputs
            remaining_parts = parts[1:]
        elif root == "results":
            if len(parts) < 2:
                return None
            var_name = parts[1]
            current = self._vars.get(var_name)
            remaining_parts = parts[2:]
        elif root in self._vars:
            current = self._vars[root]
            remaining_parts = parts[1:]
        else:
            return None

        for part in remaining_parts:
            if isinstance(current, dict):
                current = current.get(part)
            else:
                return None

        return current
