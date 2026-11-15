"""Isolated installer configuration/state contracts; no Docker or host mutations."""
from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import shlex
import sys
import tempfile
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[2] / "scripts/installer"
API_SECRETS = ("MINIO_ROOT_PASSWORD", "UPLOAD_TOKEN_SECRET", "TUSD_HOOK_SECRET", "DOWNLOAD_TOKEN_SECRET",
               "RAG_CALLBACK_SECRET", "NEO4J_PASSWORD", "TELEMETRY_CLICKHOUSE_PASSWORD", "ANALYZER_POSTGRES_PASSWORD", "SONARQUBE_ADMIN_PASSWORD")
WEB_SECRETS = ("OIDC_CLIENT_SECRET", "SESSION_SECRET", "KEYCLOAK_ADMIN_PASSWORD", "KEYCLOAK_BOOTSTRAP_PASSWORD",
               "OUTLINE_SECRET_KEY", "OUTLINE_UTILS_SECRET", "OUTLINE_DATABASE_PASSWORD", "OUTLINE_OIDC_CLIENT_SECRET")


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="installer config test ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {"INSTALL_ROOT": str(self.root)}).start()
        # Each test imports fresh modules against INSTALL_ROOT. Restore any
        # modules owned by another test suite when this fixture finishes.
        original = {name: sys.modules.get(name) for name in ("state", "config", "system")}

        def restore_modules():
            for name, module in original.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module

        self.addCleanup(restore_modules)
        for name in original:
            spec = importlib.util.spec_from_file_location(name, SOURCE / (name + ".py"))
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            setattr(self, name, module)
        self.state.private_dir(self.state.STATE)
        self.choices = self.config.defaults()
        self.state.save(self.state.STATE / "choices.json", self.choices)
        (self.root / "api").mkdir()
        (self.root / "web/keycloak").mkdir(parents=True)
        self.realm_path = self.root / "web/keycloak/developer-assistant-realm.json"
        self.realm_path.write_text(json.dumps({"realm": "developer-assistant", "clients": [{"clientId": "old"}],
                                               "users": [{"id": "existing", "username": "existing-user"}]}))

    def write_env(self, folder, values):
        path = self.root / folder / ".env"
        path.write_text("# user comment\n" + "\n".join(f'{key}="{value}"' for key, value in values.items()) + "\n")
        return path

    def read_env(self, folder):
        return self.state.env_read(self.root / folder / ".env")

    def save_choices(self):
        self.state.save(self.state.STATE / "choices.json", self.choices)

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    def test_fresh_credentials_are_strong_unique_and_stable_on_resume(self):
        self.config.generate()
        api, web = self.read_env("api"), self.read_env("web")
        values = [api[key] for key in API_SECRETS] + [web[key] for key in WEB_SECRETS]
        values.append(self.state.env_read(self.state.STATE / "proxy.env")["QDRANT_PUBLIC_API_KEY"])
        for value in values:
            self.assertRegex(value, r"^[0-9a-f]{64}$")
        self.assertEqual(len(values), len(set(values)))
        self.assertEqual(api["RAG_CALLBACK_SECRET"], web["RAG_CALLBACK_SECRET"])
        before = self.snapshot()
        self.config.generate()
        self.assertEqual(self.snapshot(), before)

    def test_existing_nonempty_secrets_including_weak_values_are_preserved(self):
        api = {key: "existing-api-" + key for key in API_SECRETS}
        web = {key: "existing-web-" + key for key in WEB_SECRETS}
        api["NEO4J_PASSWORD"] = "weak"
        web["SESSION_SECRET"] = "weak"
        web["RAG_CALLBACK_SECRET"] = api["RAG_CALLBACK_SECRET"]
        api.update(MINIO_ROOT_USER="existing-minio", NEO4J_USER="existing-neo4j", USER_DEFINED="keep-me")
        web.update(OIDC_CLIENT_ID="existing-client", OIDC_BOOTSTRAP_ADMIN_SUB="existing-subject")
        api_path, web_path = self.write_env("api", api), self.write_env("web", web)
        original_api, original_web = api_path.read_bytes(), web_path.read_bytes()
        self.config.generate()
        for key, value in api.items():
            self.assertEqual(self.read_env("api")[key], value)
        for key, value in web.items():
            self.assertEqual(self.read_env("web")[key], value)
        self.assertIn("# user comment", api_path.read_text())
        self.assertEqual((self.state.STATE / "backups/api.env").read_bytes(), original_api)
        self.assertEqual((self.state.STATE / "backups/web.env").read_bytes(), original_web)
        self.config.generate()
        self.assertEqual((self.state.STATE / "backups/api.env").read_bytes(), original_api)

    def test_empty_credentials_are_filled(self):
        self.write_env("api", {"NEO4J_PASSWORD": "", "MINIO_ROOT_PASSWORD": ""})
        self.write_env("web", {"SESSION_SECRET": ""})
        self.config.generate()
        self.assertRegex(self.read_env("api")["NEO4J_PASSWORD"], r"^[0-9a-f]{64}$")
        self.assertRegex(self.read_env("web")["SESSION_SECRET"], r"^[0-9a-f]{64}$")

    def test_shared_secret_present_only_in_web_is_adopted_without_rotation(self):
        self.write_env("web", {"RAG_CALLBACK_SECRET": "keep-existing-callback"})
        self.config.generate()
        self.assertEqual(self.read_env("api")["RAG_CALLBACK_SECRET"], "keep-existing-callback")

    def test_keycloak_template_preserves_passwords_with_json_and_sed_metacharacters(self):
        realm = json.loads(self.realm_path.read_text())
        realm["users"][0]["credentials"] = [{"value": "__KEYCLOAK_BOOTSTRAP_PASSWORD__"}]
        self.realm_path.write_text(json.dumps(realm))
        self.config.generate()
        service = ""
        for line in (self.root / "web/docker-compose.override.yml").read_text().splitlines():
            if line.startswith("  ") and not line.startswith("    "): service = line.strip()
            if service == "keycloak:" and line.startswith("    command:"):
                command = json.loads(line.split(": ", 1)[1])[0].replace("$$", "$")
        runtime = self.root / "keycloak-runtime"
        (runtime / "realm-template").mkdir(parents=True)
        (runtime / "bin").mkdir()
        (runtime / "realm-template/developer-assistant-realm.json").write_bytes((self.state.STATE / "realm/developer-assistant-realm.json").read_bytes())
        executable = runtime / "bin/kc.sh"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        secret = 'preserve&pipe|quote"backslash\\single\''
        result = subprocess.run(["bash", "-c", command.replace("/opt/keycloak", shlex.quote(str(runtime)))],
                                env=dict(os.environ, OIDC_CLIENT_SECRET=secret, KEYCLOAK_BOOTSTRAP_PASSWORD=secret), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        generated = json.loads((runtime / "data/import/developer-assistant-realm.json").read_text())
        self.assertEqual(generated["clients"][0]["secret"], secret)
        self.assertEqual(generated["users"][0]["credentials"][0]["value"], secret)

    def test_mismatched_callback_secrets_fail_before_any_file_mutation(self):
        self.write_env("api", {"RAG_CALLBACK_SECRET": "api-callback"})
        self.write_env("web", {"RAG_CALLBACK_SECRET": "web-callback"})
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "differ"):
            self.config.generate()
        self.assertEqual(self.snapshot(), before)

    def test_user_compose_overrides_fail_before_any_file_mutation(self):
        for folder in ("api", "web"):
            for name in ("docker-compose.override.yml", "compose.override.yaml", "compose.override.yml", "docker-compose.override.yaml"):
                with self.subTest(folder=folder, name=name):
                    path = self.root / folder / name
                    path.write_text("services: {}\n# user owned\n")
                    before = self.snapshot()
                    with self.assertRaisesRegex(ValueError, "manual merge"):
                        self.config.generate()
                    self.assertEqual(self.snapshot(), before)
                    path.unlink()

    def test_certificate_changes_recreate_the_correct_consumers(self):
        for name in ("proxy", "neo4j"):
            directory = self.state.STATE / "certs" / name
            directory.mkdir(parents=True)
            (directory / "cert.pem").write_text("original identity")
        self.config.generate()
        api = (self.root / "api/docker-compose.override.yml").read_text()
        web = (self.root / "web/docker-compose.override.yml").read_text()
        self.assertIn("devassistant.local-tls", web)
        self.assertIn("devassistant.local-tls", api)
        (self.state.STATE / "certs/proxy/cert.pem").write_text("renewed identity")
        self.config.make_overrides(self.choices)
        self.assertNotEqual((self.root / "web/docker-compose.override.yml").read_text(), web)
        self.assertEqual((self.root / "api/docker-compose.override.yml").read_text(), api)

    def test_public_urls_and_oidc_redirects_follow_editable_hostnames_and_ports(self):
        self.choices["optional"] = ["drawio", "outline", "sonarqube"]
        for index, name in enumerate(self.config.SERVICES):
            self.choices["hosts"][name] = {"hostname": name + ".custom.test", "port": 14430 + index}
        self.choices.update(bolt_port=17687, grpc_port=16334)
        self.save_choices()
        self.config.generate()
        web = self.read_env("web")
        self.assertEqual(web["APP_URL"], "https://app.custom.test:14430")
        self.assertEqual(web["OIDC_REDIRECT_URI"], web["APP_URL"] + "/api/auth/callback")
        self.assertEqual(self.read_env("api")["WEB_ORIGIN"], web["APP_URL"])
        self.assertEqual(web["OIDC_ISSUER"], "https://keycloak.custom.test:14431/realms/developer-assistant")
        self.assertEqual(web["VITE_DRAWIO_ORIGIN"], self.config.url(self.choices, "drawio"))
        realm = json.loads((self.state.STATE / "realm/developer-assistant-realm.json").read_text())
        self.assertEqual(realm["clients"][0]["redirectUris"], [web["OIDC_REDIRECT_URI"]])
        self.assertEqual(realm["clients"][0]["webOrigins"], [web["APP_URL"]])
        self.assertEqual(realm["clients"][0]["secret"], "__OIDC_CLIENT_SECRET__")
        self.assertEqual(realm["users"][0]["id"], web["OIDC_BOOTSTRAP_ADMIN_SUB"])
        self.assertIn("https://qdrant.custom.test:16334", (self.state.STATE / "Caddyfile").read_text())

    def test_default_https_port_is_omitted_from_public_url(self):
        self.assertEqual(self.config.url(self.choices, "app"), "https://app.localhost.dev")

    def test_colliding_endpoints_and_private_or_reserved_ports_are_rejected(self):
        changes = [
            lambda c: c["hosts"]["qdrant"].update(c["hosts"]["app"]),
            lambda c: c.update(bolt_port=443), lambda c: c.update(grpc_port=443),
            lambda c: c.update(bolt_port=c["grpc_port"]),
        ]
        changes += [lambda c, port=p: c["hosts"]["app"].update(port=port)
                    for p in (47831, 8765, 11434, 24318, 9443, 0, 65536, True, "443")]
        for change in changes:
            candidate = copy.deepcopy(self.choices)
            change(candidate)
            with self.subTest(choices=candidate), self.assertRaises(ValueError):
                self.config.validate(candidate)

    def test_hostname_validation_rejects_config_injection_and_invalid_dns(self):
        for hostname in ("localhost", "UPPER.test", "bad_name.test", "-bad.test", "bad..test", "app.test\n}", "app.test:443"):
            candidate = copy.deepcopy(self.choices)
            candidate["hosts"]["app"]["hostname"] = hostname
            with self.subTest(hostname=hostname), self.assertRaises(ValueError):
                self.config.validate(candidate)

    def test_same_hostname_with_distinct_https_ports_is_valid(self):
        self.choices["hosts"]["qdrant"] = {"hostname": self.choices["hosts"]["app"]["hostname"], "port": 8443}
        self.assertEqual(self.config.validate(self.choices), self.choices)

    def test_generated_overrides_replace_published_ports_and_bind_loopback(self):
        self.choices["optional"] = sorted(self.config.OPTIONAL)
        self.save_choices()
        self.config.generate()
        expected = {"api": {"tusd": [], "minio": [], "qdrant": [], "neo4j": ["127.0.0.1:7687:7687"], "analyzer-worker": []},
                    "web": {"web": [], "keycloak": [], "outline": [], "drawio": [],
                            "local-https": ["127.0.0.1:443:443", "127.0.0.1:6334:6334"]}}
        for folder, ports in expected.items():
            text = (self.root / folder / "docker-compose.override.yml").read_text()
            self.assertTrue(text.startswith(self.config.MARKER))
            actual = {}
            service = None
            for line in text.splitlines():
                if line.startswith("  ") and not line.startswith("    "):
                    service = line.strip().rstrip(":")
                if line.startswith("    ports:"):
                    self.assertTrue(line.startswith("    ports: !override "))
                    actual[service] = json.loads(line.split("!override ", 1)[1])
            self.assertEqual(actual, ports)

    def test_existing_application_data_and_realm_source_are_unchanged(self):
        files = ("api/data/neo4j/graph.db", "api/data/qdrant/collection.bin", "api/data/minio/object", "web/data/keycloak/database", "api/.cache/download.part")
        for name in files:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"existing application data\x00" + name.encode())
        original = {name: (self.root / name).read_bytes() for name in files}
        realm = self.realm_path.read_bytes()
        self.config.generate()
        for name, value in original.items():
            self.assertEqual((self.root / name).read_bytes(), value)
        self.assertEqual(self.realm_path.read_bytes(), realm)

    def test_hosts_block_is_idempotent_and_preserves_unowned_content(self):
        original = "127.0.0.1 localhost\n::1 localhost\n# my database\n192.0.2.5 unrelated.test # keep\n"
        proposed = self.config.hosts_block(original, self.choices)
        self.assertTrue(proposed.startswith(original))
        self.assertEqual(self.config.hosts_block(proposed, self.choices), proposed)
        self.assertEqual(proposed.count("# BEGIN DEVASSISTANT INSTALLER"), 1)
        for service in self.config.selected(self.choices):
            self.assertIn(self.choices["hosts"][service]["hostname"], proposed)
        self.assertNotIn("outline.localhost.dev", proposed)
        self.choices["hosts"]["app"]["hostname"] = "new-app.test"
        edited = self.config.hosts_block(proposed, self.choices)
        self.assertIn("new-app.test", edited)
        self.assertNotIn("app.localhost.dev", edited)

    def test_hosts_conflicting_unowned_mapping_is_rejected(self):
        for address in ("192.0.2.1", "0.0.0.0", "2001:db8::1"):
            with self.subTest(address=address), self.assertRaisesRegex(ValueError, "mapping"):
                self.config.hosts_block(f"{address} other.test app.localhost.dev # user mapping\n", self.choices)

    def test_hosts_malformed_blocks_are_rejected(self):
        begin, end = "# BEGIN DEVASSISTANT INSTALLER", "# END DEVASSISTANT INSTALLER"
        for original in (begin + "\n", end + "\n", begin + "\n" + begin + "\n" + end + "\n" + end + "\n", end + "\n" + begin + "\n"):
            with self.subTest(original=original), self.assertRaisesRegex(ValueError, "Malformed"):
                self.config.hosts_block(original, self.choices)

    def test_generated_secrets_backups_and_state_have_private_modes(self):
        self.write_env("api", {"NEO4J_PASSWORD": "existing-value"})
        self.write_env("web", {"SESSION_SECRET": "existing-value"})
        self.config.generate()
        for name in ("api/.env", "web/.env", ".install/choices.json", ".install/proxy.env", ".install/backups/api.env", ".install/backups/web.env"):
            with self.subTest(path=name):
                self.assertEqual(stat.S_IMODE((self.root / name).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.state.STATE / "realm/developer-assistant-realm.json").stat().st_mode), 0o644)
        for name in (".install", ".install/backups", ".install/realm"):
            self.assertEqual(stat.S_IMODE((self.root / name).stat().st_mode), 0o700)

    def test_redaction_hides_known_secrets_from_all_env_sources(self):
        self.write_env("api", {"NEO4J_PASSWORD": "known-api-secret", "UPLOAD_TOKEN_SECRET": "known-upload-token"})
        self.write_env("web", {"SESSION_SECRET": "known-web-secret"})
        self.state.env_update(self.state.STATE / "proxy.env", {"QDRANT_PUBLIC_API_KEY": "known-proxy-key"})
        line = "failure known-api-secret known-upload-token known-web-secret known-proxy-key ordinary-text"
        result = self.state.redact(line)
        for value in ("known-api-secret", "known-upload-token", "known-web-secret", "known-proxy-key"):
            self.assertNotIn(value, result)
        self.assertIn("ordinary-text", result)
        self.assertEqual(result.count("[REDACTED]"), 4)

    def test_invalid_env_suppresses_output_instead_of_leaking_secrets(self):
        (self.root / "api/.env").write_text('SESSION_SECRET="first"\nSESSION_SECRET="second"\n')
        self.assertIn("output suppressed", self.state.redact("first second"))

    def test_env_parser_never_executes_persisted_shell_text(self):
        marker = self.root / "never-created"
        path = self.root / "api/.env"
        path.write_text(f"NEO4J_PASSWORD=$(touch {marker})\n")
        with self.assertRaises(ValueError):
            self.state.env_read(path)
        self.assertFalse(marker.exists())

    def test_checkpoint_reuses_unchanged_input_and_invalidates_every_stage_on_input_change(self):
        paths = (".install/choices.json", "api/.env", "web/docker-compose.yml", "web/docker-compose.override.yml", "scripts/installer/local.py", "scripts/installer/local.sh", "api/Dockerfile", "web/package-lock.json", "api/Python-Service/requirements.txt", "web/src/main.ts", "api/Python-Service/app/main.py")
        for name in paths:
            with self.subTest(path=name):
                for stage in self.state.STAGES:
                    self.state.checkpoint(stage)
                    self.assertTrue(self.state.checkpoint(stage, valid=True))
                path = self.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text((path.read_text() if path.exists() else "") + "\nchanged-input\n")
                for stage in self.state.STAGES:
                    self.assertFalse(self.state.checkpoint(stage, valid=True))
                self.assertEqual(stat.S_IMODE((self.state.STATE / "checkpoint.json").stat().st_mode), 0o600)

    def test_mutable_downloads_and_database_files_do_not_invalidate_checkpoint(self):
        self.state.checkpoint("images")
        for name in (".install/downloads/image.part", "api/data/qdrant/collection.db", "web/src/ignored.log", "api/Python-Service/app/__pycache__/module.pyc"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("mutable bytes")
        self.assertTrue(self.state.checkpoint("images", valid=True))

    def test_occupied_unowned_public_port_is_actionable(self):
        sock = mock.MagicMock()
        sock.__enter__.return_value.bind.side_effect = OSError("occupied")
        with mock.patch.object(self.system.shutil, "which", return_value=None), mock.patch.object(self.system.socket, "socket", return_value=sock):
            with self.assertRaisesRegex(RuntimeError, "occupied.*choices.json"):
                self.system.check_ports()

    def test_installer_owned_published_ports_allow_resume(self):
        sock = mock.MagicMock()
        sock.__enter__.return_value.bind.side_effect = OSError("occupied")
        rows = [{"Service": "local-https", "Publishers": [{"PublishedPort": 443}, {"PublishedPort": 6334}]},
                {"Service": "neo4j", "Publishers": [{"PublishedPort": 7687}]}]
        result = mock.Mock(returncode=0, stdout=json.dumps(rows))
        with mock.patch.object(self.system.shutil, "which", return_value="docker"), mock.patch.object(self.system.socket, "socket", return_value=sock), mock.patch.object(self.system.subprocess, "run", return_value=result), mock.patch("sys.stdout", new_callable=io.StringIO):
            self.system.check_ports()


if __name__ == "__main__":
    unittest.main()
