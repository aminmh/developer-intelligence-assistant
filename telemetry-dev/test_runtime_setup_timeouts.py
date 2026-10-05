import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_setup


class RuntimeHelperGumTests(unittest.TestCase):
    def test_gum_attaches_to_controlling_terminal_when_helper_stdin_is_redirected(self):
        terminal = MagicMock()
        completed = subprocess.CompletedProcess(["gum", "choose"], 0, stdout="php\n", stderr=None)

        with patch.object(runtime_setup.sys.stdin, "isatty", return_value=False):
            with patch.object(runtime_setup, "terminal_streams", return_value=(terminal, terminal, None)):
                with patch.object(runtime_setup.shutil, "which", return_value="/opt/homebrew/bin/gum"):
                    with patch.object(runtime_setup.subprocess, "run", return_value=completed) as run_gum:
                        result = runtime_setup._run_gum(["choose", "php"])

        self.assertEqual(result.stdout, "php\n")
        self.assertIs(run_gum.call_args.kwargs["stdin"], terminal)
        self.assertIs(run_gum.call_args.kwargs["stderr"], terminal)
        self.assertEqual(run_gum.call_args.kwargs["stdout"], subprocess.PIPE)

    def test_service_multiselect_uses_gum_checkboxes_and_accepts_one_service(self):
        completed = subprocess.CompletedProcess(["gum", "choose"], 0, stdout="php\n", stderr=None)

        with patch.object(runtime_setup, "_run_gum", return_value=completed) as run_gum:
            selected = runtime_setup.gum_choose(
                ["php", "database", "worker"], title="Choose services", multi=True,
            )

        self.assertEqual(selected, ["php"])
        args = run_gum.call_args.args[0]
        self.assertIn("--no-limit", args)
        self.assertIn("--ordered", args)
        self.assertIn("☑ ", args)
        self.assertIn("☐ ", args)
        self.assertEqual(args[-3:], ["php", "database", "worker"])

    def test_multiselect_can_return_empty_for_caller_validation(self):
        completed = subprocess.CompletedProcess(["gum", "choose"], 0, stdout="\n", stderr=None)
        with patch.object(runtime_setup, "_run_gum", return_value=completed):
            self.assertEqual(runtime_setup.gum_choose(["php"], title="Choose", multi=True), [])

    def test_escape_is_reported_as_cancellation_and_other_gum_errors_are_distinct(self):
        cancelled = subprocess.CompletedProcess(["gum", "choose"], 130, stdout="", stderr=None)
        failed = subprocess.CompletedProcess(["gum", "choose"], 2, stdout="", stderr=None)

        with patch.object(runtime_setup, "_run_gum", return_value=cancelled):
            with self.assertRaisesRegex(runtime_setup.ApprovalCancelled, "cancelled in Gum"):
                runtime_setup.gum_choose(["php"], title="Choose")
        with patch.object(runtime_setup, "_run_gum", return_value=failed):
            with self.assertRaisesRegex(runtime_setup.GumError, "exit 2"):
                runtime_setup.gum_choose(["php"], title="Choose")

    def test_grouped_approval_uses_optional_gum_checkbox_acknowledgements(self):
        with patch.object(runtime_setup, "gum_choose", return_value=["Read Compose files"]) as choose:
            runtime_setup.required_approval(
                "Runtime setup", ["Read Compose files", "Recreate services"], summary="Project: dash",
            )

        self.assertEqual(choose.call_args.kwargs["multi"], True)
        self.assertIn("Project: dash", choose.call_args.kwargs["title"])
        self.assertIn("Enter continues", choose.call_args.kwargs["title"])
        self.assertIn("x toggles", choose.call_args.kwargs["title"])

    def test_declining_gum_confirmation_returns_false(self):
        declined = subprocess.CompletedProcess(["gum", "confirm"], 1, stdout="", stderr=None)
        with patch.object(runtime_setup, "_run_gum", return_value=declined):
            self.assertFalse(runtime_setup.gum_confirm("Read Docker metadata?"))

    def test_run_bounds_runtime_commands_and_reports_timeout(self):
        timeout = runtime_setup.HELPER_COMMAND_TIMEOUT_SECONDS

        with patch.object(
            runtime_setup.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["docker", "compose"], timeout),
        ) as run_process:
            with self.assertRaisesRegex(RuntimeError, f"timed out after {timeout} seconds"):
                runtime_setup.run(["docker", "compose", "up", "-d"])

        self.assertEqual(run_process.call_args.kwargs["timeout"], timeout)


if __name__ == "__main__":
    unittest.main()
