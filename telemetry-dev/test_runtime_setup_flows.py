from __future__ import annotations

import json
import base64
import hashlib
import hmac
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_setup


class RuntimeSetupFlowTests(unittest.TestCase):
    def test_code_quality_helper_authorization_is_scoped_to_operation_and_target(self):
        signing_key = b"test-helper-signing-key"
        claims = {"aud": "runtime-helper", "origin": "http://localhost:3000",
                  "operation": "code-quality-benchmark-use",
                  "target_id": "12345678-1234-5678-1234-567812345678",
                  "exp": runtime_setup.time.time() + 60}
        encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
        head = encode({"alg": "HS256", "typ": "JWT"})
        body = encode(claims)
        signature = base64.urlsafe_b64encode(hmac.new(signing_key, f"{head}.{body}".encode(), hashlib.sha256).digest()).decode().rstrip("=")
        token = f"{head}.{body}.{signature}"
        with patch.object(runtime_setup, "helper_secret", return_value=signing_key):
            self.assertTrue(runtime_setup.verify_helper_token(
                token, claims["origin"], operation="code-quality-benchmark-use", target_id=claims["target_id"]))
            self.assertFalse(runtime_setup.verify_helper_token(
                token, claims["origin"], operation="code-quality-bearer-manage", target_id=claims["target_id"]))
            self.assertFalse(runtime_setup.verify_helper_token(
                token, claims["origin"], operation="code-quality-benchmark-use", target_id="87654321-4321-8765-4321-876543218765"))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runtime-flow-")
        self.root = Path(self.temp.name)
        self.project_dir = self.root / "compose-project"
        self.project_dir.mkdir()
        self.compose_file = self.project_dir / "compose.yml"
        self.compose_original = "services:\n  php:\n    image: php:8\n  database:\n    image: postgres:16\n"
        self.compose_file.write_text(self.compose_original)
        self.project = {
            "name": "fixture-project",
            "directory": str(self.project_dir),
            "files": [str(self.compose_file)],
        }
        self.state_file = self.root / "state" / "local-setup.json"
        self.api_dir = self.root / "api"
        self.api_dir.mkdir()
        self.cert_dir = self.root / "certificates"
        self.env_file = self.root / "api.env"
        self.old_current_setup = dict(runtime_setup.CURRENT_SETUP)
        runtime_setup.CURRENT_SETUP.clear()

    def tearDown(self):
        runtime_setup.CURRENT_SETUP.clear()
        runtime_setup.CURRENT_SETUP.update(self.old_current_setup)
        self.temp.cleanup()

    def path_patches(self, stack: ExitStack):
        stack.enter_context(patch.object(runtime_setup, "state_path", return_value=self.state_file))
        stack.enter_context(patch.object(runtime_setup, "API", self.api_dir))
        stack.enter_context(patch.object(runtime_setup, "CERT_DIR", self.cert_dir))
        stack.enter_context(patch.object(runtime_setup, "ENV_FILE", self.env_file))
        stack.enter_context(patch.object(runtime_setup, "require_terminal"))
        stack.enter_context(patch.object(runtime_setup, "compose_args", return_value=["docker", "compose", "-f", str(self.compose_file)]))

    def initial_cleanup_state(self):
        changed_compose = "services:\n  php:\n    image: php:8\n    labels:\n      com.devassistant.telemetry.enabled: 'true'\n  database:\n    image: postgres:16\n"
        self.compose_file.write_text(changed_compose)
        self.cert_dir.mkdir()
        (self.cert_dir / "generated.pem").write_text("fixture")
        self.env_file.write_text(
            'TELEMETRY_HOST_SCOPES={"fingerprint":{"projects":{"dash-fixture":["development"]}}}\n'
        )
        return {
            "version": 1,
            "phase": "active",
            "project_id": "dash-fixture",
            "environment": "development",
            "project": self.project,
            "services": ["php"],
            "compose_originals": {str(self.compose_file): self.compose_original},
            "fingerprint": "fingerprint",
            "scope_original": True,
            "certificates_created": True,
        }

    def test_setup_uses_one_grouped_approval_and_records_recovery_before_mutations(self):
        run_calls = []
        approvals = []
        compose_mutations = []

        def run(args, **kwargs):
            run_calls.append(args)
            if args[0] == "docker" and "config" in args and "--services" in args:
                stdout = "php\ndatabase\n"
            elif args[0] == "git" and "rev-parse" in args:
                stdout = "a" * 40 + "\n"
            else:
                stdout = ""
            return runtime_setup.subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

        real_write_text = runtime_setup.write_text_atomic

        def write_text(path, value, mode=None):
            saved = runtime_setup.read_state()
            self.assertIsNotNone(saved)
            self.assertIn(str(self.compose_file), saved["compose_originals"])
            compose_mutations.append(path)
            return real_write_text(path, value, mode)

        def approve(title, items, summary=""):
            self.assertFalse(self.state_file.exists(), "recovery state must be written after approval")
            approvals.append((title, items, summary))

        with ExitStack() as stack:
            self.path_patches(stack)
            stack.enter_context(patch.object(runtime_setup, "discover_projects", return_value=[self.project]))
            stack.enter_context(patch.object(runtime_setup, "confirm", return_value=True))
            stack.enter_context(patch.object(runtime_setup.sys.stdin, "isatty", return_value=False))
            stack.enter_context(patch.object(runtime_setup, "gum_choose", return_value=["php", "database"]))
            stack.enter_context(patch.object(runtime_setup, "prompt", side_effect=["dash-fixture", "development"]))
            stack.enter_context(patch.object(runtime_setup, "run", side_effect=run))
            stack.enter_context(patch.object(runtime_setup, "required_approval", side_effect=approve))
            stack.enter_context(patch.object(runtime_setup, "start_ingress_and_agent", return_value=(runtime_setup.CURRENT_SETUP, "fingerprint")))
            stack.enter_context(patch.object(runtime_setup, "write_text_atomic", side_effect=write_text))

            runtime_setup.setup("fixture-project")

            saved = json.loads(self.state_file.read_text())

        self.assertEqual(len(approvals), 1)
        self.assertEqual(len(approvals[0][1]), 6)
        self.assertEqual(compose_mutations, [self.compose_file])
        self.assertEqual(saved["phase"], "active")
        self.assertEqual(saved["compose_originals"][str(self.compose_file)], self.compose_original)
        self.assertTrue(any("--no-deps" in command for command in run_calls))

    def test_setup_reports_active_record_as_success_without_overwriting_it(self):
        state = self.initial_cleanup_state()

        with ExitStack() as stack:
            self.path_patches(stack)
            runtime_setup.write_state(state)
            saved_before = self.state_file.read_text()
            stack.enter_context(patch.object(runtime_setup, "yaml_module"))
            discover = stack.enter_context(patch.object(runtime_setup, "discover_projects"))
            run_command = stack.enter_context(patch.object(runtime_setup, "run"))

            result = runtime_setup.setup("fixture-project")

        self.assertEqual(result["status"], "complete")
        self.assertIn("already active", result["message"])
        self.assertEqual(self.state_file.read_text(), saved_before)
        discover.assert_not_called()
        run_command.assert_not_called()

    def test_setup_accepts_one_service_from_gum_multiselect(self):
        run_calls = []
        selection_calls = []
        approvals = []

        def run(args, **kwargs):
            run_calls.append(args)
            if args[0] == "docker" and "config" in args and "--services" in args:
                stdout = "php\ndatabase\n"
            elif args[0] == "git" and "rev-parse" in args:
                stdout = "a" * 40 + "\n"
            else:
                stdout = ""
            return runtime_setup.subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

        def choose_services(choices, *, title, multi=False):
            selection_calls.append((choices, title, multi))
            return [] if len(selection_calls) == 1 else ["php"]

        with ExitStack() as stack:
            self.path_patches(stack)
            stack.enter_context(patch.object(runtime_setup, "discover_projects", return_value=[self.project]))
            stack.enter_context(patch.object(runtime_setup, "confirm", return_value=True))
            stack.enter_context(patch.object(runtime_setup.sys.stdin, "isatty", return_value=True))
            stack.enter_context(patch.object(runtime_setup, "gum_choose", side_effect=choose_services))
            stack.enter_context(patch.object(runtime_setup, "prompt", side_effect=["dash-fixture", "development"]))
            stack.enter_context(patch.object(runtime_setup, "run", side_effect=run))
            stack.enter_context(patch.object(runtime_setup, "required_approval", side_effect=lambda title, items, summary="": approvals.append(items)))
            stack.enter_context(patch.object(runtime_setup, "start_ingress_and_agent", return_value=(runtime_setup.CURRENT_SETUP, "fingerprint")))

            runtime_setup.setup("fixture-project")
            saved = runtime_setup.read_state()

        self.assertEqual(selection_calls[0][0], ["php", "database"])
        self.assertEqual(len(selection_calls), 2, "an empty selection must return to Gum selection")
        self.assertTrue(all(call[2] for call in selection_calls))
        self.assertEqual(saved["services"], ["php"])
        self.assertEqual(len(approvals), 1)
        self.assertTrue(any("--no-deps" in command for command in run_calls))

    def test_cleanup_uses_one_grouped_approval_and_removes_recovery_after_success(self):
        approvals = []
        state = self.initial_cleanup_state()

        with ExitStack() as stack:
            self.path_patches(stack)
            runtime_setup.write_state(state)
            stack.enter_context(patch.object(runtime_setup, "required_approval", side_effect=lambda title, items, summary="": approvals.append((title, items, summary))))
            stack.enter_context(patch.object(runtime_setup, "run", return_value=runtime_setup.subprocess.CompletedProcess([], 0, stdout="", stderr="")))

            result = runtime_setup.cleanup()

        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(approvals), 1)
        self.assertTrue(any("Permanently remove generated development certificates" in item for item in approvals[0][1]))
        self.assertFalse(self.state_file.exists())
        self.assertFalse(self.cert_dir.exists())
        self.assertEqual(self.compose_file.read_text(), self.compose_original)
        self.assertIn('TELEMETRY_HOST_SCOPES={}', self.env_file.read_text())

    def test_cleanup_failure_keeps_cleanup_incomplete_recovery_state(self):
        state = self.initial_cleanup_state()

        def fail_compose(args, **kwargs):
            raise RuntimeError("fixture Compose failure")

        with ExitStack() as stack:
            self.path_patches(stack)
            runtime_setup.write_state(state)
            stack.enter_context(patch.object(runtime_setup, "required_approval"))
            stack.enter_context(patch.object(runtime_setup, "run", side_effect=fail_compose))

            result = runtime_setup.cleanup()
            saved = json.loads(self.state_file.read_text())

        self.assertEqual(result["status"], "error")
        self.assertIn("recovery state was retained", result["message"])
        self.assertEqual(saved["phase"], "cleanup-incomplete")
        self.assertIn("fixture Compose failure", saved["cleanup_error"])


if __name__ == "__main__":
    unittest.main()
