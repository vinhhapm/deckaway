#!/usr/bin/env python3
"""Choose how DeckAway on this Mac is reached: this Mac only, over Tailscale, or publicly over HTTPS through
Tailscale Funnel or a Cloudflare quick tunnel.

The panel is a web terminal, so it is never published on plain HTTP: in a public mode it listens on loopback
only and the tunnel is the single public door, with HTTPS in front of it. One mode at a time: each switch closes
the other doors first. Sessions keep running across a switch, because tmux is a separate server.
"""
import argparse
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from install import LABELS, atomic_write, launch_agent, run, tailscale_ipv4, wait_for_panel

PANEL = LABELS[1]
TUNNEL = 'com.agent-deck.tunnel'
TUNNEL_URL = re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com')
TUNNEL_WAIT = 60  # Half-second looks for the address cloudflared prints.
LOOPBACK = '127.0.0.1'
MIN_PUBLIC_PASSWORD = 16
APP = Path('/Applications/Tailscale.app/Contents/MacOS/Tailscale')


def tailscale(*args, check=True):
    binary = shutil.which('tailscale') or (str(APP) if APP.is_file() else None)
    if not binary:
        raise ValueError('Tailscale is not installed')
    try:
        result = subprocess.run([binary, *args], input='', capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired as error:
        # A tailnet without Funnel prints the page that enables it, then waits for someone to open it.
        said = error.stdout or b''
        said = said.decode('utf-8', 'replace') if isinstance(said, bytes) else said
        raise ValueError(f'Tailscale did not finish `{args[0]}` within 20 s. {said.strip()[-400:]}'.strip()) from None
    if check and result.returncode:
        raise ValueError(f'`tailscale {args[0]}` failed: {result.stderr.strip()[-400:]}')
    return result.stdout


def funnel_on():
    if not shutil.which('tailscale') and not APP.is_file():
        return False
    try:
        config = json.loads(tailscale('funnel', 'status', '--json') or '{}')
    except ValueError as error:
        if isinstance(error, json.JSONDecodeError):
            raise ValueError('Could not read the Funnel status from Tailscale') from None
        raise
    return any((config.get('AllowFunnel') or {}).values())


def close_funnel():
    if funnel_on():
        tailscale('funnel', '--https=443', 'off')


def cloudflared():
    binary = shutil.which('cloudflared') or '/opt/homebrew/bin/cloudflared'
    if not Path(binary).is_file():
        raise ValueError('cloudflared is not installed: brew install cloudflared')
    return binary


def tunnel_paths(home):
    return home / 'Library/LaunchAgents' / (TUNNEL + '.plist'), home / 'Library/Logs/Agent Deck'


def tunnel_url(home):
    """The address of the running tunnel: the newest in its log, since launchd restarts get a new one."""
    log = tunnel_paths(home)[1] / (TUNNEL + '.log')
    found = TUNNEL_URL.findall(log.read_text(errors='replace')) if log.is_file() else []
    return found[-1] if found else None


def stop_tunnel(home):
    plist = tunnel_paths(home)[0]
    if not plist.exists():
        return
    service = f'gui/{os.getuid()}/{TUNNEL}'
    run('launchctl', 'bootout', service, check=False)
    if run('launchctl', 'print', service, check=False).returncode == 0:
        raise RuntimeError(f'The tunnel is still running; stop it with: launchctl bootout {service}')
    plist.unlink()


def start_tunnel(home, port):
    plist, logs = tunnel_paths(home)
    atomic_write(logs / (TUNNEL + '.log'), b'')  # An old address must not be read as the new one.
    definition = launch_agent(TUNNEL, [cloudflared(), 'tunnel', '--no-autoupdate', '--url', f'http://{LOOPBACK}:{port}'],
                              {'HOME': str(home), 'PATH': '/usr/bin:/bin'}, logs)
    atomic_write(plist, plistlib.dumps(definition))
    run('launchctl', 'bootstrap', f'gui/{os.getuid()}', plist)
    for _ in range(TUNNEL_WAIT):
        url = tunnel_url(home)
        if url:
            return url
        time.sleep(.5)
    stop_tunnel(home)
    raise RuntimeError(f'cloudflared gave no address in time; see {logs / (TUNNEL + ".log")}')


def public_url():
    name = (json.loads(tailscale('status', '--json')).get('Self') or {}).get('DNSName', '').rstrip('.')
    return f'https://{name}' if name else None


def paths(home):
    return home / '.config/cc-panel/macos.json', home / 'Library/LaunchAgents' / (PANEL + '.plist')


def restart_panel(plist):
    domain = f'gui/{os.getuid()}'
    run('launchctl', 'bootout', f'{domain}/{PANEL}', check=False)
    for attempt in range(6):  # launchd may need a moment to finish stopping the old process.
        if run('launchctl', 'bootstrap', domain, plist, check=False).returncode == 0:
            return
        time.sleep(.5)
    raise RuntimeError(f'launchd did not start {PANEL} again; check: launchctl bootstrap {domain} {plist}')


def bind(home, host, automatic):
    """Saves the address where a rerun of the installer also finds it, then moves the panel there."""
    config, plist = paths(home)
    settings = json.loads(config.read_text())
    definition = plistlib.loads(plist.read_bytes())
    changed = definition['EnvironmentVariables'].get('BIND_HOST') != host
    settings.update(BIND_HOST=host, BIND_HOST_AUTO='1' if automatic else '0')
    atomic_write(config, (json.dumps(settings, indent=2) + '\n').encode())
    if changed:
        definition['EnvironmentVariables']['BIND_HOST'] = host
        atomic_write(plist, plistlib.dumps(definition))  # Private: it holds the panel password.
        restart_panel(plist)
        wait_for_panel(settings, home / 'Library/Logs/Agent Deck')
    return settings


def use_tailscale(home):
    address = tailscale_ipv4()
    if not address:
        raise ValueError('Tailscale is not connected: open the app, sign in, then run this again')
    close_funnel()  # First: whatever fails below, the panel is no longer public.
    stop_tunnel(home)
    settings = bind(home, address, True)
    print(f"DeckAway is on Tailscale only: http://{address}:{settings['BIND_PORT']}")


def use_local(home):
    close_funnel()
    stop_tunnel(home)
    settings = bind(home, LOOPBACK, False)
    print(f"DeckAway is on this Mac only: http://{LOOPBACK}:{settings['BIND_PORT']}")


def require_strong_password(home):
    settings = json.loads(paths(home)[0].read_text())
    if len(settings.get('PANEL_PASSWORD', '')) < MIN_PUBLIC_PASSWORD:
        raise ValueError(f'The panel password is shorter than {MIN_PUBLIC_PASSWORD} characters: too weak for the internet')


def use_cloudflare(home, confirm):
    require_strong_password(home)
    cloudflared()
    if not confirm():
        raise ValueError('Cancelled; nothing was changed')
    close_funnel()
    stop_tunnel(home)
    settings = bind(home, LOOPBACK, False)  # First: the tunnel must never find the panel on another address.
    url = start_tunnel(home, settings['BIND_PORT'])
    print(f'DeckAway is public: {url}')
    print('Anyone can reach its sign-in page, and signing in gives a shell. Cloudflare can read this traffic.')
    print('The address changes whenever the tunnel restarts; `deckaway-network status` shows the current one.')


def use_internet(home, confirm):
    require_strong_password(home)
    if not tailscale_ipv4():
        raise ValueError('Tailscale is not connected: open the app, sign in, then run this again')
    if not confirm():
        raise ValueError('Cancelled; nothing was changed')
    stop_tunnel(home)
    settings = bind(home, LOOPBACK, False)  # First: Funnel must never find the panel on another address.
    tailscale('funnel', '--bg', str(settings['BIND_PORT']))
    print(f'DeckAway is public: {public_url() or "see `tailscale funnel status`"}')
    print('Anyone can reach its sign-in page, and signing in gives a shell. Tailscale access is off until you switch back.')


def status(home):
    settings = json.loads(paths(home)[0].read_text())
    host, port = settings['BIND_HOST'], settings['BIND_PORT']
    if tunnel_paths(home)[0].exists():  # Checked first: this must never read as private while it is public.
        print(f'Mode: cloudflare ({tunnel_url(home) or "address not known yet"} -> http://{host}:{port})')
    elif funnel_on():
        print(f'Mode: internet ({public_url() or "see `tailscale funnel status`"} -> http://{host}:{port})')
    elif host == LOOPBACK:
        print(f'Mode: this Mac only (http://{host}:{port})')
    else:
        print(f'Mode: tailscale (http://{host}:{port})')


def ask(word='internet'):
    print('This publishes a web terminal on the internet. Anyone who gets the password has a shell as your user, '
          'with your SSH keys and agent logins.')
    try:
        return input(f'Type "{word}" to continue: ').strip() == word
    except EOFError:
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(prog='deckaway-network', description='Switch how DeckAway on this Mac is reached.')
    parser.add_argument('mode', choices=('local', 'tailscale', 'internet', 'cloudflare', 'status'))
    parser.add_argument('--yes', action='store_true', help='Do not ask before going public')
    args = parser.parse_args(argv)
    if sys.platform != 'darwin' or os.geteuid() == 0:
        parser.error('Run as your logged-in macOS user, without sudo')
    try:
        if args.mode == 'local':
            use_local(Path.home())
        elif args.mode == 'tailscale':
            use_tailscale(Path.home())
        elif args.mode == 'cloudflare':
            use_cloudflare(Path.home(), (lambda: True) if args.yes else (lambda: ask('cloudflare')))
        elif args.mode == 'internet':
            use_internet(Path.home(), (lambda: True) if args.yes else ask)
        else:
            status(Path.home())
    except (ValueError, RuntimeError, OSError) as error:
        print(f'deckaway-network: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
