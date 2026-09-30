"""Exercise lock lifetime without installing dependencies or touching the DB."""
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HELPER = Path(__file__).with_name('dev_lock.py')


class DevLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)

    def launch(self, command):
        return subprocess.Popen(
            [sys.executable, str(HELPER), str(self.state), '/bin/bash', '-c', command],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def test_stale_and_empty_legacy_lock(self):
        legacy = self.state / 'lock'
        legacy.mkdir()
        for value in (None, 'invalid', '2147483647'):
            if value is not None:
                (legacy / 'pid').write_text(value)
            proc = self.launch('echo started')
            out, err = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0, err)
            self.assertEqual(out.strip(), 'started')

    def test_live_legacy_owner_is_preserved(self):
        legacy = self.state / 'lock'
        legacy.mkdir()
        (legacy / 'pid').write_text(str(os.getpid()))
        proc = self.launch('echo should-not-run')
        out, err = proc.communicate(timeout=5)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(out, '')
        self.assertIn('已有启动任务', err)

    def test_script_entry_recovers_legacy_lock_before_preflight(self):
        root = self.state / 'project'
        (root / 'tools').mkdir(parents=True)
        shutil.copy(HELPER, root / 'tools/dev_lock.py')
        shutil.copy(HELPER.parent.parent / 'dev.sh', root / 'dev.sh')
        legacy = root / '.dev/lock'
        legacy.mkdir(parents=True)
        (legacy / 'pid').write_text('2147483647')
        binaries = root / 'bin'
        binaries.mkdir()
        for name in ('uv', 'node', 'npm', 'curl'):
            stub = binaries / name
            stub.write_text('#!/bin/sh\necho preflight-reached >&2\nexit 42\n')
            stub.chmod(0o755)
        env = dict(os.environ, PATH=f'{binaries}:{os.environ["PATH"]}')
        env.pop('WORKFLOW_DEV_LOCKED', None)
        for _ in range(2):
            result = subprocess.run(
                ['/bin/bash', str(root / 'dev.sh'), '--skip-install'],
                env=env, capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, 42, result.stderr)
            self.assertIn('preflight-reached', result.stderr)

    def test_duplicate_rejected_and_sigkill_releases_lock(self):
        first = self.launch('echo ready; exec sleep 60')
        try:
            self.assertEqual(first.stdout.readline().strip(), 'ready')
            second = self.launch('echo should-not-run')
            out, err = second.communicate(timeout=5)
            self.assertNotEqual(second.returncode, 0)
            self.assertEqual(out, '')
            self.assertIn('已有启动任务', err)
        finally:
            first.kill()
            first.communicate(timeout=5)
        third = self.launch('echo restarted')
        out, err = third.communicate(timeout=5)
        self.assertEqual(third.returncode, 0, err)
        self.assertEqual(out.strip(), 'restarted')

    def test_orphan_service_keeps_lock(self):
        first = self.launch('sleep 60 & echo $!; wait')
        child = int(first.stdout.readline())
        try:
            first.kill()
            first.wait(timeout=5)
            second = self.launch('echo should-not-run')
            out, err = second.communicate(timeout=5)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn('已有启动任务', err)
        finally:
            os.kill(child, signal.SIGTERM)
            first.communicate(timeout=5)
        for _ in range(50):
            third = self.launch('echo restarted')
            out, err = third.communicate(timeout=5)
            if third.returncode == 0:
                break
            time.sleep(0.02)
        self.assertEqual(third.returncode, 0, err)


if __name__ == '__main__':
    unittest.main()
