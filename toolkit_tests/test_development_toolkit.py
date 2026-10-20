"""Offline unit smoke tests for the published Development Toolkit wrapper."""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from development_toolkit import Tools, ToolkitError  # noqa: E402


class ToolkitTests(unittest.TestCase):
    def setUp(self):
        self.tool = Tools()

    def test_no_bundled_api_key(self):
        self.assertEqual(self.tool.valves.API_KEY, "")

    def test_workspace_paths(self):
        self.assertEqual(self.tool._safe_path("."), "/workspace")
        self.assertEqual(self.tool._safe_path("a/../b.txt"), "/workspace/b.txt")
        self.assertEqual(self.tool._safe_path("/workspace/project"), "/workspace/project")

    def test_path_escape_rejected(self):
        for p in ("../etc/passwd", "/etc/passwd", "/workspace2/x", "project/../../etc"):
            with self.subTest(path=p), self.assertRaises(ValueError):
                self.tool._safe_path(p)

    def test_no_api_key_request_rejected(self):
        with self.assertRaisesRegex(ToolkitError, "API key is missing"):
            self.tool._request_sync("GET", "/api/config")

    def test_diagnostic_excludes_key_material(self):
        self.tool.valves.API_KEY = "test-only-secret"
        out = json.loads(asyncio.run(self.tool._diagnose_runtime_config()))
        self.assertEqual(out["api_key_configured"], True)
        self.assertNotIn("test-only-secret", json.dumps(out))
        self.assertNotIn("api_key_sha256_prefix", out)

    def test_diagnostic_no_network(self):
        self.tool._request = AsyncMock(side_effect=AssertionError("network called"))
        asyncio.run(self.tool._diagnose_runtime_config())
        self.tool._request.assert_not_called()

    def test_fixed_diagnostic_command_uses_configured_workspace(self):
        self.tool.valves.WORKSPACE_ROOT = "/home/user/project"
        self.tool.valves.CONFIRM_SHELL_COMMANDS = False
        self.tool._request = AsyncMock(return_value={"status": "ok"})
        result = asyncio.run(self.tool._test_open_terminal_execute())
        self.assertEqual(json.loads(result), {"status": "ok"})
        self.assertEqual(self.tool._request.call_args.kwargs["body"]["cwd"], "/home/user/project")
        self.assertEqual(self.tool._request.call_args.kwargs["body"]["command"], "echo TOOLKIT_EXECUTE_TEST")

    def test_command_default_cwd_uses_valve(self):
        self.tool.valves.WORKSPACE_ROOT = "/home/user/project"
        self.tool.valves.CONFIRM_SHELL_COMMANDS = False
        self.tool._request = AsyncMock(return_value={"status": "ok"})
        asyncio.run(self.tool.dev_run_command("echo offline"))
        self.assertEqual(self.tool._request.call_args.kwargs["body"], {"command": "echo offline", "cwd": "/home/user/project"})

    def test_run_command_rejects_escape_cwd(self):
        self.tool._request = AsyncMock()
        response = json.loads(asyncio.run(self.tool.dev_run_command("echo hello", cwd="../outside")))
        self.assertEqual(response["error"], "INVALID_ARGUMENT")
        self.tool._request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
