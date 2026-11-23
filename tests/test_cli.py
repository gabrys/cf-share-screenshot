import os
import subprocess
import sys
import tempfile
import unittest

import cf_share_screenshot


def run_cli(*args, env=None):
    return subprocess.run(
        [sys.executable, '-m', 'cf_share_screenshot', *args],
        capture_output=True, text=True, check=False, env=env, timeout=30,
    )


class CommandTests(unittest.TestCase):
    def test_help(self):
        result = run_cli('--help')
        self.assertEqual(result.returncode, 0)
        self.assertIn('usage: cf-share-screenshot [-h] [--version]\n', result.stdout)
        for hidden in ('snap', 'status', '--internal-cmd'):
            self.assertNotIn(hidden, result.stdout)

    def test_positional_arguments_are_rejected(self):
        for command in ('snap', 'serve', 'status'):
            result = run_cli(command)
            self.assertEqual(result.returncode, 2, command)
            self.assertIn('unrecognized arguments', result.stderr)

    def test_internal_cmd_only_accepts_serve(self):
        result = run_cli('--internal-cmd', 'status')
        self.assertEqual(result.returncode, 2)
        self.assertIn('invalid choice', result.stderr)

    def test_version(self):
        result = run_cli('--version')
        self.assertEqual(result.returncode, 0)
        self.assertIn(cf_share_screenshot.__version__, result.stdout)

    def test_reports_missing_tool(self):
        with tempfile.TemporaryDirectory() as empty:
            env = dict(os.environ, PATH=empty)
            result = run_cli(env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn('wl-copy not found', result.stderr)


class TunnelUrlTests(unittest.TestCase):
    def test_finds_quick_tunnel_url(self):
        line = '2026-11-23T12:00:00Z INF |  https://some-random-words.trycloudflare.com  |'
        match = cf_share_screenshot.URL_RE.search(line)
        self.assertEqual(match.group(0), 'https://some-random-words.trycloudflare.com')

    def test_ignores_api_url_in_errors(self):
        line = ('2026-11-23T12:00:00Z ERR Error unmarshaling QuickTunnel response '
                'error="failed to request quick Tunnel: Post '
                '\\"https://api.trycloudflare.com/tunnel\\""')
        self.assertIsNone(cf_share_screenshot.URL_RE.search(line))
