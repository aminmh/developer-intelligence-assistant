"""Tests for Runtime SSH OS credential-store handling.

Credential-store APIs are mocked. Parser integration uses disposable keys and
an isolated temporary ssh-agent, never the developer's Keychain or identities.
"""
from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import runtime_ssh_credentials as credentials

# Importing the helper registers shutdown cleanup for its runtime-owned agent.
# Unit tests must never stop or unlink the developer's real desktop agent.
atexit.unregister(credentials.stop_agent)


def _temporary_ssh_agent_available() -> bool:
    if not shutil.which("ssh-agent") or not shutil.which("ssh-add"):
        return False
    try:
        with tempfile.TemporaryDirectory(prefix="da-agent-test-") as directory:
            sock = Path(directory) / "agent.sock"
            started = subprocess.run(
                ["ssh-agent", "-a", str(sock), "-s"], capture_output=True, text=True,
                check=False, timeout=5,
            )
            match = re.search(r"SSH_AGENT_PID=(\d+);", started.stdout)
            if not started.returncode and match:
                subprocess.run(
                    ["ssh-agent", "-k"],
                    env={**os.environ, "SSH_AGENT_PID": match.group(1), "SSH_AUTH_SOCK": str(sock)},
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=5,
                )
                return True
    except (OSError, subprocess.TimeoutExpired):
        return False
    return False


TEMPORARY_SSH_AGENT_AVAILABLE = _temporary_ssh_agent_available()


