import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cf_share_screenshot as app


class TokenTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / 'app'
        self.file = self.dir / 'bearer.token'
        patcher = patch.object(app, 'TOKEN_FILE', self.file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_created_once_with_private_permissions(self):
        token = app.ensure_token()
        self.assertGreaterEqual(len(token), 40)
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)
        self.assertEqual([p.name for p in self.dir.iterdir()], ['bearer.token'])

    def test_existing_token_is_reused(self):
        first = app.ensure_token()
        self.assertEqual(app.ensure_token(), first)
        self.assertEqual(app.read_token(), first)

    def test_empty_token_file_is_rejected(self):
        self.dir.mkdir()
        self.file.write_text('\n')
        with self.assertRaises(RuntimeError):
            app.ensure_token()
