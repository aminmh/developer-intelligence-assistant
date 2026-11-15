"""Run the real Bash stage owner in disposable app folders with command stubs."""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

SOURCE = Path(__file__).resolve().parents[2]

CLI = '''import os,sys,time
from pathlib import Path
root=Path(os.environ['INSTALL_ROOT']); state=root/'.install'
action,*args=sys.argv[1:]
if action=='lock': os.execvp('bash',['bash',*args])
if action=='logs':
 for line in sys.stdin: print(line,end='',flush=True)
 sys.exit(0)
with (state/'calls').open('a') as log: log.write(' '.join([action,*args])+'\\n')
if action=='valid': sys.exit(0 if (state/('stage-'+args[0])).exists() else 1)
if action=='probe': sys.exit(1 if os.environ.get('INVALIDATE')==args[0] else 0)
if action=='checkpoint': (state/('stage-'+args[0])).touch()
operation = args[1] if action=='compose' else action
if operation==os.environ.get('FAIL_OPERATION'):
 count=state/('attempts-'+operation)
 n=int(count.read_text())+1 if count.exists() else 1
 count.write_text(str(n))
 (state/'retained.part').write_text('completed reusable bytes')
 if os.environ.get('BLOCK'):
  (state/'blocked').touch(); time.sleep(60)
 if n <= int(os.environ.get('FAIL_COUNT','3')): sys.exit(9)
'''


class FlowTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='installer flow ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / '.install'
        target = self.root / 'scripts/installer'
        target.mkdir(parents=True)
        shutil.copy2(SOURCE / 'scripts/installer/install.sh', target / 'install.sh')
        (target / 'cli.py').write_text(CLI)
        (self.root / 'api/telemetry-dev').mkdir(parents=True)
        (self.root / 'api/telemetry-dev/runtime_stack.py').write_text('pass\n')
        (self.state / 'venv/bin').mkdir(parents=True)
        python = self.state / 'venv/bin/python3'
        python.write_text(f'#!/bin/bash\ncase "$1" in -c|-m) exit 0;; esac\nexec "{sys.executable}" "$@"\n')
        python.chmod(0o755)
        binary = self.state / 'bin'
        binary.mkdir()
        for name in ('docker', 'just', 'sleep'):
            path = binary / name
            path.write_text('#!/bin/bash\nexit 0\n')
            path.chmod(0o755)
        self.env = dict(os.environ, PATH=f'{binary}:/usr/bin:/bin', RUNTIME_SSH_AGENT_SOURCE='/tmp/unused')
        self.command = ['bash', str(target / 'install.sh'), 'resume']

    def run_install(self, **env):
        return subprocess.run(self.command, input='yes\n', text=True, capture_output=True,
                              env=dict(self.env, **env), timeout=20)

    def calls(self):
        return (self.state / 'calls').read_text()

    def test_repeated_install_revalidates_caches_and_verifies_services_again(self):
        first = self.run_install()
        self.assertEqual(first.returncode, 0, first.stderr)
        (self.state / 'calls').write_text('')
        second = self.run_install()
        self.assertEqual(second.returncode, 0, second.stderr)
        for stage in ('images', 'build', 'models'):
            self.assertIn('probe ' + stage, self.calls())
        self.assertNotIn('compose api pull', self.calls())
        self.assertIn('configuration', self.calls())
        self.assertIn('verify', self.calls())
        self.assertIn('100% Installation verified', second.stdout)

    def test_failed_pulls_builds_and_models_pause_then_resume_retained_work(self):
        for operation, stage in (('pull', 'images'), ('build', 'build'), ('models', 'models')):
            with self.subTest(operation=operation):
                for path in self.state.glob('stage-*'): path.unlink()
                result = self.run_install(FAIL_OPERATION=operation)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((self.state / ('attempts-' + operation)).read_text(), '3')
                self.assertIn('Resume from this app folder: bash install.sh', result.stderr)
                self.assertFalse((self.state / ('stage-' + stage)).exists())
                result = self.run_install()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((self.state / 'retained.part').read_text(), 'completed reusable bytes')

    def test_invalid_probe_reexecutes_completed_stage(self):
        self.assertEqual(self.run_install().returncode, 0)
        (self.state / 'calls').write_text('')
        result = self.run_install(INVALIDATE='build')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('compose api build', self.calls())
        self.assertNotIn('compose api pull', self.calls())

    def test_declined_review_does_not_mutate_services(self):
        result = subprocess.run(self.command, input='no\n', text=True, capture_output=True, env=self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('configuration', self.calls())
        self.assertNotIn('compose', self.calls())

    def test_termination_preserves_checkpoints_and_next_process_resumes(self):
        process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True,
                                   env=dict(self.env, FAIL_OPERATION='models', BLOCK='1'))
        process.stdin.write('yes\n'); process.stdin.flush()
        try:
            for _ in range(100):
                if (self.state / 'blocked').exists(): break
                time.sleep(0.05)
            self.assertTrue((self.state / 'blocked').exists())
            os.killpg(process.pid, signal.SIGTERM)
            process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0)
            self.assertTrue((self.state / 'stage-build').exists())
            result = self.run_install()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((self.state / 'retained.part').exists())
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()


if __name__ == '__main__':
    unittest.main()
