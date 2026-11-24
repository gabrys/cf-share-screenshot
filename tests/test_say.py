import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cf_share_screenshot as app

FAKE_TOOLS = {
    'wl-copy': '#!/bin/sh\n/bin/cat > "$SAY_OUT/clipboard"\n',
    'notify-send': '#!/bin/sh\nprintf "%s\\n" "$@" > "$SAY_OUT/notification"\n',
    'failing-wl-copy': '#!/bin/sh\nexit 1\n',
}


class SayTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name) / 'out'
        self.bin = Path(tmp.name) / 'bin'
        self.out.mkdir()
        self.bin.mkdir()
        patcher = patch.dict(os.environ, {'PATH': str(self.bin), 'SAY_OUT': str(self.out)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def install(self, name, source=None):
        tool = self.bin / name
        tool.write_text(FAKE_TOOLS[source or name])
        tool.chmod(0o755)

    def call(self, message, to_stderr=False, **kwargs):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            if to_stderr:
                kwargs['file'] = app.sys.stderr
            app.say(message, **kwargs)
        return stdout.getvalue(), stderr.getvalue()

    def test_default_is_stdout_only(self):
        self.install('wl-copy')
        self.install('notify-send')
        stdout, stderr = self.call('hello')
        self.assertEqual((stdout, stderr), ('hello\n', ''))
        self.assertEqual(list(self.out.iterdir()), [])

    def test_stderr(self):
        stdout, stderr = self.call('oops', to_stderr=True)
        self.assertEqual((stdout, stderr), ('', 'oops\n'))

    def test_clipboard_and_notification(self):
        self.install('wl-copy')
        self.install('notify-send')
        stdout, stderr = self.call('screenshot at https://x', notify=True, clipboard=True)
        self.assertEqual((stdout, stderr), ('screenshot at https://x\n', ''))
        self.assertEqual((self.out / 'clipboard').read_text(), 'screenshot at https://x')
        self.assertIn('screenshot at https://x', (self.out / 'notification').read_text())

    def test_missing_tools_are_skipped(self):
        stdout, stderr = self.call('hello', notify=True, clipboard=True)
        self.assertEqual((stdout, stderr), ('hello\n', ''))

    def test_failing_clipboard_is_ignored(self):
        self.install('wl-copy', 'failing-wl-copy')
        stdout, stderr = self.call('hello', clipboard=True)
        self.assertEqual((stdout, stderr), ('hello\n', ''))
