# PGS Runtime Test Suite

Comprehensive test suite for PGS Runtime execution layer using Python's standard `unittest` framework.

## Test Categories

Per CLAUDE.md Testing Requirements, tests are organized into 6 categories:

### 1. Snapshot Loading Tests (`test_snapshot_loading.py`)

Tests that runtime correctly loads artifacts from `protocol_snapshot/` directory.

- ✅ Snapshot root existence validation
- ✅ Workflow artifact loading
- ✅ RB artifact loading
- ✅ Intent resolution from snapshot
- ✅ Snapshot immutability during execution
- ✅ Missing artifact failure modes

### 2. CS Binding Tests (`test_cs_binding.py`)

Tests that RuntimeLoader loads RB artifacts and resolves CS runtimes from compile-time-sealed
`handler_ref` in compiled CS artifacts. No static registry — binding is sealed at compile time.

- ✅ RuntimeLoader RB path validation
- ✅ Parameter substitution ({{module_data_root}}, etc.)
- ✅ Missing module_data_root raises error
- ✅ Unknown CS artifact fails with clear error

### 3. Failure Mode Tests (`test_failure_modes.py`)

Tests that runtime fails fast and explicitly on errors.

- ✅ Fail on missing snapshot
- ✅ Fail on missing workflow artifact
- ✅ Fail on missing RB artifact
- ✅ Fail on unknown CS capability
- ✅ Fail on malformed RB artifact
- ✅ Fail on empty bindings
- ✅ Fail on invalid binding type
- ✅ Fail on missing required binding fields
- ✅ Fail on CT dynamic imports (forbidden)

### 4. Workflow Execution Tests (`test_workflow_execution.py`)

End-to-end workflow execution tests.

- ✅ Minimal workflow execution
- ✅ Execution trace generation
- ✅ Exit condition: SUCCESS
- ✅ Payload immutability
- ✅ ExecutionResult structure validation

### 5. Determinism Tests (`test_determinism.py`)

Tests that runtime execution is deterministic.

- ✅ Same input → same result
- ✅ Deterministic ID generation
- ✅ Admission gate actor_id derivation
- ✅ Snapshot-based execution reproducibility
- ✅ Trace IDs unique but execution deterministic
- ✅ JSON serialization deterministic (sorted keys)

### 6. CT Conformance Tests (`test_ct_conformance.py`)

Tests for CT execution via handler_ref embedded in CT-IR at compile time.
CT handler_ref binding is compile-time sealed — no runtime registry.

- ✅ handler_ref execution dispatches to correct atom callable
- ✅ $.inputs.* expression resolution in CT context
- ✅ Missing handler_ref raises CTExecutionError
- ✅ Atom returning None raises CTExecutionError
- ✅ Missing atom_stream raises CTExecutionError
- ✅ Multi-step result chaining via $.results.*

## Running Tests

```bash
# Run all tests
python -m unittest discover tests/

# Run specific test suite
python -m tests.test_snapshot_loading -v    # you can drop unittest

# Run specific test class
python -m unittest tests.test_snapshot_loading.TestSnapshotLoading

# Run specific test method
python -m unittest tests.test_snapshot_loading.TestSnapshotLoading.test_snapshot_root_must_exist

# Run with verbose output
python -m unittest discover tests/ -v

# Run all tests in verbose mode
python -m unittest discover -s tests -p 'test_*.py' -v
```

## Test Framework

Tests use Python's standard **unittest** framework (no external dependencies required).

### Base Test Class

All runtime tests inherit from `RuntimeTestCase` (defined in `test_helpers.py`):

```python
from testbed.implementations.tests import RuntimeTestCase


class TestMyFeature(RuntimeTestCase):
    def test_something(self):
        # self.snapshot_root - temporary snapshot directory
        # self.module_data_root - temporary module data directory
        # self.create_artifact_file() - helper to write artifacts
        # self.get_minimal_workflow_artifact() - minimal workflow
        # self.get_minimal_rb_artifact() - minimal RB
        pass
```

### Automatic Setup/Teardown

`RuntimeTestCase` provides:

- `setUp()`: Creates temporary directories for each test
  - `self.snapshot_root` - protocol snapshot with artifacts subdirs
  - `self.module_data_root` - module data directory
- `tearDown()`: Cleans up temporary directories after each test

### Helper Methods

- `create_artifact_file(artifact_type, artifact)` - Write artifact JSON to snapshot
- `get_minimal_workflow_artifact()` - Get minimal valid workflow dict
- `get_minimal_rb_artifact()` - Get minimal valid RB dict

## Test Principles

Per CLAUDE.md:

1. **Use snapshot artifacts as input** - All artifacts loaded from `protocol_snapshot/`
2. **Test against snapshot artifacts** - CS/CT binding is compile-time sealed; tests load from snapshot
3. **Verify execution determinism** - Same input always produces same output
4. **Test failure modes explicitly** - Verify fail-fast behavior, no silent degradation
5. **Mock external I/O** - Mock SMTP, filesystem operations when needed
6. **No compiler dependencies** - Runtime tests MUST NOT import `pgs_compiler.*`
7. **No protocol parsing** - Runtime tests MUST NOT parse `.md` files
8. **No structure.* imports** - Runtime tests use snapshot-based loading only

## Test Invariants

Tests enforce these architectural invariants:

- **Determinism**: Same snapshot + payload → same result
- **Snapshot-driven**: No protocol .md parsing
- **Registry-bound**: No dynamic capability discovery
- **Side-effect-explicit**: All mutations via declared CS
- **Fail-fast**: Invalid input → immediate error (no silent degradation)

## Adding New Tests

When adding tests:

1. Inherit from `RuntimeTestCase` for automatic setup/teardown
2. Use `self.snapshot_root` and `self.module_data_root` for temporary paths
3. Use helper methods for creating test artifacts
4. Test both success and failure modes
5. Verify determinism where applicable
6. Document test purpose with clear docstrings
7. Follow CLAUDE.md testing requirements

Example:

```python
from testbed.implementations.tests import RuntimeTestCase


class TestMyFeature(RuntimeTestCase):
    """Tests for my feature."""

    def test_feature_works(self):
        """Feature MUST work correctly."""
        # Create test artifacts
        wf = self.get_minimal_workflow_artifact()
        rb = self.get_minimal_rb_artifact()

        self.create_artifact_file("workflows", wf)
        self.create_artifact_file("capability_side_effects", rb)

        # Test your feature
        # ...

        self.assertEqual(expected, actual)
```

## Test Status

Current status: **All tests converted to unittest** ✅

- 6 test suites implemented
- 50+ test cases
- All architectural invariants covered
- No external test dependencies required
- Standard Python unittest framework

## No External Dependencies

Tests use only Python standard library:
- `unittest` - test framework
- `tempfile` - temporary directories
- `json` - JSON serialization
- `pathlib` - path handling
- `copy` - deep copying
- `os` - environment variables

No pip install required for testing!
