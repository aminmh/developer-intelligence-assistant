"""Local validation tests for the Runtime SSH tunnel target contract."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Python-Service" / "app"))
import telemetry.ssh_tunnel as ssh_tunnel


class RuntimeSshTunnelTargetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runtime-tunnel-")
        self.target_path = Path(self.temp.name) / "ssh-target.json"
        self.target_patch = patch.object(ssh_tunnel, "TARGET", self.target_path)
        self.target_patch.start()

    def tearDown(self):
        self.target_patch.stop()
        self.temp.cleanup()

    def test_missing_or_unreadable_target_defaults_to_local(self):
        self.assertEqual(ssh_tunnel.read_target(), {"mode": "local"})
        self.target_path.write_text("not-json", encoding="utf-8")
        self.assertEqual(ssh_tunnel.read_target(), {"mode": "local"})

    def test_non_remote_mode_does_not_start_a_remote_connection(self):
        self.target_path.write_text('{"mode":"disabled"}', encoding="utf-8")
        self.assertEqual(ssh_tunnel.read_target(), {"mode": "disabled"})

    def test_valid_remote_target_requires_identity_and_valid_port(self):
        target = {
            "mode": "remote", "host": "db.example.test", "ssh_user": "telemetry",
            "ssh_port": 2222, "host_fingerprint": "SHA256:verified-fingerprint",
        }
        import json
        self.target_path.write_text(json.dumps(target), encoding="utf-8")
        self.assertEqual(ssh_tunnel.read_target(), target)

        target["ssh_port"] = True
        self.target_path.write_text(json.dumps(target), encoding="utf-8")
        self.assertEqual(ssh_tunnel.read_target(), {"mode": "invalid"})

    def test_remote_target_requires_sha256_host_fingerprint(self):
        self.target_path.write_text(
            '{"mode":"remote","host":"db.example.test","ssh_user":"telemetry",'
            '"ssh_port":22,"host_fingerprint":"MD5:untrusted"}',
            encoding="utf-8",
        )
        self.assertEqual(ssh_tunnel.read_target(), {"mode": "invalid"})

    def test_trusted_key_requires_exact_fingerprint_match(self):
        class FakeKey:
            def get_fingerprint(self, algorithm):
                self.algorithm = algorithm
                return "SHA256:expected"

        key = FakeKey()
        self.assertTrue(ssh_tunnel.trusted_key("SHA256:expected", key))
        self.assertFalse(ssh_tunnel.trusted_key("SHA256:other", key))
        self.assertFalse(ssh_tunnel.trusted_key("", key))
        self.assertEqual(key.algorithm, "sha256")


if __name__ == "__main__":
    unittest.main()
