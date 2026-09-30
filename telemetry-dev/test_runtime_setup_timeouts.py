import base64
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_setup


class RuntimeHelperTimeoutTests(unittest.TestCase):
    def test_macos_checklist_uses_clickable_appkit_checkboxes_and_required_continue(self):
        completed = subprocess.CompletedProcess(["swift", "-"], 0, stdout="1,2\n", stderr="")

        with patch.object(runtime_setup, "dialog_backend", return_value=("mac", "osascript")):
            with patch.object(runtime_setup.shutil, "which", return_value="/usr/bin/swift"):
                with patch.object(runtime_setup.subprocess, "run", return_value=completed) as run_process:
                    result = runtime_setup._dialog(
                        "checklist", "Runtime approval", "Review each action.",
                        choices=["Read Compose files", "Remove local containers"],
                    )

        self.assertEqual(result, "1,2")
        self.assertEqual(run_process.call_args.args[0], ["/usr/bin/swift", "-"])
        script = run_process.call_args.kwargs["input"]
        payload = script.split('let encodedPayload = "', 1)[1].split('"', 1)[0]
        self.assertTrue(json.loads(base64.b64decode(payload))["require_all"])
        self.assertIn("import AppKit", script)
        self.assertIn("NSButton(checkboxWithTitle:", script)
        self.assertIn("var checklistTarget: ChecklistTarget? = nil", script)
        self.assertIn("checklistTarget = target", script)
        self.assertIn("box.target = target", script)
        self.assertIn("withExtendedLifetime(checklistTarget) { alert.runModal() }", script)
        self.assertIn("if requireAll {", script)
        self.assertIn("continueButton.isEnabled = !boxes.isEmpty && checked == boxes.count", script)

    def test_macos_service_multiselect_enables_continue_after_one_choice(self):
        completed = subprocess.CompletedProcess(["swift", "-"], 0, stdout="1\n", stderr="")

        with patch.object(runtime_setup, "dialog_backend", return_value=("mac", "osascript")):
            with patch.object(runtime_setup.shutil, "which", return_value="/usr/bin/swift"):
                with patch.object(runtime_setup.subprocess, "run", return_value=completed) as run_process:
                    result = runtime_setup._dialog(
                        "multiselect", "Choose Runtime Services", "Choose at least one service.",
                        choices=["php", "database", "worker"],
                    )

        self.assertEqual(result, "1")
        script = run_process.call_args.kwargs["input"]
        payload = script.split('let encodedPayload = "', 1)[1].split('"', 1)[0]
        self.assertFalse(json.loads(base64.b64decode(payload))["require_all"])
        self.assertIn("continueButton.isEnabled = checked > 0", script)
        self.assertIn('kind == "checklist" || kind == "multiselect" || kind == "radiolist"', script)
        self.assertIn("checked == boxes.count", script)

    def test_zenity_service_multiselect_uses_checkbox_list(self):
        completed = subprocess.CompletedProcess(["zenity"], 0, stdout="php\n", stderr="")

        with patch.object(runtime_setup, "dialog_backend", return_value=("zenity", "zenity")):
            with patch.object(runtime_setup.subprocess, "run", return_value=completed) as run_process:
                result = runtime_setup._dialog(
                    "multiselect", "Choose Runtime Services", "Choose at least one service.",
                    choices=["php", "database"],
                )

        self.assertEqual(result, "php")
        args = run_process.call_args.args[0]
        self.assertIn("--checklist", args)
        self.assertNotIn("--radiolist", args)

    def test_kdialog_service_multiselect_uses_checkbox_list(self):
        completed = subprocess.CompletedProcess(["kdialog"], 0, stdout="php\n", stderr="")

        with patch.object(runtime_setup, "dialog_backend", return_value=("kdialog", "kdialog")):
            with patch.object(runtime_setup.subprocess, "run", return_value=completed) as run_process:
                result = runtime_setup._dialog(
                    "multiselect", "Choose Runtime Services", "Choose at least one service.",
                    choices=["php", "database"],
                )

        self.assertEqual(result, "php")
        args = run_process.call_args.args[0]
        self.assertIn("--checklist", args)
        self.assertNotIn("--menu", args)

    def test_windows_service_multiselect_accepts_partial_checked_list(self):
        completed = subprocess.CompletedProcess(["powershell"], 0, stdout="php\n", stderr="")

        with patch.object(runtime_setup, "dialog_backend", return_value=("windows", "powershell")):
            with patch.object(runtime_setup.subprocess, "run", return_value=completed) as run_process:
                result = runtime_setup._dialog(
                    "multiselect", "Choose Runtime Services", "Choose at least one service.",
                    choices=["php", "database"],
                )

        self.assertEqual(result, "php")
        script = run_process.call_args.args[0][-1]
        self.assertIn("$b.Enabled=($count -gt 0)", script)
        self.assertIn("$d.kind -eq 'multiselect' -and $c.CheckedItems.Count -lt 1", script)

    def test_macos_confirmation_uses_appkit_and_returns_continue(self):
        completed = subprocess.CompletedProcess(["swift", "-"], 0, stdout="yes\n", stderr="")

        with patch.object(runtime_setup, "dialog_backend", return_value=("mac", "osascript")):
            with patch.object(runtime_setup.shutil, "which", return_value="/usr/bin/swift"):
                with patch.object(runtime_setup.subprocess, "run", return_value=completed) as run_process:
                    result = runtime_setup._dialog("confirm", "Runtime approval", "Read local Docker metadata?")

        self.assertEqual(result, "yes")
        self.assertEqual(run_process.call_args.args[0], ["/usr/bin/swift", "-"])
        self.assertIn('alert.addButton(withTitle: "Continue")', run_process.call_args.kwargs["input"])

    def test_macos_entry_uses_appkit_text_field(self):
        completed = subprocess.CompletedProcess(["swift", "-"], 0, stdout="dash\n", stderr="")

        with patch.object(runtime_setup, "dialog_backend", return_value=("mac", "osascript")):
            with patch.object(runtime_setup.shutil, "which", return_value="/usr/bin/swift"):
                with patch.object(runtime_setup.subprocess, "run", return_value=completed) as run_process:
                    result = runtime_setup._dialog(
                        "entry", "Runtime project", "Enter a project ID.", default="dash",
                    )

        self.assertEqual(result, "dash")
        self.assertEqual(run_process.call_args.args[0], ["/usr/bin/swift", "-"])
        self.assertIn("NSTextField(string:", run_process.call_args.kwargs["input"])

    def test_macos_checklist_reports_missing_swift_without_opening_jxa(self):
        with patch.object(runtime_setup, "dialog_backend", return_value=("mac", "osascript")):
            with patch.object(runtime_setup.shutil, "which", return_value=None):
                with patch.object(runtime_setup.subprocess, "run") as run_process:
                    with self.assertRaisesRegex(runtime_setup.NativeDialogError, "Swift"):
                        runtime_setup._dialog("checklist", "Runtime approval", "Review.", choices=["One"])

        run_process.assert_not_called()

    def test_required_approval_rejects_a_partial_native_checklist(self):
        with patch.object(runtime_setup, "_dialog", return_value="1"):
            with self.assertRaisesRegex(runtime_setup.ApprovalCancelled, "every checklist item"):
                runtime_setup.required_approval("Runtime approval", ["Read files", "Remove containers"])

    def test_required_approval_accepts_only_the_complete_terminal_checklist(self):
        with patch.object(runtime_setup.sys.stdin, "isatty", return_value=True):
            with patch.object(runtime_setup, "prompt", return_value="1,2"):
                with patch.object(runtime_setup, "_dialog") as native_dialog:
                    runtime_setup.required_approval("Runtime approval", ["Read files", "Remove containers"])

        native_dialog.assert_not_called()

    def test_terminal_approval_rejects_a_missing_item(self):
        with patch.object(runtime_setup.sys.stdin, "isatty", return_value=True):
            with patch.object(runtime_setup, "prompt", return_value="1"):
                with self.assertRaisesRegex(runtime_setup.ApprovalCancelled, "every checklist item"):
                    runtime_setup.required_approval("Runtime approval", ["Read files", "Remove containers"])

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

    def test_native_approval_timeout_is_reported_as_dialog_error(self):
        timeout = runtime_setup.NATIVE_DIALOG_TIMEOUT_SECONDS

        with patch.object(runtime_setup, "dialog_backend", return_value=("mac", "osascript")):
            with patch.object(
                runtime_setup.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired("osascript", timeout),
            ):
                with self.assertRaisesRegex(runtime_setup.NativeDialogError, f"timed out after {timeout} seconds"):
                    runtime_setup._dialog("confirm", "Runtime approval", "Continue?")


if __name__ == "__main__":
    unittest.main()
