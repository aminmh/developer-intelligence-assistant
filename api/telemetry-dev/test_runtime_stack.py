"""Tests for mode-aware Runtime Compose startup sequencing."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

MODULE_PATH = Path(__file__).with_name("runtime_stack.py")
SPEC = importlib.util.spec_from_file_location("runtime_stack", MODULE_PATH)
runtime_stack = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(runtime_stack)


class RuntimeStackTests(unittest.TestCase):
    def setUp(self):
        self.commands: list[list[str]] = []
        self.mode = "local"

        def fake_run(command, **kwargs):
            self.commands.append(command)
            output = self.mode if "get_storage_target" in command[-1] else ""
            if "set_storage_mode" in command[-1]:
                self.mode = "remote" if "'remote'" in command[-1] else "local" if "'local'" in command[-1] else "disabled"
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

        self.runner = patch.object(runtime_stack.subprocess, "run", side_effect=fake_run)
        self.runner.start()
        self.addCleanup(self.runner.stop)

    def names(self):
        return [" ".join(command) for command in self.commands]

    def test_local_start_verifies_after_health_and_before_collector(self):
        self.mode = '"local"'
        self.assertEqual(runtime_stack.start_saved_mode(), "local")
        commands = self.names()
        local = next(i for i, command in enumerate(commands) if "telemetry-clickhouse" in command and "--wait" in command)
        verify = next(i for i, command in enumerate(commands) if "verify_local_clickhouse_connection" in command)
        collector = next(i for i, command in enumerate(commands) if "up -d telemetry-collector-runtime" in command)
        self.assertLess(local, verify)
        self.assertLess(verify, collector)
        self.assertFalse(any("stop telemetry-clickhouse" in command for command in commands))

    def test_remote_start_stops_local_and_verifies_before_collector(self):
        self.mode = '"remote"'
        runtime_stack.start_saved_mode()
        commands = self.names()
        local_stop = next(i for i, command in enumerate(commands) if "stop telemetry-clickhouse" in command)
        verify = next(i for i, command in enumerate(commands) if "verify_router_clickhouse_connection" in command)
        collector = next(i for i, command in enumerate(commands) if "up -d telemetry-collector-runtime" in command)
        self.assertLess(local_stop, verify)
        self.assertLess(verify, collector)
        self.assertFalse(any("--wait" in command and "telemetry-clickhouse" in command for command in commands))

    def test_disabled_start_starts_neither_database_nor_collector(self):
        self.mode = '"disabled"'
        runtime_stack.start_saved_mode()
        commands = self.names()
        self.assertTrue(any("stop telemetry-clickhouse" in command for command in commands))
        self.assertFalse(any("up -d telemetry-collector-runtime" in command for command in commands))
        self.assertFalse(any("--wait" in command and "telemetry-clickhouse" in command for command in commands))

    def test_remote_selection_applies_mode_before_starting_collector(self):
        runtime_stack.select_mode("remote")
        commands = self.names()
        apply = next(i for i, command in enumerate(commands) if "set_storage_mode" in command)
        collector = next(i for i, command in enumerate(commands) if "up -d telemetry-collector-runtime" in command)
        self.assertLess(apply, collector)
        self.assertTrue(any("stop telemetry-clickhouse" in command for command in commands))

    def test_unknown_mode_is_rejected_without_commands(self):
        with self.assertRaises(ValueError):
            runtime_stack.select_mode("fallback")
        self.assertEqual(self.commands, [])

    def test_collector_does_not_start_if_remote_verification_fails(self):
        def failing_run(command, **kwargs):
            self.commands.append(command)
            output = '"remote"' if "get_storage_target" in command[-1] else ""
            if "verify_router_clickhouse_connection" in command[-1]:
                raise subprocess.CalledProcessError(1, command, stderr="unavailable")
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

        with patch.object(runtime_stack.subprocess, "run", side_effect=failing_run):
            with self.assertRaises(subprocess.CalledProcessError):
                runtime_stack.start_saved_mode()
        self.assertFalse(any("up -d telemetry-collector-runtime" in " ".join(command) for command in self.commands))


if __name__ == "__main__":
    unittest.main()
