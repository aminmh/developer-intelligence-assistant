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
import unittest.mock
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

    def test_github_helper_authorization_is_scoped_to_project_and_operation(self):
        signing_key = b"test-helper-signing-key"
        claims = {"aud": "runtime-helper", "origin": "http://localhost:3000",
                  "operation": "code-quality-github-fetch", "project_id": "pizza",
                  "target_id": "12345678-1234-5678-1234-567812345678",
                  "exp": runtime_setup.time.time() + 60}
        encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
        head, body = encode({"alg": "HS256", "typ": "JWT"}), encode(claims)
        signature = base64.urlsafe_b64encode(hmac.new(signing_key, f"{head}.{body}".encode(), hashlib.sha256).digest()).decode().rstrip("=")
        token = f"{head}.{body}.{signature}"
        with patch.object(runtime_setup, "helper_secret", return_value=signing_key):
            self.assertTrue(runtime_setup.verify_helper_token(token, claims["origin"],
                operation="code-quality-github-fetch", project_id="pizza", target_id=claims["target_id"]))
            self.assertFalse(runtime_setup.verify_helper_token(token, claims["origin"],
                operation="code-quality-github-fetch", project_id="other", target_id=claims["target_id"]))
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
        stack.enter_context(patch.object(runtime_setup, "verify_agent_report", return_value=None))

    LABEL_VALUES = {"php": {runtime_setup.LABELS["enabled"]: "true", runtime_setup.LABELS["project"]: "dash-fixture",
                            runtime_setup.LABELS["environment"]: "development",
                            runtime_setup.LABELS["commit"]: "a" * 40, runtime_setup.LABELS["service"]: "php"}}

    def initial_cleanup_state(self):
        # What setup records: the original, the label values, and the hash of the text it wrote.
        changed_compose = runtime_setup.add_service_labels(self.compose_original, "php", self.LABEL_VALUES["php"])
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
            "label_values": self.LABEL_VALUES,
            "compose_originals": {str(self.compose_file): self.compose_original},
            "compose_hashes": {str(self.compose_file): runtime_setup.digest(changed_compose)},
            "fingerprint": "fingerprint",
            "scope_original": True,
            "certificates_created": True,
        }

    def test_cleanup_keeps_lines_added_to_the_compose_file_after_setup(self):
        state = self.initial_cleanup_state()
        later = self.compose_file.read_text().replace(
            "    image: php:8\n", "    image: php:8\n    volumes:\n      - ../library:/library:ro\n", 1)
        later += "networks:\n  shared:\n    external: true\n"
        self.compose_file.write_text(later)
        with ExitStack() as stack:
            self.path_patches(stack)
            runtime_setup.write_state(state)
            stack.enter_context(patch.object(runtime_setup, "required_approval"))
            stack.enter_context(patch.object(runtime_setup, "run", return_value=runtime_setup.subprocess.CompletedProcess([], 0, stdout="", stderr="")))
            result = runtime_setup.cleanup()
        restored = self.compose_file.read_text()
        self.assertEqual(result["status"], "complete")
        self.assertNotIn("com.devassistant.telemetry", restored)
        self.assertIn("../library:/library:ro", restored)
        self.assertIn("external: true", restored)

    def test_revert_refuses_unsafe_cases_and_accepts_already_reverted_files(self):
        proposed = runtime_setup.add_service_labels(self.compose_original, "php", self.LABEL_VALUES["php"])
        revert = runtime_setup.revert_setup_edits
        self.assertEqual(revert(proposed, self.compose_original, proposed), self.compose_original)
        edited_by_hand = self.compose_original + "  cache:\n    image: redis:7\n"
        self.assertEqual(revert(edited_by_hand, self.compose_original, proposed), edited_by_hand)
        label_block = proposed[len("services:\n  php:\n    image: php:8\n"):proposed.index("  database:")]
        duplicated = proposed + "  other:\n    image: x\n" + label_block
        self.assertIsNone(revert(duplicated, self.compose_original, proposed))
        half = proposed.replace('"com.devassistant.telemetry.service": "php"', '"com.devassistant.telemetry.service": "web"')
        self.assertIsNone(revert(half, self.compose_original, proposed))

    def test_cleanup_stops_without_writing_when_labels_cannot_be_removed_safely(self):
        state = self.initial_cleanup_state()
        state["compose_hashes"] = {str(self.compose_file): "does-not-match"}
        before = self.compose_file.read_text() + "# edited later\n"
        self.compose_file.write_text(before)
        with ExitStack() as stack:
            self.path_patches(stack)
            runtime_setup.write_state(state)
            stack.enter_context(patch.object(runtime_setup, "required_approval"))
            stack.enter_context(patch.object(runtime_setup, "run", return_value=runtime_setup.subprocess.CompletedProcess([], 0, stdout="", stderr="")))
            result = runtime_setup.cleanup()
            saved = runtime_setup.read_state()
        self.assertEqual(result["status"], "error")
        self.assertIn("could not be removed safely", result["message"])
        self.assertEqual(self.compose_file.read_text(), before)
        self.assertEqual(saved["phase"], "cleanup-incomplete")

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


    def fixture_plan(self):
        with patch.object(runtime_setup, "CERT_DIR", self.cert_dir):
            return runtime_setup.build_setup_plan(self.project, ["php", "database"], ["php"],
                                                  "dash-fixture", "development", "a" * 40)

    def test_setup_plan_previews_only_added_label_lines_and_changes_nothing(self):
        plan = self.fixture_plan()
        self.assertEqual(plan["services"], ["php"])
        self.assertEqual([change["file"] for change in plan["changes"]], [str(self.compose_file)])
        added = plan["changes"][0]["added"]
        self.assertTrue(any("com.devassistant.telemetry.project" in line for line in added))
        self.assertFalse(any("postgres" in line or "php:8" in line for line in added), "no context lines are previewed")
        self.assertEqual(len(plan["approval_items"]), 6)
        self.assertEqual(self.compose_file.read_text(), self.compose_original)

    def test_setup_plan_rejects_invalid_choices(self):
        cases = [([], "dash-fixture", "development", "a" * 40, "at least one service"),
                 (["cache"], "dash-fixture", "development", "a" * 40, "not in the Compose project"),
                 (["php"], "bad id!", "development", "a" * 40, "Project ID"),
                 (["php"], "dash-fixture", "", "a" * 40, "Environment"),
                 (["php"], "dash-fixture", "development", "abc", "Commit")]
        for selected, project_id, environment, commit, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(runtime_setup.SetupInputError, message):
                runtime_setup.build_setup_plan(self.project, ["php", "database"], selected, project_id, environment, commit)

    OLD_REV = "rev_" + "c" * 32
    NEW_REV = "rev_" + "7" * 32

    def write_app_env(self):
        self.app_env = self.project_dir / ".env"
        self.app_env.write_text(
            "APP_NAME=Pizza\nINDEXER_PROJECT_ID=dash-fixture\n"
            f"INDEXER_REVISION_ID={self.OLD_REV}\n"
            f"OTEL_RESOURCE_ATTRIBUTES=service.name=app,indexer.project_id=dash-fixture,indexer.revision_id={self.OLD_REV},indexer.code_dirty=false\n"
            "INDEXER_OTEL_PROFILE_TOKEN=super-secret-token\n")

    def env_plan(self):
        self.write_app_env()
        with patch.object(runtime_setup, "CERT_DIR", self.cert_dir):
            return runtime_setup.build_setup_plan(self.project, ["php", "database"], ["php"],
                                                  "dash-fixture", "development", "a" * 40, self.NEW_REV)

    def test_plan_rewrites_only_the_revision_lines_of_the_app_env_and_never_previews_secrets(self):
        before = None
        self.write_app_env()
        before = self.app_env.read_text()
        plan = self.env_plan()
        self.assertEqual(self.app_env.read_text(), before, "planning changes nothing")
        env_change = next(change for change in plan["changes"] if change["file"] == str(self.app_env))
        self.assertEqual(len(env_change["added"]), 2)
        self.assertTrue(all(self.NEW_REV in line for line in env_change["added"]))
        self.assertNotIn("super-secret-token", json.dumps(plan["changes"]) + json.dumps(plan["approval_items"]))
        self.assertTrue(any("INDEXER_REVISION_ID" in item and self.NEW_REV in item for item in plan["approval_items"]))
        proposed = plan["app_env"]["proposed"]
        self.assertIn("APP_NAME=Pizza", proposed)
        self.assertIn("indexer.code_dirty=false", proposed)
        self.assertNotIn(self.OLD_REV, proposed)

    def test_plan_leaves_apps_without_revision_settings_alone_and_rejects_bad_ids(self):
        (self.project_dir / ".env").write_text("APP_NAME=Plain\n")
        with patch.object(runtime_setup, "CERT_DIR", self.cert_dir):
            plan = runtime_setup.build_setup_plan(self.project, ["php", "database"], ["php"],
                                                  "dash-fixture", "development", "a" * 40, self.NEW_REV)
        self.assertIsNone(plan["app_env"])
        self.assertEqual(len(plan["approval_items"]), 6)
        for bad in ("rev_xyz", "7" * 32, "rev_" + "7" * 31, 5):
            with self.subTest(bad=bad), self.assertRaisesRegex(runtime_setup.SetupInputError, "Revision ID"):
                runtime_setup.build_setup_plan(self.project, ["php", "database"], ["php"],
                                               "dash-fixture", "development", "a" * 40, bad)

    def test_apply_writes_the_app_env_and_records_only_the_previous_revision_for_cleanup(self):
        plan = self.env_plan()
        with ExitStack() as stack:
            self.path_patches(stack)
            stack.enter_context(patch.object(runtime_setup, "run"))
            stack.enter_context(patch.object(runtime_setup, "start_ingress_and_agent", return_value=(runtime_setup.CURRENT_SETUP, "fingerprint")))
            result = runtime_setup.apply_setup_plan(plan, approve=lambda *_a, **_k: None)
            saved_text = self.state_file.read_text()
        written = self.app_env.read_text()
        self.assertIn(f"INDEXER_REVISION_ID={self.NEW_REV}", written)
        self.assertIn(f"indexer.revision_id={self.NEW_REV}", written)
        self.assertIn("super-secret-token", written, "the rest of the file is kept")
        self.assertIn(self.NEW_REV, result["message"])
        self.assertNotIn("super-secret-token", saved_text, "the recovery record never holds the app's secrets")
        self.assertEqual(json.loads(saved_text)["app_env"]["previous"],
                         {"indexer_revision_id": self.OLD_REV, "resource_revision": self.OLD_REV})

    def test_apply_refuses_when_the_app_env_changed_since_the_preview(self):
        plan = self.env_plan()
        self.app_env.write_text(self.app_env.read_text() + "EXTRA=1\n")
        approve = unittest.mock.Mock()
        with ExitStack() as stack:
            self.path_patches(stack)
            with self.assertRaisesRegex(RuntimeError, r"app's \.env changed since the preview"):
                runtime_setup.apply_setup_plan(plan, approve=approve)
        approve.assert_not_called()
        self.assertNotIn(self.NEW_REV, self.app_env.read_text())

    def test_env_only_change_restarts_services_when_labels_already_exist(self):
        with patch.object(runtime_setup, "CERT_DIR", self.cert_dir):
            labeled = runtime_setup.build_setup_plan(self.project, ["php", "database"], ["php"], "dash-fixture", "development", "a" * 40)
        self.compose_file.write_text(next(iter(labeled["proposed"].values())))  # labels already in place
        plan = self.env_plan()
        self.assertEqual(plan["proposed"], {})
        calls = []
        with ExitStack() as stack:
            self.path_patches(stack)
            stack.enter_context(patch.object(runtime_setup, "run", side_effect=lambda args, **_k: calls.append(args)))
            stack.enter_context(patch.object(runtime_setup, "start_ingress_and_agent", return_value=(runtime_setup.CURRENT_SETUP, "fingerprint")))
            runtime_setup.apply_setup_plan(plan, approve=lambda *_a, **_k: None)
        self.assertTrue(any("restart" in call and "php" in call for call in calls))
        self.assertIn(f"INDEXER_REVISION_ID={self.NEW_REV}", self.app_env.read_text())

    def test_revert_app_env_restores_the_previous_revision_but_not_a_hand_edited_value(self):
        self.write_app_env()
        record = {"path": str(self.app_env), "revision_id": self.NEW_REV,
                  "previous": {"indexer_revision_id": self.OLD_REV, "resource_revision": self.OLD_REV}}
        applied = self.app_env.read_text().replace(self.OLD_REV, self.NEW_REV)
        self.app_env.write_text(applied)
        self.assertEqual(runtime_setup.revert_app_env(record), str(self.app_env))
        self.assertEqual(self.app_env.read_text().count(self.OLD_REV), 2)
        self.assertNotIn(self.NEW_REV, self.app_env.read_text())
        custom = "rev_" + "9" * 32
        self.app_env.write_text(applied.replace(self.NEW_REV, custom))
        self.assertIsNone(runtime_setup.revert_app_env(record))
        self.assertIn(custom, self.app_env.read_text())

    def test_apply_refuses_when_compose_file_changed_since_preview(self):
        plan = self.fixture_plan()
        self.compose_file.write_text(self.compose_original + "  cache:\n    image: redis:7\n")
        approve = unittest.mock.Mock()
        with ExitStack() as stack:
            self.path_patches(stack)
            with self.assertRaisesRegex(RuntimeError, "changed since the preview"):
                runtime_setup.apply_setup_plan(plan, approve=approve)
        approve.assert_not_called()
        self.assertFalse(self.state_file.exists())

    def test_declined_approval_leaves_files_and_recovery_state_untouched(self):
        plan = self.fixture_plan()

        def decline(title, items, summary=""):
            raise runtime_setup.ApprovalCancelled("Approve Runtime setup was not approved on this computer")

        with ExitStack() as stack:
            self.path_patches(stack)
            run_command = stack.enter_context(patch.object(runtime_setup, "run"))
            with self.assertRaises(runtime_setup.ApprovalCancelled):
                runtime_setup.apply_setup_plan(plan, approve=decline)
        run_command.assert_not_called()
        self.assertFalse(self.state_file.exists())
        self.assertEqual(self.compose_file.read_text(), self.compose_original)

    def test_apply_uses_the_given_approver_then_labels_and_recreates(self):
        plan = self.fixture_plan()
        approvals, run_calls = [], []
        with ExitStack() as stack:
            self.path_patches(stack)
            stack.enter_context(patch.object(runtime_setup, "run", side_effect=lambda args, **kwargs: run_calls.append(args)))
            stack.enter_context(patch.object(runtime_setup, "start_ingress_and_agent", return_value=(runtime_setup.CURRENT_SETUP, "fingerprint")))
            gum = stack.enter_context(patch.object(runtime_setup, "required_approval"))
            result = runtime_setup.apply_setup_plan(plan, approve=lambda title, items, summary="": approvals.append(title))
            saved = runtime_setup.read_state()
        gum.assert_not_called()
        self.assertEqual(approvals, ["Approve Runtime setup"])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(saved["phase"], "active")
        self.assertIn("com.devassistant.telemetry.enabled", self.compose_file.read_text())
        self.assertTrue(any("--no-deps" in command and "php" in command for command in run_calls))

    def test_agent_report_check_retries_then_names_the_reason(self):
        outputs = iter(["tls_verify:Missing_Authority_Key_Identifier\n"] * 2 + ["ok\n"])
        command = ({"cwd": self.api_dir, "env": {}}, {"args": ["docker", "compose"]})
        completed = lambda *args, **kwargs: runtime_setup.subprocess.CompletedProcess([], 0, stdout=next(outputs), stderr="")
        with patch.object(runtime_setup, "managed_agent_environment", return_value=command), \
                patch.object(runtime_setup, "run", side_effect=completed):
            self.assertIsNone(runtime_setup.verify_agent_report(attempts=3, delay=0))
        outputs = iter(["tls_verify:Missing_Authority_Key_Identifier\n"] * 2)
        with patch.object(runtime_setup, "managed_agent_environment", return_value=command), \
                patch.object(runtime_setup, "run", side_effect=completed):
            self.assertEqual(runtime_setup.verify_agent_report(attempts=2, delay=0),
                             "tls_verify:Missing_Authority_Key_Identifier")

    def test_helper_operations_are_dispatched_with_desktop_approval(self):
        with patch.object(runtime_setup, "cleanup", return_value={"status": "complete"}) as cleanup, \
                patch.object(runtime_setup, "remove_project_containers", return_value={"status": "complete"}) as remove, \
                patch.object(runtime_setup, "apply_setup_plan", return_value={"status": "complete"}) as apply:
            runtime_setup.run_helper_operation("cleanup")
            runtime_setup.run_helper_operation("remove local containers", project_id="pizza")
            runtime_setup.run_helper_operation("setup", plan={"fixture": True})
        cleanup.assert_called_once_with(approve=runtime_setup.helper_approval)
        remove.assert_called_once_with("pizza", approve=runtime_setup.helper_approval)
        apply.assert_called_once_with({"fixture": True}, approve=runtime_setup.helper_approval)
        with self.assertRaises(RuntimeError):
            runtime_setup.run_helper_operation("setup")

    def test_native_approval_requires_an_explicit_allow(self):
        completed = lambda code, out: runtime_setup.subprocess.CompletedProcess([], code, stdout=out, stderr="")
        with patch.object(runtime_setup.sys, "platform", "darwin"), \
                patch.object(runtime_setup.shutil, "which", return_value="/usr/bin/osascript"):
            with patch.object(runtime_setup.subprocess, "run", return_value=completed(0, "Allow\n")) as dialog:
                runtime_setup.native_approval("Approve Runtime setup", ["Recreate php"], "Compose project: x")
            command = dialog.call_args.args[0]
            self.assertEqual(command[0], "osascript")
            self.assertIn("Recreate php", command[-1])
            self.assertNotIn("Recreate php", command[2], "text is passed as an argument, never into the script")
            for result in (completed(1, ""), completed(0, "timeout\n")):
                with patch.object(runtime_setup.subprocess, "run", return_value=result), \
                        self.assertRaises(runtime_setup.ApprovalCancelled):
                    runtime_setup.native_approval("Approve Runtime setup", ["Recreate php"])
        with patch.object(runtime_setup.sys, "platform", "linux"), patch.dict(runtime_setup.os.environ, {}, clear=True), \
                self.assertRaises(runtime_setup.ApprovalUnavailable):
            runtime_setup.native_approval("Approve Runtime setup", ["Recreate php"])

    def test_helper_approval_falls_back_to_the_terminal_or_explains(self):
        with patch.object(runtime_setup, "native_approval", side_effect=runtime_setup.ApprovalUnavailable("No desktop.")), \
                patch.object(runtime_setup, "require_terminal"), \
                patch.object(runtime_setup, "required_approval") as gum:
            runtime_setup.helper_approval("Approve Runtime cleanup", ["Restore files"])
        gum.assert_called_once()
        with patch.object(runtime_setup, "native_approval", side_effect=runtime_setup.ApprovalUnavailable("No desktop.")), \
                patch.object(runtime_setup, "require_terminal", side_effect=runtime_setup.GumError("no tty")), \
                self.assertRaisesRegex(RuntimeError, "No terminal"):
            runtime_setup.helper_approval("Approve Runtime cleanup", ["Restore files"])
        self.assertNotIn("awaiting_approval", runtime_setup.HELPER_STATE)


if __name__ == "__main__":
    unittest.main()
