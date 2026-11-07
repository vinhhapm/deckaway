import importlib.util
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from support import ROOT

spec = importlib.util.spec_from_file_location('mac_network', ROOT / 'macos/network.py')
network = importlib.util.module_from_spec(spec)
spec.loader.exec_module(network)

STRONG = 'a-strong-generated-password'


class NetworkSwitchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.config, self.plist = network.paths(self.home)
        self.write_settings(STRONG, '100.64.0.5')
        self.plist.parent.mkdir(parents=True)
        self.plist.write_bytes(plistlib.dumps({'EnvironmentVariables': {'BIND_HOST': '100.64.0.5'}}))
        self.steps = []
        self.funnel_is_on = False
        for name, replacement in (('tailscale', self.fake_tailscale), ('tailscale_ipv4', Mock(return_value='100.64.0.5')),
                                  ('restart_panel', lambda plist: self.steps.append('restart')),
                                  ('wait_for_panel', Mock(return_value='url'))):
            patcher = patch.object(network, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_settings(self, password, host):
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(json.dumps({'BIND_HOST': host, 'BIND_HOST_AUTO': '1', 'BIND_PORT': '8790',
                                           'PANEL_PASSWORD': password}))

    def fake_tailscale(self, *args, check=True):
        if args[:2] == ('funnel', 'status'):
            return json.dumps({'AllowFunnel': {'mac.example.ts.net:443': True}} if self.funnel_is_on else {})
        if args[0] == 'status':
            return json.dumps({'Self': {'DNSName': 'mac.example.ts.net.'}})
        self.steps.append(' '.join(args))
        return ''

    def saved(self):
        return json.loads(self.config.read_text()), plistlib.loads(self.plist.read_bytes())['EnvironmentVariables']

    def test_internet_moves_the_panel_to_loopback_before_opening_funnel(self):
        network.use_internet(self.home, lambda: True)
        self.assertEqual(self.steps, ['restart', 'funnel --bg 8790'])
        settings, environment = self.saved()
        self.assertEqual((settings['BIND_HOST'], environment['BIND_HOST']), ('127.0.0.1', '127.0.0.1'))
        self.assertEqual(settings['BIND_HOST_AUTO'], '0')  # A rerun of the installer must not move it to Tailscale.
        self.assertEqual(self.plist.stat().st_mode & 0o777, 0o600)  # It holds the panel password.

    def test_a_weak_password_never_goes_public(self):
        self.write_settings('short', '100.64.0.5')
        with self.assertRaisesRegex(ValueError, 'too weak'):
            network.use_internet(self.home, lambda: True)
        self.assertEqual(self.steps, [])

    def test_refused_confirmation_changes_nothing(self):
        before = self.config.read_text(), self.plist.read_bytes()
        with self.assertRaisesRegex(ValueError, 'Cancelled'):
            network.use_internet(self.home, lambda: False)
        self.assertEqual(self.steps, [])
        self.assertEqual((self.config.read_text(), self.plist.read_bytes()), before)

    def test_internet_needs_a_connected_tailscale_and_changes_nothing_without_it(self):
        network.tailscale_ipv4.return_value = None
        with self.assertRaisesRegex(ValueError, 'not connected'):
            network.use_internet(self.home, lambda: True)
        self.assertEqual(self.steps, [])

    def test_failed_funnel_leaves_the_panel_on_loopback_only(self):
        def refuse(*args, check=True):
            raise ValueError('funnel refused')
        with patch.object(network, 'tailscale', refuse), self.assertRaisesRegex(ValueError, 'funnel refused'):
            network.use_internet(self.home, lambda: True)
        self.assertEqual(self.saved()[1]['BIND_HOST'], '127.0.0.1')

    def test_tailscale_closes_funnel_before_moving_the_panel(self):
        self.write_settings(STRONG, '127.0.0.1')
        self.plist.write_bytes(plistlib.dumps({'EnvironmentVariables': {'BIND_HOST': '127.0.0.1'}}))
        self.funnel_is_on = True
        network.use_tailscale(self.home)
        self.assertEqual(self.steps, ['funnel --https=443 off', 'restart'])
        settings, environment = self.saved()
        self.assertEqual((settings['BIND_HOST'], environment['BIND_HOST']), ('100.64.0.5', '100.64.0.5'))
        self.assertEqual(settings['BIND_HOST_AUTO'], '1')

    def test_tailscale_mode_needs_a_connection(self):
        network.tailscale_ipv4.return_value = None
        with self.assertRaisesRegex(ValueError, 'not connected'):
            network.use_tailscale(self.home)
        self.assertEqual(self.steps, [])

    def test_same_address_does_not_restart_the_panel(self):
        network.use_tailscale(self.home)
        self.assertEqual(self.steps, [])


class CloudflareTunnelTests(NetworkSwitchTests):
    URL = 'https://calm-river-word.trycloudflare.com'

    def setUp(self):
        super().setUp()
        self.answers = [self.URL]
        self.tunnel_plist, logs = network.tunnel_paths(self.home)
        self.log = logs / (network.TUNNEL + '.log')
        for name, replacement in (('run', self.fake_run), ('cloudflared', Mock(return_value='/bin/cloudflared'))):
            patcher = patch.object(network, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(network.time, 'sleep')
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_run(self, *args, check=True):
        if args[1] == 'bootstrap':
            self.steps.append('tunnel')
            self.log.write_text(''.join(f'INF |  {url}  |\n' for url in self.answers))
        elif args[1] == 'bootout':
            self.steps.append('tunnel stop')
        return SimpleNamespace(returncode=1 if args[1] == 'print' else 0)

    def test_moves_the_panel_to_loopback_before_the_tunnel_opens(self):
        network.use_cloudflare(self.home, lambda: True)
        self.assertEqual(self.steps, ['restart', 'tunnel'])
        settings, environment = self.saved()
        self.assertEqual((settings['BIND_HOST'], environment['BIND_HOST'], settings['BIND_HOST_AUTO']), ('127.0.0.1', '127.0.0.1', '0'))
        arguments = plistlib.loads(self.tunnel_plist.read_bytes())['ProgramArguments']
        self.assertEqual(arguments[-2:], ['--url', 'http://127.0.0.1:8790'])
        self.assertEqual(network.tunnel_url(self.home), self.URL)

    def test_a_weak_password_opens_no_tunnel(self):
        self.write_settings('short', '100.64.0.5')
        with self.assertRaisesRegex(ValueError, 'too weak'):
            network.use_cloudflare(self.home, lambda: True)
        self.assertEqual(self.steps, [])
        self.assertFalse(self.tunnel_plist.exists())

    def test_refused_confirmation_opens_no_tunnel(self):
        with self.assertRaisesRegex(ValueError, 'Cancelled'):
            network.use_cloudflare(self.home, lambda: False)
        self.assertEqual(self.steps, [])

    def test_no_address_stops_the_tunnel_and_stays_on_loopback(self):
        self.answers = []
        with self.assertRaisesRegex(RuntimeError, 'no address'):
            network.use_cloudflare(self.home, lambda: True)
        self.assertFalse(self.tunnel_plist.exists())
        self.assertEqual(self.saved()[1]['BIND_HOST'], '127.0.0.1')

    def test_the_newest_address_is_the_one_shown(self):
        self.answers = ['https://old-one.trycloudflare.com', self.URL]
        network.use_cloudflare(self.home, lambda: True)
        self.assertEqual(network.tunnel_url(self.home), self.URL)

    def test_status_never_reads_private_while_the_tunnel_is_open(self):
        network.use_cloudflare(self.home, lambda: True)
        with patch('builtins.print') as output:
            network.status(self.home)
        self.assertIn('Mode: cloudflare', output.call_args[0][0])
        self.assertIn(self.URL, output.call_args[0][0])

    def test_local_closes_every_public_door(self):
        network.use_cloudflare(self.home, lambda: True)
        self.funnel_is_on = True
        self.steps.clear()
        network.use_local(self.home)
        self.assertEqual(self.steps, ['funnel --https=443 off', 'tunnel stop'])
        self.assertFalse(self.tunnel_plist.exists())

    def test_tailscale_closes_the_tunnel_before_moving_the_panel(self):
        network.use_cloudflare(self.home, lambda: True)
        self.steps.clear()
        network.use_tailscale(self.home)
        self.assertEqual(self.steps, ['tunnel stop', 'restart'])
        self.assertFalse(self.tunnel_plist.exists())

    def test_a_tunnel_that_will_not_stop_is_reported_and_kept_on_record(self):
        network.use_cloudflare(self.home, lambda: True)
        with patch.object(network, 'run', return_value=SimpleNamespace(returncode=0)), \
                self.assertRaisesRegex(RuntimeError, 'still running'):
            network.use_local(self.home)
        self.assertTrue(self.tunnel_plist.exists())


class TailscaleCommandTests(unittest.TestCase):
    def test_a_stalled_command_reports_what_it_printed(self):
        stalled = subprocess.TimeoutExpired(['tailscale'], 20, output=b'To enable, visit: https://login.tailscale.com/f/funnel')
        with patch.object(network.shutil, 'which', return_value='/usr/local/bin/tailscale'), \
                patch.object(network.subprocess, 'run', side_effect=stalled), \
                self.assertRaisesRegex(ValueError, 'login.tailscale.com/f/funnel'):
            network.tailscale('funnel', '--bg', '8790')


if __name__ == '__main__':
    unittest.main()