class RuntimeSshCredentialTests(unittest.TestCase):
    def _generated_key(self, directory: str, name: str, passphrase: str) -> tuple[str, str]:
        key_path = Path(directory) / name
        subprocess.run(
            [shutil.which("ssh-keygen"), "-q", "-t", "ed25519", "-N", passphrase, "-f", str(key_path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return key_path.read_text(encoding="utf-8"), key_path.with_suffix(".pub").read_text(encoding="ascii").strip()

    def test_provider_selects_macos_keychain(self):
        with patch.object(credentials.sys, "platform", "darwin"):
            self.assertEqual(credentials._provider(), "keychain")

    def test_provider_selects_linux_secret_service_only_for_unlocked_session(self):
        with patch.object(credentials.sys, "platform", "linux"), \
                patch.dict(credentials.os.environ, {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/session"}), \
                patch.object(credentials.shutil, "which", return_value="/usr/bin/secret-tool"):
            self.assertEqual(credentials._provider(), "secret-service")

    def test_provider_reports_locked_when_linux_keyring_is_unavailable(self):
        with patch.object(credentials.sys, "platform", "linux"), \
                patch.dict(credentials.os.environ, {}, clear=True), \
                patch.object(credentials.shutil, "which", return_value=None):
            with self.assertRaisesRegex(credentials.CredentialStoreError, "unlocked Linux Secret Service") as caught:
                credentials._provider()
        self.assertTrue(caught.exception.locked)

    @unittest.skipUnless(TEMPORARY_SSH_AGENT_AVAILABLE, "sandbox does not permit a temporary SSH agent socket")
    def test_public_key_reads_valid_unencrypted_and_encrypted_keys(self):
        with tempfile.TemporaryDirectory(prefix="runtime-ssh-key-test-") as directory:
            for name, passphrase in (("unencrypted", ""), ("encrypted", "test")):
                with self.subTest(passphrase_supplied=bool(passphrase)):
                    private_key, public_key = self._generated_key(directory, name, passphrase)
                    for pasted_key in (
                        private_key,
                        private_key.replace("\n", "\r\n"),
                        f" \r\n{private_key.strip()}\r\n ",
                    ):
                        with self.subTest(line_endings="CRLF" if "\r\n" in pasted_key else "LF"):
                            self.assertEqual(
                                " ".join(credentials._public_key(pasted_key, passphrase).split()[:2]),
                                " ".join(public_key.split()[:2]),
                            )

    @unittest.skipUnless(TEMPORARY_SSH_AGENT_AVAILABLE, "sandbox does not permit a temporary SSH agent socket")
    def test_public_key_rejects_encrypted_key_with_wrong_passphrase(self):
        with tempfile.TemporaryDirectory(prefix="runtime-ssh-key-test-") as directory:
            private_key, _ = self._generated_key(directory, "encrypted", "correct-test-passphrase")
            with self.assertRaisesRegex(credentials.CredentialStoreError, "did not unlock"):
                credentials._public_key(private_key, "test")

    def test_public_key_passes_secret_on_stdin_to_an_isolated_agent(self):
        key = "TEST PRIVATE KEY MATERIAL"
        start = subprocess.CompletedProcess([], 0, "SSH_AUTH_SOCK=/tmp/test-agent; export SSH_AUTH_SOCK;\nSSH_AGENT_PID=12345; export SSH_AGENT_PID;\n", "")
        add = subprocess.CompletedProcess([], 0, b"", b"")
        listed = subprocess.CompletedProcess([], 0, b"ssh-ed25519 AAAA test\n", b"")
        stopped = subprocess.CompletedProcess([], 0, b"", b"")
        with patch.object(credentials.subprocess, "run", side_effect=[start, add, listed, stopped]) as run:
            public_key = credentials._public_key(key, "test-passphrase")

        self.assertEqual(public_key, "ssh-ed25519 AAAA test")
        self.assertEqual(run.call_args_list[1].args[0], ["ssh-add", "-"])
        self.assertEqual(
            run.call_args_list[1].kwargs["input"],
            credentials._normalize_private_key(key).encode("utf-8"),
        )
        self.assertNotIn(key, repr(run.call_args_list[1].args[0]))
        self.assertEqual(run.call_args_list[1].kwargs["env"]["DEVASSISTANT_ASKPASS_PASSPHRASE"], "test-passphrase")

    def test_public_key_reports_wrong_passphrase_without_exposing_agent_diagnostics(self):
        start = subprocess.CompletedProcess([], 0, "SSH_AGENT_PID=12345;\n", "")
        bad_passphrase = subprocess.CompletedProcess([], 1, b"", b"Bad passphrase for key (stdin).\n")
        stopped = subprocess.CompletedProcess([], 0, b"", b"")
        with patch.object(credentials.subprocess, "run", side_effect=[start, bad_passphrase, stopped]):
            with self.assertRaisesRegex(credentials.CredentialStoreError, "actual passphrase") as caught:
                credentials._public_key("TEST PRIVATE KEY MATERIAL", "test")

        self.assertNotIn("Bad passphrase for key", str(caught.exception))

    def test_keychain_store_sends_secret_on_stdin_not_process_arguments(self):
        value = {"private_key": "PRIVATE-KEY", "passphrase": "KEY-PASSPHRASE"}
        completed = subprocess.CompletedProcess([], 0, b'{"saved":true}', b"")
        with patch.object(credentials, "_provider", return_value="keychain"), \
                patch.object(credentials, "_run", return_value=completed) as run:
            credentials._store_secret(value)

        command = run.call_args.args[0]
        self.assertEqual(command, ["/usr/bin/swift", str(credentials.KEYCHAIN_HELPER)])
        self.assertNotIn("PRIVATE-KEY", command)
        self.assertNotIn("KEY-PASSPHRASE", command)
        self.assertEqual(json.loads(run.call_args.kwargs["input_data"]), {
            "action": "store", "account": credentials.ACCOUNT,
            "service": credentials.SERVICE, "value": value,
        })

    def test_keychain_store_reports_locked_status_without_exposing_secret(self):
        value = {"private_key": "PRIVATE-KEY", "passphrase": "KEY-PASSPHRASE"}
        locked = subprocess.CompletedProcess([], 1, b"", b"keychain-locked:-25308\n")
        with patch.object(credentials, "_provider", return_value="keychain"), \
                patch.object(credentials, "_run", return_value=locked):
            with self.assertRaisesRegex(credentials.CredentialStoreError, "Unlock the macOS Keychain") as caught:
                credentials._store_secret(value)

        self.assertTrue(caught.exception.locked)
        self.assertNotIn("PRIVATE-KEY", str(caught.exception))
        self.assertNotIn("KEY-PASSPHRASE", str(caught.exception))

    def test_keychain_read_returns_secret_only_from_helper_response(self):
        value = {"private_key": "PRIVATE-KEY", "passphrase": "KEY-PASSPHRASE"}
        with patch.object(credentials, "_provider", return_value="keychain"), \
                patch.object(credentials, "_keychain_request", return_value={"found": True, "value": value}) as request:
            self.assertEqual(credentials._read_secret(), value)

        request.assert_called_once_with("read")

    def test_keychain_read_reports_missing_item(self):
        with patch.object(credentials, "_provider", return_value="keychain"), \
                patch.object(credentials, "_keychain_request", return_value={"found": False}):
            self.assertIsNone(credentials._read_secret())

    def test_secret_service_store_sends_json_on_stdin(self):
        value = {"private_key": "PRIVATE-KEY", "passphrase": ""}
        completed = subprocess.CompletedProcess([], 0, b"", b"")
        with patch.object(credentials, "_provider", return_value="secret-service"), \
                patch.object(credentials, "_run", return_value=completed) as run:
            credentials._store_secret(value)

        self.assertEqual(run.call_args.args[0][0:2], ["secret-tool", "store"])
        self.assertNotIn("PRIVATE-KEY", run.call_args.args[0])
        self.assertEqual(json.loads(run.call_args.kwargs["input_data"]), value)

    def test_status_distinguishes_missing_credentials(self):
        with patch.object(credentials.sys, "platform", "linux"), \
                patch.object(credentials, "_provider", return_value="secret-service"), \
                patch.object(credentials, "_read_secret", return_value=None):
            status = credentials.credential_status()

        self.assertEqual(status, {
            "provider": "secret-service", "saved": False, "loaded": False,
            "state": "missing", "message": None,
        })

    def test_status_explains_locked_keyring(self):
        locked = credentials.CredentialStoreError("Unlock the keyring and retry.", locked=True)
        with patch.object(credentials, "_provider", side_effect=locked):
            status = credentials.credential_status()

        self.assertEqual(status["state"], "locked")
        self.assertFalse(status["saved"])
        self.assertIn("Unlock", status["message"])

    def test_save_stores_key_material_but_returns_only_status(self):
        stored = []
        private_key = "\r\n  PRIVATE-KEY\r\n  "
        passphrase = "KEY-PASSPHRASE"
        with patch.object(credentials, "ensure_agent"), \
                patch.object(credentials, "_public_key", return_value="ssh-ed25519 AAAA test"), \
                patch.object(credentials, "_read_secret", return_value=None), \
                patch.object(credentials, "_store_secret", side_effect=lambda value: stored.append(dict(value))), \
                patch.object(credentials, "_load_into_agent", return_value=True), \
                patch.object(credentials, "credential_status", return_value={"provider": "keychain"}):
            result = credentials.save_credentials(private_key, passphrase)

        self.assertEqual(stored[0]["private_key"], "PRIVATE-KEY")
        self.assertEqual(stored[0]["passphrase"], passphrase)
        self.assertEqual(result, {"saved": True, "loaded": True, "provider": "keychain"})
        self.assertNotIn(private_key, json.dumps(result))
        self.assertNotIn(passphrase, json.dumps(result))

    def test_remove_deletes_saved_item_and_reloads_agent_without_returning_secret(self):
        secret = {"private_key": "PRIVATE-KEY", "passphrase": "KEY-PASSPHRASE", "public_key": "ssh-ed25519 AAAA"}
        with patch.object(credentials, "_read_secret", return_value=secret), \
                patch.object(credentials, "_remove_agent_key") as remove_agent_key, \
                patch.object(credentials, "_delete_secret") as delete_secret, \
                patch.object(credentials, "_load_into_agent") as load_agent:
            result = credentials.remove_credentials()

        remove_agent_key.assert_called_once_with("ssh-ed25519 AAAA", owned=False)
        delete_secret.assert_called_once_with()
        load_agent.assert_called_once_with(None)
        self.assertEqual(result, {"saved": False, "loaded": False})

    def test_saved_value_must_be_a_json_object(self):
        with patch.object(credentials, "_provider", return_value="keychain"), \
                patch.object(credentials, "_keychain_request", return_value={"found": True, "value": []}):
            with self.assertRaisesRegex(credentials.CredentialStoreError, "saved Runtime SSH credential is invalid"):
                credentials._read_secret()


if __name__ == "__main__":
    unittest.main()
