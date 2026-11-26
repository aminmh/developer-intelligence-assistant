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
        self.legacy_file = self.root / "state" / "local-setup.json"
        self.state_file = self.root / "state" / "setups" / "dash-fixture@development.json"
        self.host_file = self.root / "state" / "host.json"
        self.api_dir = self.root / "api"
        self.api_dir.mkdir()
        self.cert_dir = self.root / "certificates"
        self.env_file = self.root / "api.env"
        self.old_current_setup = dict(runtime_setup.CURRENT_SETUP)
        runtime_setup.CURRENT_SETUP.clear()
        # Never read the developer's real recovery records, even in tests that do not call path_patches.
        isolate = patch.object(runtime_setup, "state_path", return_value=self.legacy_file)
        isolate.start()
        self.addCleanup(isolate.stop)
        # Plans in these tests must not run the developer's Docker; the image check has its own test.
        self.real_image_check = runtime_setup.unresolved_image_problem
        no_docker = patch.object(runtime_setup, "unresolved_image_problem", return_value=None)
        no_docker.start()
        self.addCleanup(no_docker.stop)

    def tearDown(self):
        runtime_setup.CURRENT_SETUP.clear()
        runtime_setup.CURRENT_SETUP.update(self.old_current_setup)
        self.temp.cleanup()

    def path_patches(self, stack: ExitStack):
        stack.enter_context(patch.object(runtime_setup, "state_path", return_value=self.legacy_file))
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
            runtime_setup.write_record(state)
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
            runtime_setup.write_record(state)
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

    def test_a_setup_for_the_same_project_and_environment_is_refused_without_changing_its_record(self):
        state = self.initial_cleanup_state()

        with ExitStack() as stack:
            self.path_patches(stack)
            runtime_setup.write_record(state)
            saved_before = self.state_file.read_text()

            blocker, active = runtime_setup.setup_blocker(self.project, ["php"], "dash-fixture", "development")

        self.assertIn("already active", blocker)
        self.assertTrue(active)
        self.assertEqual(self.state_file.read_text(), saved_before)

    # ── several setups at once ───────────────────────────────────────────────

    def second_state(self, project_name="other-compose", files=None, project_id="orders", environment="staging"):
        return {"version": 1, "phase": "active", "project_id": project_id, "environment": environment,
                "project": {"name": project_name, "directory": str(self.root), "files": files or [str(self.root / "other.yml")]},
                "services": ["web"], "compose_originals": {}, "compose_hashes": {}}

    def test_an_old_single_record_is_moved_unchanged_and_listed_with_a_new_one(self):
        old = self.initial_cleanup_state()
        self.legacy_file.parent.mkdir(parents=True)
        self.legacy_file.write_text(json.dumps(old))

        with ExitStack() as stack:
            self.path_patches(stack)
            runtime_setup.write_record(self.second_state())
            states = runtime_setup.list_states()
            migrated = json.loads(self.state_file.read_text())
            host = json.loads(self.host_file.read_text())

        self.assertEqual(sorted(item["project_id"] for item in states), ["dash-fixture", "orders"])
        self.assertEqual(migrated, old)
        self.assertFalse(self.legacy_file.exists())
        self.assertEqual(host, {"certificates_created": True, "fingerprint": "fingerprint"})

    def test_a_second_project_is_accepted_but_the_same_compose_project_or_files_are_not(self):
        with ExitStack() as stack:
            self.path_patches(stack)
            runtime_setup.write_record(self.initial_cleanup_state())
            other = {"name": "other-compose", "directory": str(self.root), "files": [str(self.root / "other.yml")]}

            accepted = runtime_setup.setup_blocker(other, ["web"], "orders", "staging")
            same_project = runtime_setup.setup_blocker(self.project, ["database"], "billing", "development")
            same_file = runtime_setup.setup_blocker({**other, "files": [str(self.compose_file)]}, ["web"], "orders", "staging")
            same_identity = runtime_setup.setup_blocker(other, ["web"], "dash-fixture", "development")

        self.assertEqual(accepted, (None, False))
        self.assertIn("same Compose files", same_project[0])
        self.assertIn("same Compose files", same_file[0])
        self.assertIn("already active", same_identity[0])

    def test_an_unfinished_record_blocks_every_new_setup_and_is_never_overwritten(self):
        broken = {**self.initial_cleanup_state(), "phase": "cleanup-incomplete"}
        with ExitStack() as stack:
            self.path_patches(stack)
            runtime_setup.write_record(broken)
            before = self.state_file.read_text()

            blocker, _ = runtime_setup.setup_blocker()

        self.assertIn("cleanup-incomplete", blocker)
        self.assertEqual(self.state_file.read_text(), before)

    def test_cleaning_one_setup_keeps_the_other_and_the_shared_host_until_the_last_one(self):
        approvals, commands = [], []
        first = self.initial_cleanup_state()
        second = self.second_state()
        second.update({"fingerprint": "fingerprint", "scope_original": True})
        self.env_file.write_text('TELEMETRY_HOST_SCOPES={"fingerprint":{"projects":{"dash-fixture":["development"],"orders":["staging"]}}}\n')

        def run(args, **kwargs):
            commands.append(args)
            return runtime_setup.subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with ExitStack() as stack:
            self.path_patches(stack)
            runtime_setup.write_record(first)
            runtime_setup.write_record(second)
            runtime_setup.write_host_state({"certificates_created": True, "fingerprint": "fingerprint"})
            stack.enter_context(patch.object(runtime_setup, "run", side_effect=run))
            approve = lambda title, items, summary="": approvals.append(items)

            with self.assertRaises(RuntimeError) as several:
                runtime_setup.cleanup(approve=approve)
            runtime_setup.cleanup(approve=approve, project_id="orders", environment="staging")
            after_first = runtime_setup.list_states()
            agent_stopped_first = any("down" in command for command in commands)
            certificates_kept = self.cert_dir.exists()
            scopes_after_first = json.loads(self.env_file.read_text().split("=", 1)[1])

            commands.clear()
            runtime_setup.cleanup(approve=approve)
            after_last = runtime_setup.list_states()
            agent_stopped_last = any("down" in command for command in commands)

        self.assertIn("choose which one", str(several.exception))
        self.assertEqual([item["project_id"] for item in after_first], ["dash-fixture"])
        self.assertFalse(agent_stopped_first)
        self.assertTrue(certificates_kept)
        self.assertEqual(scopes_after_first, {"fingerprint": {"projects": {"dash-fixture": ["development"]}}})
        self.assertTrue(any("Keep the telemetry agent" in item for item in approvals[0]))
        self.assertEqual(after_last, [])
        self.assertTrue(agent_stopped_last)
        self.assertFalse(self.cert_dir.exists())
        self.assertFalse(self.host_file.exists())
        self.assertEqual(self.compose_file.read_text(), self.compose_original)

    def test_cleanup_of_an_unknown_setup_names_it_and_changes_nothing(self):
        with ExitStack() as stack:
            self.path_patches(stack)
            runtime_setup.write_record(self.initial_cleanup_state())
            with self.assertRaises(RuntimeError) as raised:
                runtime_setup.cleanup(approve=lambda *a, **k: None, project_id="nope", environment="x")
            remaining = len(runtime_setup.list_states())

        self.assertIn("nope · x", str(raised.exception))
        self.assertEqual(remaining, 1)

    def test_the_agent_starts_when_any_setup_is_active(self):
        stalled = {**self.second_state(), "phase": "starting"}
        with ExitStack() as stack:
            self.path_patches(stack)
            stack.enter_context(patch.object(runtime_setup, "docker_socket_group_id", return_value="0"))
            socket = self.root / "docker.sock"
            socket.write_text("")
            stack.enter_context(patch.dict(runtime_setup.os.environ, {"DOCKER_SOCK_PATH": str(socket)}))
            active = self.initial_cleanup_state()
            runtime_setup.write_record(stalled)
            for name in ("ca.pem", "host.crt", "host.key"):
                (self.cert_dir / "agent").mkdir(parents=True, exist_ok=True)
                (self.cert_dir / "agent" / name).write_text("x")
            only_stalled = runtime_setup.managed_agent_environment()
            runtime_setup.write_record(active)
            with_active = runtime_setup.managed_agent_environment()

        self.assertEqual(only_stalled, (None, None))
        self.assertIsNotNone(with_active[0])

    def test_preview_stops_when_compose_cannot_resolve_an_image_variable(self):
        config = json.dumps({"services": {"database": {"image": "mariadb:"}, "php": {"image": "php:8"}}})
        stderr = 'time=1 level=warning msg="The \\"MARIA_VERSION\\" variable is not set. Defaulting to a blank string."'
        done = runtime_setup.subprocess.CompletedProcess([], 0, stdout=config, stderr=stderr)
        with patch.object(runtime_setup, "run", return_value=done), \
                patch.object(runtime_setup, "compose_args", return_value=["docker", "compose"]):
            problem = self.real_image_check(self.project, ["database", "php"])
            fine = self.real_image_check(self.project, ["php"])

        self.assertIn("database", problem)
        self.assertIn("MARIA_VERSION", problem)
        self.assertIn("No changes were made", problem)
        self.assertIsNone(fine)

    def test_instrumentation_check_reads_composer_files_and_reports_missing_declared_installed_and_unknown(self):
        check = lambda: runtime_setup.instrumentation_status(self.project)
        self.assertEqual(check()["status"], "unknown")  # no composer.json yet

        composer = self.project_dir / "composer.json"
        lock = self.project_dir / "composer.lock"
        composer.write_text(json.dumps({"require": {"php": ">=8.2", "symfony/framework-bundle": "^6.3"}}))
        missing = check()
        self.assertEqual(missing["status"], "missing")
        self.assertIn("no request spans", missing["message"])
        self.assertEqual((missing["adapter"], missing["install_command"]),
                         ("symfony", "composer require aminmh/developer-assistant-otel-symfony"))
        composer.write_text(json.dumps({"require": {"laravel/framework": "^11"}}))
        self.assertEqual(check()["install_command"], "composer require aminmh/developer-assistant-otel-laravel")
        composer.write_text(json.dumps({"require": {"php": ">=8.2"}}))
        self.assertEqual(check()["install_command"], "composer require aminmh/developer-assistant-otel-core")

        composer.write_text(json.dumps({"require": {"aminmh/developer-assistant-otel-symfony": "^0.1"}}))
        lock.write_text(json.dumps({"packages": [{"name": "symfony/console"}]}))
        self.assertEqual(check()["status"], "declared")
        self.assertEqual(check()["install_command"], "composer install")

        lock.write_text(json.dumps({"packages": [{"name": "aminmh/developer-assistant-otel-core"}, {"name": "aminmh/developer-assistant-otel-symfony"}]}))
        installed = check()
        self.assertEqual((installed["status"], installed["packages"]), ("installed", ["aminmh/developer-assistant-otel-core", "aminmh/developer-assistant-otel-symfony"]))

        composer.write_text("{not json")
        self.assertEqual(check()["status"], "unknown")

    def test_instrumentation_check_warns_when_the_locked_framework_is_older_than_the_adapter_supports(self):
        (self.project_dir / "composer.json").write_text(json.dumps({"require": {"symfony/framework-bundle": "6.3.*"}}))
        lock = self.project_dir / "composer.lock"
        lock.write_text(json.dumps({"packages": [{"name": "symfony/http-kernel", "version": "v6.2.10"}]}))
        warning = runtime_setup.instrumentation_status(self.project)["compatibility"]
        self.assertIn("symfony/http-kernel v6.2.10", warning)
        self.assertIn("Symfony 6.3", warning)

        lock.write_text(json.dumps({"packages": [{"name": "symfony/http-kernel", "version": "v6.3.12"}]}))  # supported since 0.1.1
        self.assertIsNone(runtime_setup.instrumentation_status(self.project)["compatibility"])

        lock.write_text(json.dumps({"packages": [{"name": "symfony/http-kernel", "version": "v7.1.0"}]}))
        self.assertIsNone(runtime_setup.instrumentation_status(self.project)["compatibility"])
        (self.project_dir / "composer.json").write_text(json.dumps({"require": {"aminmh/developer-assistant-otel-core": "^0.1"}}))
        lock.write_text(json.dumps({"packages": [{"name": "aminmh/developer-assistant-otel-core"}]}))
        self.assertEqual(runtime_setup.instrumentation_status(self.project)["status"], "installed")

    # ── installing the Indexr library from the wizard ────────────────────────

    def install_patches(self, stack: ExitStack, output="", code=0, composer=None):
        stack.enter_context(patch.object(runtime_setup, "find_compose_project", return_value=self.project))
        stack.enter_context(patch.object(runtime_setup, "compose_project_details", return_value={"services": ["php", "database"], "commit": None}))
        self.ensure_agent = stack.enter_context(patch.object(runtime_setup, "ensure_host_agent", return_value=False))
        stack.enter_context(patch.object(runtime_setup, "host_agent_exists", return_value=True))
        if composer is not None:
            (self.project_dir / "composer.json").write_text(json.dumps(composer))
        done = runtime_setup.subprocess.CompletedProcess([], code, stdout=output, stderr="")
        return stack.enter_context(patch.object(runtime_setup, "run", return_value=done))

    SYMFONY_APP = {"require": {"symfony/framework-bundle": "6.3.*"}}

    def test_install_asks_approval_then_runs_the_setup_script_with_the_published_version(self):
        approvals = []
        with ExitStack() as stack:
            run = self.install_patches(stack, output="done", composer=self.SYMFONY_APP)
            result = runtime_setup.install_indexr_library(
                "fixture-project", "skiplanner", "^0.1.1", approve=lambda title, items, summary="": approvals.append((title, items)))

        self.assertEqual(result["status"], "complete")
        command = run.call_args.args[0]
        self.assertEqual(command[0], "bash")
        self.assertTrue(command[1].endswith("scripts/setup-php-telemetry.sh"))
        self.assertEqual(command[2:], [str(self.project_dir), "--project-id", "skiplanner", "--version", "^0.1.1", "--yes", "--non-interactive"])
        self.assertTrue(run.call_args.kwargs["env"]["COMPOSER_BIN"].endswith("scripts/composer-host.sh"))
        self.assertEqual(run.call_args.kwargs["env"]["COMPOSER_NO_INTERACTION"], "1")
        self.assertEqual(len(approvals), 1)
        self.assertTrue(any("aminmh/developer-assistant-otel-symfony ^0.1.1" in item for item in approvals[0][1]))
        self.assertTrue(any("Commit composer.json and composer.lock" in item for item in approvals[0][1]))

    def test_install_does_nothing_when_the_library_is_already_there_or_the_approval_is_declined(self):
        with ExitStack() as stack:
            run = self.install_patches(stack, composer={"require": {"aminmh/developer-assistant-otel-core": "^0.1"}})
            (self.project_dir / "composer.lock").write_text(json.dumps({"packages": [{"name": "aminmh/developer-assistant-otel-core"}]}))
            (self.project_dir / ".env").write_text("INDEXER_PROJECT_ID=p\n")
            already = runtime_setup.install_indexr_library("fixture-project", "p", "^0.1.1", approve=lambda *a, **k: self.fail("no approval needed"))
            run.assert_not_called()

            (self.project_dir / "composer.json").write_text(json.dumps(self.SYMFONY_APP))
            (self.project_dir / "composer.lock").unlink()

            def decline(*args, **kwargs):
                raise runtime_setup.ApprovalCancelled("not approved")

            with self.assertRaises(runtime_setup.ApprovalCancelled):
                runtime_setup.install_indexr_library("fixture-project", "p", "^0.1.1", approve=decline)
            run.assert_not_called()

        self.assertTrue(already["already_installed"])

    def test_install_explains_a_composer_401_from_a_private_registry(self):
        output = ("https://gitlab.birad.ir/api/v4/group/33/-/packages/composer/packages.json could not be fully loaded "
                  "(The 'https://gitlab.birad.ir/api/v4/group/33/-/packages/composer/packages.json' URL required authentication (HTTP 401).")
        with ExitStack() as stack:
            self.install_patches(stack, output=output, code=2, composer=self.SYMFONY_APP)
            result = runtime_setup.install_indexr_library("fixture-project", "p", "^0.1.1", approve=lambda *a, **k: None)

        self.assertEqual((result["status"], result["code"], result["registry"]), ("error", "composer_auth", "gitlab.birad.ir"))
        self.assertIn("composer config --global gitlab-token.gitlab.birad.ir", result["message"])
        self.assertIn("local copy of the library would not avoid it", result["message"])
        self.assertIn("Nothing was committed", result["message"])

    def test_install_reports_other_failures_with_the_end_of_the_output(self):
        with ExitStack() as stack:
            self.install_patches(stack, output="line\n" * 30 + "Error: The checkout has uncommitted changes.", code=1, composer=self.SYMFONY_APP)
            result = runtime_setup.install_indexr_library("fixture-project", "p", "^0.1.1", approve=lambda *a, **k: None)

        self.assertEqual((result["status"], result["code"]), ("error", "install_failed"))
        self.assertIn("uncommitted changes", result["message"])
        self.assertLess(result["message"].count("line"), 15)

    def test_install_stops_before_approval_when_a_compose_image_cannot_be_resolved(self):
        approvals = []
        with ExitStack() as stack:
            run = self.install_patches(stack, composer=self.SYMFONY_APP)
            stack.enter_context(patch.object(runtime_setup, "unresolved_image_problem", return_value="Compose cannot resolve the image of database. No changes were made."))
            with self.assertRaises(RuntimeError) as raised:
                runtime_setup.install_indexr_library("fixture-project", "p", "^0.1.1", approve=lambda *a, **k: approvals.append(a))
            run.assert_not_called()

        self.assertIn("cannot resolve the image", str(raised.exception))
        self.assertEqual(approvals, [])

    def test_a_silent_script_failure_says_how_to_see_the_reason(self):
        with ExitStack() as stack:
            self.install_patches(stack, output="== Runtime telemetry setup ==", code=1, composer=self.SYMFONY_APP)
            result = runtime_setup.install_indexr_library("fixture-project", "p", "^0.1.1", approve=lambda *a, **k: None)

        self.assertEqual(result["code"], "install_failed")
        self.assertIn("printed no message", result["message"])
        self.assertIn("setup-php-telemetry.sh", result["message"])
        self.assertNotIn("--non-interactive", result["message"])

    def test_install_creates_the_host_agent_after_approval_when_there_is_none(self):
        approvals = []
        with ExitStack() as stack:
            self.install_patches(stack, composer=self.SYMFONY_APP)
            stack.enter_context(patch.object(runtime_setup, "host_agent_exists", return_value=False))
            runtime_setup.install_indexr_library("fixture-project", "p", "^0.1.1", approve=lambda title, items, summary="": approvals.append(items))

        self.assertTrue(any("Create local development certificates" in item for item in approvals[0]))
        self.ensure_agent.assert_called_once()

    def test_ensure_host_agent_creates_certificates_once_records_them_and_starts_the_agent(self):
        started, ran = [], []

        def bootstrap(args, **kwargs):
            ran.append(args)
            for name in ("ca.pem", "server.crt", "server.key"):
                (self.cert_dir / "ingress").mkdir(parents=True, exist_ok=True)
                (self.cert_dir / "ingress" / name).write_text("x")
            for name in ("ca.pem", "host.crt", "host.key"):
                (self.cert_dir / "agent").mkdir(parents=True, exist_ok=True)
                (self.cert_dir / "agent" / name).write_text("x")
            return runtime_setup.subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with ExitStack() as stack:
            self.path_patches(stack)
            stack.enter_context(patch.object(runtime_setup, "run", side_effect=bootstrap))
            stack.enter_context(patch.object(runtime_setup, "cert_fingerprint", return_value="f" * 64))
            stack.enter_context(patch.object(runtime_setup, "start_host_containers", side_effect=lambda: started.append(True)))
            first = runtime_setup.ensure_host_agent()
            second = runtime_setup.ensure_host_agent()
            host = json.loads(self.host_file.read_text())

        self.assertEqual((first, second), (True, False))
        self.assertEqual(len(ran), 1)
        self.assertEqual(started, [True])
        self.assertEqual(host, {"certificates_created": True, "fingerprint": "f" * 64})

    def test_the_agent_is_managed_with_only_a_host_record(self):
        with ExitStack() as stack:
            self.path_patches(stack)
            self.assertEqual(runtime_setup.managed_agent_environment(), (None, None))
            runtime_setup.write_host_state({"certificates_created": True, "fingerprint": "f" * 64})
            socket = self.root / "docker.sock"
            socket.write_text("")
            stack.enter_context(patch.dict(runtime_setup.os.environ, {"DOCKER_SOCK_PATH": str(socket)}))
            stack.enter_context(patch.object(runtime_setup, "docker_socket_group_id", return_value="0"))
            for name in ("ca.pem", "host.crt", "host.key"):
                (self.cert_dir / "agent").mkdir(parents=True, exist_ok=True)
                (self.cert_dir / "agent" / name).write_text("x")
            managed = runtime_setup.managed_agent_environment()

        self.assertIsNotNone(managed[0])

    def test_an_installed_but_unconfigured_library_resumes_the_setup(self):
        with ExitStack() as stack:
            run = self.install_patches(stack, output="done", composer={"require": {"aminmh/developer-assistant-otel-symfony": "^0.1.1"}})
            (self.project_dir / "composer.lock").write_text(json.dumps({"packages": [{"name": "aminmh/developer-assistant-otel-symfony"}]}))
            status = runtime_setup.instrumentation_status(self.project)
            result = runtime_setup.install_indexr_library("fixture-project", "p", "^0.1.1", approve=lambda *a, **k: None)
            run.assert_called_once()

            (self.project_dir / ".env").write_text("APP_ENV=dev\nINDEXER_REVISION_ID=rev_1\n")
            configured = runtime_setup.instrumentation_status(self.project)

        self.assertEqual((status["status"], status["configured"]), ("installed", False))
        self.assertIn("setup was not finished", status["message"])
        self.assertEqual(result["status"], "complete")
        self.assertTrue(configured["configured"])

    def test_the_symfony_bundle_is_registered_once_and_nothing_else_in_the_file_changes(self):
        script = Path(__file__).resolve().parents[2] / "scripts" / "register-symfony-bundle.py"
        bundles = self.project_dir / "config" / "bundles.php"
        bundles.parent.mkdir()
        original = "<?php\n\nreturn [\n    Symfony\\Bundle\\FrameworkBundle\\FrameworkBundle::class => ['all' => true],\n    Foo\\Bar::class => ['dev' => true],\n];\n"
        run = lambda: runtime_setup.subprocess.run([sys.executable, str(script), str(self.project_dir)], capture_output=True, text=True)

        first, second = run(), run()
        self.assertEqual((first.stdout.strip(), second.stdout.strip()), ("missing-file", "missing-file"))
        bundles.write_text(original)
        first, second = run(), run()
        text = bundles.read_text()
        self.assertEqual((first.stdout.strip(), second.stdout.strip()), ("added", "present"))
        self.assertEqual(text.count("IndexerOtelBundle"), 1)
        self.assertEqual(text.replace("    Indexr\\Otel\\Symfony\\IndexerOtelBundle::class => ['all' => true],\n", ""), original)
        self.assertTrue(text.rstrip().endswith("];"))

    def test_a_symfony_app_without_the_registered_bundle_is_not_configured(self):
        (self.project_dir / "composer.json").write_text(json.dumps({"require": {"symfony/framework-bundle": "6.3.*", "aminmh/developer-assistant-otel-symfony": "^0.1.1"}}))
        (self.project_dir / "composer.lock").write_text(json.dumps({"packages": [{"name": "aminmh/developer-assistant-otel-symfony"}]}))
        (self.project_dir / ".env").write_text("INDEXER_PROJECT_ID=p\n")
        bundles = self.project_dir / "config" / "bundles.php"
        bundles.parent.mkdir()
        bundles.write_text("<?php\nreturn [\n];\n")
        missing = runtime_setup.instrumentation_status(self.project)
        bundles.write_text("<?php\nreturn [\n    Indexr\\Otel\\Symfony\\IndexerOtelBundle::class => ['all' => true],\n];\n")
        registered = runtime_setup.instrumentation_status(self.project)

        self.assertFalse(missing["configured"])
        self.assertIn("IndexerOtelBundle is not registered", missing["message"])
        # The library sets up its own SDK from .env; exporting OTEL_PHP_AUTOLOAD_ENABLED to the container would start the
        # SDK's autoloader first and bypass that, so nothing else is required of the container.
        self.assertTrue(registered["configured"])

    def test_a_library_older_than_0_1_2_must_be_updated_before_tracing_is_turned_on(self):
        (self.project_dir / "composer.json").write_text(json.dumps({"require": {"symfony/framework-bundle": "6.3.*", "aminmh/developer-assistant-otel-symfony": "^0.1.1"}}))
        lock = self.project_dir / "composer.lock"
        lock.write_text(json.dumps({"packages": [{"name": "aminmh/developer-assistant-otel-core", "version": "v0.1.1"},
                                                 {"name": "aminmh/developer-assistant-otel-symfony", "version": "v0.1.1"}]}))
        old = runtime_setup.instrumentation_status(self.project)
        with ExitStack() as stack:
            run = self.install_patches(stack)
            with self.assertRaises(runtime_setup.SetupInputError) as refused:
                runtime_setup.install_indexr_library("fixture-project", "p", "^0.1.2", approve=lambda *a, **k: self.fail("no approval"))
            run.assert_not_called()
        lock.write_text(json.dumps({"packages": [{"name": "aminmh/developer-assistant-otel-core", "version": "v0.1.2"},
                                                 {"name": "aminmh/developer-assistant-otel-symfony", "version": "v0.1.2"}]}))
        new = runtime_setup.instrumentation_status(self.project)

        self.assertTrue(old["needs_update"])
        self.assertFalse(old["configured"])
        self.assertIn("composer update aminmh/developer-assistant-otel-core aminmh/developer-assistant-otel-symfony", str(refused.exception))
        self.assertFalse(new["needs_update"])

    def test_method_activity_check_asks_the_running_php_container_for_ext_opentelemetry(self):
        def fake_run(args, **kwargs):
            if "detect" in args:
                return runtime_setup.subprocess.CompletedProcess(args, 0, stdout=json.dumps({"service": "php"}), stderr="")
            return runtime_setup.subprocess.CompletedProcess(args, 0, stdout=self.probe_output, stderr="")

        with ExitStack() as stack:
            stack.enter_context(patch.object(runtime_setup, "run", side_effect=fake_run))
            stack.enter_context(patch.object(runtime_setup, "compose_args", return_value=["docker", "compose"]))
            self.probe_output = "no\ninstaller\n"
            missing = runtime_setup.method_activity_status(self.project)
            self.probe_output = "no\nno-installer\n"
            no_installer = runtime_setup.method_activity_status(self.project)
            self.probe_output = "yes\ninstaller\n"
            loaded = runtime_setup.method_activity_status(self.project)
            self.probe_output = ""
            stopped = runtime_setup.method_activity_status(self.project)

        self.assertEqual((missing["status"], missing["service"]), ("missing", "php"))
        self.assertEqual(missing["image_fix"], "install-php-extensions opentelemetry")
        self.assertEqual(missing["quick_fix"], "docker compose exec php install-php-extensions opentelemetry")
        self.assertIn("pecl install opentelemetry", no_installer["image_fix"])
        self.assertIn("pecl install opentelemetry", no_installer["quick_fix"])
        self.assertEqual(loaded["status"], "loaded")
        self.assertEqual(stopped["status"], "unknown")
        self.assertIn("is not running", stopped["message"])

    # ── ext-opentelemetry without rebuilding the image ───────────────────────

    def extension_patches(self, stack: ExitStack, *, image_has_it=False, build_ok=True, after="loaded"):
        calls = []
        stack.enter_context(patch.object(runtime_setup, "find_compose_project", return_value=self.project))
        statuses = iter([{"status": "missing", "service": "php", "message": "missing"}, {"status": after, "service": "php", "message": "now loaded"}])
        stack.enter_context(patch.object(runtime_setup, "method_activity_status", side_effect=lambda project: next(statuses)))
        stack.enter_context(patch.object(runtime_setup, "_service_image", return_value=("sha256:abc123", "linux/amd64")))
        stack.enter_context(patch.object(runtime_setup, "php_extension_dir", return_value=self.root / "extensions"))
        stack.enter_context(patch.object(runtime_setup.time, "sleep"))
        stack.enter_context(patch.object(runtime_setup, "compose_args", return_value=["docker", "compose"]))

        def fake_run(args, **kwargs):
            calls.append(args)
            ok = lambda out="": runtime_setup.subprocess.CompletedProcess(args, 0, stdout=out, stderr="")
            if args[:2] == ["docker", "run"] and "--entrypoint" in args and args[args.index("--entrypoint") + 1] == "php":
                return ok(("yes" if image_has_it else "no") + "\n/usr/local/lib/php/extensions/no-debug-non-zts-20220829\n/usr/local/etc/php/conf.d\n")
            if args[:2] == ["docker", "run"]:  # the throwaway build
                if build_ok:
                    out = Path(args[args.index("-v") + 1].split(":")[0])
                    (out / "opentelemetry.so").write_text("so")
                    (out / "opentelemetry.ini").write_text("extension=opentelemetry.so\n")
                    return ok()
                return runtime_setup.subprocess.CompletedProcess(args, 1, stdout="", stderr="E: compiler missing")
            if args[:1] == ["git"]:
                return ok("c" * 40 + "\n")
            return ok("{}")

        stack.enter_context(patch.object(runtime_setup, "run", side_effect=fake_run))
        return calls

    def test_extension_is_built_once_in_a_throwaway_container_mounted_and_the_service_recreated(self):
        approvals = []
        with ExitStack() as stack:
            calls = self.extension_patches(stack)
            result = runtime_setup.install_php_extension(
                "fixture-project", approve=lambda title, items, summary="": approvals.append((title, items)))

        self.assertEqual(result["status"], "complete")
        build = next(call for call in calls if call[:2] == ["docker", "run"] and call[call.index("--entrypoint") + 1] == "sh")
        self.assertIn("--platform", build)
        self.assertIn("linux/amd64", build)
        self.assertIn("sha256:abc123", build)
        self.assertIn("install-php-extensions opentelemetry", build[-1])
        mounts = next(call for call in calls if "extension-mounts" in call)
        self.assertEqual(mounts[mounts.index("--service") + 1], "php")
        self.assertEqual(mounts[mounts.index("--extension-dir") + 1], "/usr/local/lib/php/extensions/no-debug-non-zts-20220829")
        self.assertEqual(mounts[mounts.index("--ini-dir") + 1], "/usr/local/etc/php/conf.d")
        up = [call for call in calls if call[:4] == ["docker", "compose", "up", "-d"]]
        self.assertEqual(up, [["docker", "compose", "up", "-d", "php"]])
        self.assertEqual(len(approvals), 1)
        self.assertTrue(any("temporary container" in item for item in approvals[0][1]))
        self.assertTrue(any("compose.indexr-ext.yaml" in item for item in approvals[0][1]))

    def test_extension_install_leaves_everything_alone_when_the_image_already_has_it_or_the_build_fails(self):
        with ExitStack() as stack:
            calls = self.extension_patches(stack, image_has_it=True)
            with self.assertRaises(runtime_setup.SetupInputError) as present:
                runtime_setup.install_php_extension("fixture-project", approve=lambda *a, **k: self.fail("no approval needed"))
            self.assertFalse(any(call[:4] == ["docker", "compose", "up", "-d"] for call in calls))
        with ExitStack() as stack:
            calls = self.extension_patches(stack, build_ok=False)
            failed = runtime_setup.install_php_extension("fixture-project", approve=lambda *a, **k: None)
            self.assertFalse(any("extension-mounts" in call for call in calls))

        self.assertIn("already contains ext-opentelemetry", str(present.exception))
        self.assertEqual(failed["code"], "extension_build_failed")
        self.assertIn("Nothing was changed in the project", failed["message"])
        self.assertIn("compiler missing", failed["message"])

    def test_a_previous_build_for_the_same_image_is_reused_without_building_again(self):
        approvals = []
        with ExitStack() as stack:
            calls = self.extension_patches(stack)
            digest = runtime_setup.hashlib.sha256(b"sha256:abc123|linux/amd64").hexdigest()[:12]
            built = self.root / "extensions" / digest
            built.mkdir(parents=True)
            (built / "opentelemetry.so").write_text("so")
            (built / "opentelemetry.ini").write_text("extension=opentelemetry.so\n")
            runtime_setup.install_php_extension("fixture-project", approve=lambda title, items, summary="": approvals.append(items))

        self.assertFalse(any(call[:2] == ["docker", "run"] and call[call.index("--entrypoint") + 1] == "sh" for call in calls))
        self.assertFalse(any("temporary container" in item for item in approvals[0]))

    def test_extension_mounts_are_written_to_their_own_uncommitted_file_and_added_to_compose_file(self):
        script = Path(__file__).resolve().parents[2] / "scripts" / "php-compose-telemetry.py"
        (self.project_dir / ".env").write_text("APP_ENV=dev\n")  # compose.yml (services php and database) comes from setUp
        runtime_setup.subprocess.run(["git", "init", "-q", str(self.project_dir)], check=True)
        done = runtime_setup.subprocess.run(
            [sys.executable, str(script), "extension-mounts", str(self.project_dir), "--service", "php", "--so", "/tmp/o.so", "--ini", "/tmp/o.ini",
             "--extension-dir", "/usr/local/lib/php/extensions/x", "--ini-dir", "/usr/local/etc/php/conf.d"],
            capture_output=True, text=True)
        text = (self.project_dir / "compose.indexr-ext.yaml").read_text()
        again = runtime_setup.subprocess.run(
            [sys.executable, str(script), "extension-mounts", str(self.project_dir), "--service", "php", "--so", "/tmp/o.so", "--ini", "/tmp/o.ini",
             "--extension-dir", "/usr/local/lib/php/extensions/x", "--ini-dir", "/usr/local/etc/php/conf.d"],
            capture_output=True, text=True)
        env = (self.project_dir / ".env").read_text()
        exclude = (self.project_dir / ".git" / "info" / "exclude").read_text()

        self.assertEqual((done.returncode, again.returncode), (0, 0), done.stderr + again.stderr)
        self.assertIn('"/tmp/o.so:/usr/local/lib/php/extensions/x/opentelemetry.so:ro"', text)
        self.assertIn('"/tmp/o.ini:/usr/local/etc/php/conf.d/docker-php-ext-opentelemetry.ini:ro"', text)
        self.assertEqual(env.count("compose.indexr-ext.yaml"), 1)
        self.assertIn("COMPOSE_FILE=compose.yml:compose.indexr-ext.yaml", env)
        self.assertIn("/compose.indexr-ext.yaml", exclude)

    def test_install_validates_its_inputs_and_only_runs_for_a_missing_library(self):
        with ExitStack() as stack:
            run = self.install_patches(stack, composer=self.SYMFONY_APP)
            for project_id, version in (("bad id", "^0.1.1"), ("ok", "dev-main"), ("ok", "^0.1.1; rm -rf /")):
                with self.assertRaises(runtime_setup.SetupInputError):
                    runtime_setup.install_indexr_library("fixture-project", project_id, version, approve=lambda *a, **k: None)
            (self.project_dir / "composer.json").write_text(json.dumps({"require": {"aminmh/developer-assistant-otel-symfony": "^0.1"}}))
            with self.assertRaises(runtime_setup.SetupInputError) as declared:
                runtime_setup.install_indexr_library("fixture-project", "ok", "^0.1.1", approve=lambda *a, **k: None)
            run.assert_not_called()

        self.assertIn("composer install", str(declared.exception))

    def test_the_install_operation_is_dispatched_with_desktop_approval(self):
        with patch.object(runtime_setup, "install_indexr_library", return_value={"status": "complete"}) as install:
            runtime_setup.run_helper_operation("install library", install={"project_name": "c", "project_id": "p", "version": "^0.1.1"})
        install.assert_called_once_with("c", "p", "^0.1.1", approve=runtime_setup.helper_approval)

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
            runtime_setup.write_record(state)
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
            runtime_setup.write_record(state)
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
        cleanup.reset_mock()
        with patch.object(runtime_setup, "cleanup", return_value={"status": "complete"}) as named:
            runtime_setup.run_helper_operation("cleanup", cleanup_target=("orders", "staging"))
        named.assert_called_once_with(approve=runtime_setup.helper_approval, project_id="orders", environment="staging")
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
