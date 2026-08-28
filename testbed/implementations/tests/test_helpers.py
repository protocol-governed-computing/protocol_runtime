"""
Test helpers for runtime tests (unittest framework).

Provides base test cases and helper functions.
"""

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any


class RuntimeTestCase(unittest.TestCase):
    """
    Base test case for runtime tests.

    Provides setUp/tearDown with temporary directories.
    """

    def setUp(self):
        """Create temporary directories for each test."""
        self.temp_dir = tempfile.mkdtemp()
        self.temp_path = Path(self.temp_dir)

        # Create snapshot structure
        self.snapshot_root = self.temp_path / "protocol_snapshot"
        artifacts = self.snapshot_root / "artifacts"
        (artifacts / "workflows").mkdir(parents=True)
        (artifacts / "intents").mkdir(parents=True)
        (artifacts / "capability_contracts").mkdir(parents=True)
        (artifacts / "capability_side_effects").mkdir(parents=True)
        (artifacts / "capability_transforms").mkdir(parents=True)
        (artifacts / "runtime_bindings").mkdir(parents=True)
        (artifacts / "structures").mkdir(parents=True)

        # Create module data root
        self.module_data_root = self.temp_path / "module_data"
        self.module_data_root.mkdir(parents=True)

        # Create trace root
        self.trace_root = self.temp_path / "traces"
        self.trace_root.mkdir(parents=True)

    def tearDown(self):
        """Clean up temporary directories."""
        import shutil
        if self.temp_path.exists():
            shutil.rmtree(self.temp_path)

    def create_artifact_file(self, artifact_type: str, artifact: dict[str, Any]) -> Path:
        """
        Helper to write an artifact JSON file to the snapshot.

        Args:
            artifact_type: Subdirectory name (workflows, intents, etc.)
            artifact: Artifact dict to serialize

        Returns:
            Path to created file
        """
        artifacts_dir = self.snapshot_root / "artifacts"
        artifact_dir = artifacts_dir / artifact_type
        fqdn_id = artifact.get("fqdn_id", artifact.get("artifact_code", "UNKNOWN"))
        # Match gateway's _fqdn_to_filename: replace '::' with '__'
        filename = fqdn_id.replace("::", "__")
        artifact_file = artifact_dir / f"{filename}.json"

        with open(artifact_file, 'w') as f:
            json.dump(artifact, f, indent=2)

        return artifact_file

    def get_minimal_workflow_artifact(self) -> dict[str, Any]:
        """Get minimal valid workflow artifact for testing (IN → EXIT only, no CC/CS)."""
        return {
            "fqdn_id": "TEST::WF_MINIMAL_V0",
            "artifact_code": "WF_MINIMAL_V0",
            "namespace": "TEST",
            "frontmatter": {
                "wf_code": "TEST::WF_MINIMAL_V0",
                "runtime_binding": "TEST::RB_MINIMAL_V0",
                "core": {
                    "start_node": "validate",
                    "nodes": {
                        "validate": {
                            "type": "IN",
                            "fqdn_id": "TEST::IN_MINIMAL_V0",
                            "next": {
                                "ACK": "exit",
                                "NACK": "exit"
                            }
                        },
                        "exit": {
                            "type": "EXIT",
                            "reason": "COMPLETED"
                        }
                    }
                }
            }
        }

    def get_minimal_rb_artifact(self) -> dict[str, Any]:
        """Get minimal valid runtime binding artifact for testing."""
        return {
            "fqdn_id": "TEST::RB_MINIMAL_V0",
            "artifact_code": "RB_MINIMAL_V0",
            "frontmatter": {
                "core": {
                    "bindings": {
                        "testbed::CS_TEST_APPEND_V0": {
                            "policy": {}
                        }
                    }
                }
            }
        }

    def get_test_cs_artifact(self) -> dict[str, Any]:
        """Get CS artifact wired to the testbed TestAppendRuntime."""
        return {
            "fqdn_id": "testbed::CS_TEST_APPEND_V0",
            "artifact_code": "CS_TEST_APPEND_V0",
            "cs_ir": {
                "handler_ref": {
                    "module": "testbed.implementations.test_cs_runtime",
                    "callable": "TestAppendRuntime"
                },
                "cs_metadata": {
                    "capability": {
                        "supported_operation_specs": ["APPEND", "GET_ALL"]
                    }
                }
            }
        }
