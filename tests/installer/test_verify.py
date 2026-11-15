"""Live acceptance probe contracts, tested without services or host mutations."""
import importlib.util
import io
import json
from pathlib import Path
import stat
import sys
import types
import unittest
from unittest import mock

import test_config as fixture


class VerifyTest(unittest.TestCase):
    setUpFixture = fixture.ConfigTest.setUp
    write_env = fixture.ConfigTest.write_env
    read_env = fixture.ConfigTest.read_env

    def setUp(self):
        self.setUpFixture()
        self.config.generate()
        spec = importlib.util.spec_from_file_location('installer_verify_test', fixture.SOURCE / 'verify.py')
        self.verify = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.verify)

    def test_private_report_covers_every_selected_service_without_inventing_logins(self):
        self.choices['optional'] = sorted(self.config.OPTIONAL)
        self.state.save(self.state.STATE / 'choices.json', self.choices)
        self.verify.report({'app': 'FAILED'}, 'Verification incomplete')
        path = self.state.STATE / 'services.txt'
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        content = path.read_text()
        for name in self.config.selected(self.choices):
            self.assertIn(self.config.url(self.choices, name), content)
        self.assertIn('separate account', content)
        self.assertIn('no standalone authentication', content)
        self.assertIn('Internal only', content)
        self.assertIn(self.read_env('api')['NEO4J_PASSWORD'], content)

    def test_admin_credential_mismatch_does_not_modify_accounts(self):
        with mock.patch.object(self.verify, 'request', return_value=(401, b'{}', {})) as request:
            with self.assertRaisesRegex(RuntimeError, 'preserved'):
                self.verify.configure_oidc(None, self.choices, self.read_env('web'))
        self.assertEqual(request.call_count, 1)

    def test_existing_sonarqube_password_mismatch_never_uses_factory_password_or_resets_account(self):
        self.state.save(self.state.STATE / 'sonarqube-bootstrap.json', {'fresh': False})
        with mock.patch.object(self.verify, 'request', return_value=(200, b'{"valid":false}', {})) as request:
            with self.assertRaisesRegex(RuntimeError, 'Existing account preserved'):
                self.verify.sonarqube_check(None, self.choices)
        self.assertEqual(request.call_count, 1)
        self.assertNotIn('data', request.call_args.kwargs)

    def test_fresh_sonarqube_password_initialization_is_reentrant_without_second_reset(self):
        self.state.save(self.state.STATE / 'sonarqube-bootstrap.json', {'fresh': True})
        responses = [(200, b'{"valid":false}', {}), (200, b'{"valid":true}', {}),
                     (204, b'', {}), (200, b'{"valid":true}', {})]
        with mock.patch.object(self.verify, 'request', side_effect=responses):
            self.verify.sonarqube_check(None, self.choices)
        self.assertEqual(self.state.load(self.state.STATE / 'sonarqube-bootstrap.json'), {'fresh': False})
        with mock.patch.object(self.verify, 'request', return_value=(200, b'{"valid":true}', {})) as request:
            self.verify.sonarqube_check(None, self.choices)
        self.assertEqual(request.call_count, 1)

    def test_existing_oidc_secret_mismatch_never_issues_a_write(self):
        responses = [(200, b'{"access_token":"ephemeral"}', {}),
                     (200, b'[{"id":"existing"}]', {}), (200, b'{"value":"different"}', {})]
        with mock.patch.object(self.verify, 'request', side_effect=responses) as request:
            with self.assertRaisesRegex(RuntimeError, 'No secret was reset'):
                self.verify.configure_oidc(None, self.choices, self.read_env('web'))
        self.assertEqual(request.call_count, 3)
        self.assertTrue(all('method' not in call.kwargs for call in request.call_args_list))

    def test_upload_probe_performs_create_head_two_patches_and_checks_object(self):
        endpoint = self.config.url(self.choices, 'uploads') + '/files/'
        session = {'tus_endpoint': endpoint, 'upload_token': 'ephemeral', 'upload_session_id': 'unique-session'}
        offset = 0
        methods = []

        def respond(opener, address, **kwargs):
            nonlocal offset
            method = kwargs.get('method', 'POST' if kwargs.get('data') is not None else 'GET')
            methods.append(method)
            if address.endswith('/api/rag/upload'):
                return 200, json.dumps(session).encode(), {}
            if method == 'OPTIONS': return 204, b'', {'Access-Control-Allow-Origin': self.config.url(self.choices, 'app'), 'Access-Control-Allow-Headers': 'Authorization,Tus-Resumable,Upload-Offset,Content-Type'}
            if method == 'POST': return 201, b'', {'Location': endpoint + 'upload-id', 'Access-Control-Allow-Origin': self.config.url(self.choices, 'app')}
            if method == 'HEAD': return 200, b'', {'Upload-Offset': str(offset)}
            offset += len(kwargs['data'])
            self.assertEqual(kwargs['headers']['Content-Type'], 'application/offset+octet-stream')
            return 204, b'', {}

        compose = mock.Mock()
        with mock.patch.dict(sys.modules, {'cli': types.SimpleNamespace(compose=compose)}), mock.patch.object(self.verify, 'request', side_effect=respond):
            self.verify.upload_check(None, self.choices)
        self.assertEqual(methods, ['POST', 'OPTIONS', 'POST', 'HEAD', 'PATCH', 'HEAD', 'PATCH'])
        code = compose.call_args.args[-1]
        self.assertIn('rag-uploads/unique-session/installer-probe.zip', code)
        self.assertIn('assert actual==expected', code)

    def test_upload_probe_rejects_internal_or_different_public_endpoint(self):
        session = {'tus_endpoint': 'http://tusd:1080/files/'}
        with mock.patch.dict(sys.modules, {'cli': types.SimpleNamespace(compose=mock.Mock())}), mock.patch.object(self.verify, 'request', return_value=(200, json.dumps(session).encode(), {})):
            with self.assertRaisesRegex(RuntimeError, 'incorrect public HTTPS'):
                self.verify.upload_check(None, self.choices)

    def test_failed_route_is_not_overwritten_by_passing_authentication(self):
        request = mock.Mock(return_value=(200, b'{"status":"ok","api_version":28}', {}))

        def curl(command, **kwargs):
            return types.SimpleNamespace(returncode=0, stdout='200')  # Qdrant must reject anonymous access.

        with mock.patch.object(self.verify, 'browser', return_value=(None, [])), mock.patch.object(self.verify, 'request', request), \
                mock.patch.object(self.verify.subprocess, 'run', side_effect=curl), \
                mock.patch.object(self.verify, 'configure_oidc'), mock.patch.object(self.verify, 'login'), \
                mock.patch.object(self.verify, 'logout'), mock.patch.object(self.verify, 'upload_check'), \
                mock.patch.object(self.verify, 'bolt_check'), mock.patch.object(self.verify, 'grpc_check'), \
                mock.patch.object(self.verify.time, 'sleep'), \
                mock.patch.dict(sys.modules, {'cli': types.SimpleNamespace(compose=mock.Mock())}), \
                mock.patch('sys.stdout', new_callable=io.StringIO):
            with self.assertRaisesRegex(RuntimeError, 'Live verification failed'):
                self.verify.verify()
        result = self.state.load(self.state.STATE / 'verification.json')
        self.assertEqual(result['statuses']['qdrant'], 'FAILED')
        self.assertIn('qdrant', result['failed_checks'])


if __name__ == '__main__':
    unittest.main()
