"""Validate generated overrides with the installed Compose parser, no daemon."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

import test_config as fixture

SOURCE = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which('docker'), 'Docker CLI is unavailable')
class ComposeTest(unittest.TestCase):
    def setUp(self):
        fixture.ConfigTest.setUp(self)
        for folder in ('api', 'web'):
            shutil.copy2(SOURCE / folder / 'docker-compose.yml', self.root / folder / 'docker-compose.yml')

    def document(self, folder, optional):
        command = ['docker', 'compose', '--project-directory', str(self.root / folder)]
        profiles = ['local-auth', *[name for name in ('outline', 'drawio') if name in optional]] if folder == 'web' else ['runtime', 'runtime-local', *(['analyzer'] if 'sonarqube' in optional else [])]
        for profile in profiles: command += ['--profile', profile]
        result = subprocess.run(command + ['config', '--format', 'json'], capture_output=True, text=True,
                                env=dict(os.environ, RUNTIME_SSH_AGENT_SOURCE='/tmp/installer-unused.sock'))
        # Do not print config output on failure: it contains generated credentials.
        self.assertEqual(result.returncode, 0, 'Compose rejected the generated configuration (output withheld)')
        return json.loads(result.stdout)['services']

    def test_default_and_optional_services_have_valid_isolated_routes(self):
        for optional in ([], ['outline', 'drawio', 'sonarqube']):
            with self.subTest(optional=optional):
                self.choices['optional'] = optional
                self.state.save(self.state.STATE / 'choices.json', self.choices)
                self.config.generate()
                api, web = self.document('api', optional), self.document('web', optional)
                for service in ('tusd', 'minio', 'qdrant'):
                    self.assertFalse(api[service].get('ports'))
                    self.assertIn('devassistant_net', api[service]['networks'])
                self.assertIn('-behind-proxy', api['tusd']['command'])
                php_mount = next(v for v in api['python-api']['volumes'] if v['target'] == '/opt/php-cli')
                self.assertEqual(php_mount['type'], 'volume')
                self.assertIn('${NEO4J_AUTH%%/*}', api['neo4j']['healthcheck']['test'][1])
                for document in (api, web):
                    for service in document.values():
                        for port in service.get('ports', []):
                            self.assertEqual(port['host_ip'], '127.0.0.1')
                for service in ('web', 'keycloak'):
                    self.assertFalse(web[service].get('ports'))
                if optional:
                    self.assertIn('devassistant_net', web['drawio']['networks'])
                    self.assertFalse(api['analyzer-worker'].get('ports'))


if __name__ == '__main__':
    unittest.main()
