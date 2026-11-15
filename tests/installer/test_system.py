"""Dependency reports, certificate reuse and real installer locking."""
import contextlib
import io
import os
import signal
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

import test_config as fixture


class SystemTest(unittest.TestCase):
    def setUp(self):
        fixture.ConfigTest.setUp(self)

    def host_output(self, *command, **kwargs):
        if command[:2] == ('docker', 'info'):
            return 'linux' if 'OSType' in command[-1] else '17179869184 8'
        if command[:3] == ('docker', 'context', 'inspect'):
            return '[{"Endpoints":{"docker":{"Host":"unix:///local.sock"}}}]'
        if command[:3] == ('docker', 'buildx', 'inspect'): return 'BuildKit version: v0.20.0'
        if command[:3] == ('docker', 'compose', 'version'): return '2.30.0'
        if command[:3] == ('docker', 'buildx', 'version'): return 'github.com/docker/buildx v0.19.0'
        return {'docker': 'Docker version 28.0.0', 'node': 'v22.0.0', 'ollama': 'ollama version 0.12.0',
                'mkcert': 'v1.4.4'}.get(command[0], 'Python 3.14.0')

    @contextlib.contextmanager
    def host(self, output=None):
        with mock.patch.object(self.system.platform, 'system', return_value='Darwin'), \
                mock.patch.object(self.system.platform, 'machine', return_value='arm64'), \
                mock.patch.object(self.system.shutil, 'which', side_effect=lambda name: name), \
                mock.patch.object(self.system.shutil, 'disk_usage', return_value=mock.Mock(free=100 * 1024**3)), \
                mock.patch.object(self.system, 'run', side_effect=output or self.host_output), \
                mock.patch.dict(os.environ, {'DOCKER_HOST': ''}), \
                mock.patch('sys.stdout', new_callable=io.StringIO) as stream:
            yield stream

    def test_dependency_report_shows_required_detected_purpose_actions_and_container_boundary(self):
        with self.host() as stream:
            self.assertTrue(self.system.report(strict=True))
        result = stream.getvalue()
        for expected in ('Dependency | Required | Detected | Status | Purpose | Proposed action', 'Containers provide:', 'BuildKit | 0.12.0+', 'Docker context: local'):
            self.assertIn(expected, result)

    def test_old_dependency_is_reported_and_blocks_strict_check(self):
        def output(*args, **kwargs):
            return 'v18.0.0' if args[0] == 'node' else self.host_output(*args, **kwargs)
        with self.host(output) as stream:
            self.assertFalse(self.system.report(strict=True))
        self.assertIn('Install Node.js >=22', stream.getvalue())

    def test_remote_docker_context_is_rejected(self):
        with self.host(), mock.patch.dict(os.environ, {'DOCKER_HOST': 'ssh://remote'}):
            self.assertFalse(self.system.report(strict=True))

    def test_unknown_platform_cannot_reach_install_review(self):
        with self.host(), mock.patch.object(self.system.platform, 'machine', return_value='unsupported'):
            self.assertFalse(self.system.report(strict=False))

    def test_linux_headless_report_with_supported_distribution(self):
        original = Path.read_text
        def read(path, *args, **kwargs):
            return 'ID=ubuntu\nID_LIKE=debian\n' if str(path) == '/etc/os-release' else original(path, *args, **kwargs)
        with self.host() as stream, mock.patch.object(self.system.platform, 'system', return_value='Linux'), \
                mock.patch.object(Path, 'read_text', read), mock.patch.dict(os.environ, {'DISPLAY': '', 'WAYLAND_DISPLAY': ''}):
            self.assertTrue(self.system.report(strict=True))
        self.assertIn('Headless:', stream.getvalue())

    def test_certificates_reuse_valid_identity_and_regenerate_for_a_different_ca(self):
        for name in ('proxy', 'neo4j'):
            directory = self.state.STATE / 'certs' / name
            directory.mkdir(parents=True)
            (directory / 'cert.pem').write_text('certificate fixture')
            (directory / 'key.pem').write_text('private key fixture')
        current_hosts = self.config.hosts_block('127.0.0.1 localhost\n', self.choices)
        original = Path.read_text
        def read(path, *args, **kwargs):
            return current_hosts if str(path) == '/etc/hosts' else original(path, *args, **kwargs)
        def output(*args, **kwargs):
            if args[0] == 'mkcert': return str(self.root)
            return 'public key' if '-pubkey' in args or '-pubout' in args else 'valid'
        with mock.patch.object(Path, 'read_text', read), mock.patch.object(self.system, 'run', side_effect=output), \
                mock.patch.object(self.system.subprocess, 'run') as commands, mock.patch('sys.stdout', new_callable=io.StringIO):
            self.system.certificates()
            self.assertEqual(commands.call_count, 1)  # mkcert -install still revalidates OS trust.
        def changed_ca(*args, **kwargs):
            return '' if args[:2] == ('openssl', 'verify') else output(*args, **kwargs)
        with mock.patch.object(Path, 'read_text', read), mock.patch.object(self.system, 'run', side_effect=changed_ca), \
                mock.patch.object(self.system.subprocess, 'run') as commands, mock.patch('sys.stdout', new_callable=io.StringIO):
            self.system.certificates()
            self.assertEqual(commands.call_count, 3)

    def test_os_lock_refuses_concurrent_installer_and_releases_after_process_exit(self):
        cli = fixture.SOURCE / 'cli.py'
        ready = self.root / 'lock-ready'
        # Shell writes a marker only after the real CLI has acquired its lock.
        first = subprocess.Popen([sys.executable, str(cli), 'lock', '-c', 'touch "$1"; sleep 10', 'lock-test', str(ready)],
                                 env=dict(os.environ, INSTALL_ROOT=str(self.root)), stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            for _ in range(100):
                if ready.exists(): break
                time.sleep(0.02)
            self.assertTrue(ready.exists())
            second = subprocess.run([sys.executable, str(cli), 'lock', '-c', 'true'], capture_output=True, text=True,
                                    env=dict(os.environ, INSTALL_ROOT=str(self.root)))
            self.assertNotEqual(second.returncode, 0)
            self.assertIn('Another installer is running', second.stderr)
        finally:
            if first.poll() is None:
                os.killpg(first.pid, signal.SIGTERM)
            first.communicate(timeout=15)
        third = subprocess.run([sys.executable, str(cli), 'lock', '-c', 'true'], capture_output=True,
                               env=dict(os.environ, INSTALL_ROOT=str(self.root)))
        self.assertEqual(third.returncode, 0)


if __name__ == '__main__':
    unittest.main()
