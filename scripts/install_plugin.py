#!/usr/bin/env python3
"""Install the Makara client and connect it to a local knowledge service."""
import argparse
import fcntl
import json
import os
import re
import secrets
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from build_plugin import build

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from makara.capture import project_scope, resolve_target
from makara.client import Client
from makara.core import Error, atomic, digest
from makara.memory import Honcho


def write_json(path, value):
    atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def mcp_identity(home, config, provider, scope):
    handles = {'openai': 'agent-a', 'anthropic': 'agent-b'}
    handle = handles.get(provider, 'mcp-' + provider)
    token_file = home / (handle + '.token')
    if not token_file.exists():
        if handle in handles.values() or any(p['id'] == handle for p in config['principals'].values()):
            raise SystemExit('Missing token for an existing identity; restore it before continuing')
        atomic(token_file, secrets.token_urlsafe(32))
    key = digest(token_file.read_text().strip())
    actors = config['principals']
    if key not in actors and handle not in handles.values():
        actors[key] = dict(id=handle, subject='self', provider=provider, read=[], write=[], approve=[], delete=[])
    principal = actors.get(key, {})
    if principal.get('id') != handle or principal.get('provider') != provider:
        raise SystemExit('Token and configured provider do not match')
    for action in ('read', 'write'):
        if scope not in principal[action]:
            principal[action].append(scope)
    if provider not in config['providers']:
        config['providers'].append(provider)
    return token_file


def prepare(home, args):
    root = args.project_dir.resolve() if args.project_dir else None
    if root and not root.is_dir():
        raise SystemExit('Project directory does not exist')
    if args.honcho_url and not root:
        raise SystemExit('--honcho-url requires --project-dir to select the data to send')
    if (args.honcho_token_file or args.allow_remote) and not args.honcho_url:
        raise SystemExit('Specify --honcho-url for these Honcho options')
    config_path = home / 'config.json'
    fresh = not config_path.exists()
    scope = project_scope(str(root)) if root else 'projects/makara'
    vault = args.vault.resolve() if args.vault else Path.home() / 'MakaraVault'
    if fresh:
        subprocess.run([sys.executable, '-m', 'makara.cli', '--home', str(home),
                        'init', '--vault', str(vault), '--project', scope.split('/')[1]],
                       check=True, cwd=REPO)
    config = json.loads(config_path.read_text())
    if args.vault and Path(config['vault']) != vault:
        raise SystemExit('Existing vault differs from --vault; use its configured path')
    before = json.dumps(config, sort_keys=True)
    policy_file = home / 'capture.json'
    policy = json.loads(policy_file.read_text()) if policy_file.exists() else {
        'require_plugin': 'makara@makara-local', 'projects': {}}
    if fresh:
        for principal in config['principals'].values():
            providers = {'agent-a': 'openai', 'agent-b': 'anthropic'}
            if principal['id'] in providers:
                principal['provider'] = providers[principal['id']]
        write_json(config_path, config)
    if root:
        projects = policy.setdefault('projects', {})
        if any(not item.get('enabled') and item.get('include_children') and Path(path).resolve() in root.parents for path, item in projects.items()):
            raise SystemExit('Parent project is explicitly disabled in capture.json')
        entry = projects.get(str(root))
        if entry and not entry.get('enabled'):
            raise SystemExit('Project is explicitly disabled in capture.json')
        # Respect stored project aliases, exclusions and all-project registration.
        effective = dict(policy, require_plugin=None)
        if any(root == Path(p).expanduser().resolve() or Path(p).expanduser().resolve() in root.parents
               for p in policy.get('excluded_paths', [])):
            raise SystemExit('Project is excluded in capture.json')
        target = resolve_target(effective, str(root)) or {}
        scope = target.get('scope') or project_scope(target.get('project_root', str(root)))
        providers = list(target.get('allowed_providers', ['openai']))
        if args.honcho_url:
            candidate = dict(config.get('honcho', {}))
            address = args.honcho_url.rstrip('/').removesuffix('/v3')
            if candidate.get('base_url', '').rstrip('/') != address:
                if candidate.get('enabled') or any((home / 'memory').glob('*.json')):
                    raise SystemExit('An existing Honcho endpoint is configured; switching requires a memory migration')
                candidate.pop('token_file', None)
                candidate.pop('local_hosts', None)
                candidate['allow_remote'] = False
            if not candidate.get('enabled'):
                candidate['allowed_scopes'] = []
            candidate.update(base_url=address, enabled=True)
            if args.allow_remote:
                candidate['allow_remote'] = True
            if args.honcho_token_file:
                token = args.honcho_token_file.expanduser().resolve(strict=True)
                if not token.is_file() or token.is_relative_to(Path(config['vault']).resolve()):
                    raise SystemExit('Use a private token file outside the vault')
                candidate['token_file'] = str(token)
            api = Honcho(candidate)
            listing = api.call('POST', '/workspaces/list', {'filters': {'id': 'makara-install-check'}})
            if not isinstance(listing, dict) or not isinstance(listing.get('items'), list):
                raise SystemExit('Expected a Honcho v3 workspace listing; check the API URL')
            scopes = candidate.setdefault('allowed_scopes', [])
            if scope not in scopes:
                scopes.append(scope)
            config['honcho'] = candidate
            if 'honcho' not in providers:
                providers.append('honcho')
        for principal in config['principals'].values():
            actions = {'owner': ['read', 'write', 'approve', 'delete']}
            if args.client in ('auto', 'all', 'codex', 'agy'):
                actions['agent-a'] = ['read', 'write']
            for action in actions.get(principal['id'], []):
                if scope not in principal[action]:
                    principal[action].append(scope)
        if args.client in ('auto', 'all', 'codex', 'agy'):
            projects[str(root)] = dict(target, enabled=True, scope=scope, include_children=target.get('include_children', True),
                                       allowed_providers=providers)
            projects[str(root)].pop('project_root', None)
    token_file = mcp_identity(home, config, args.provider, scope) if args.client == 'mcp' else home / 'agent-a.token'
    changed = before != json.dumps(config, sort_keys=True)
    if changed:
        write_json(config_path, config)
    if (root and args.client in ('auto', 'all', 'codex', 'agy')) or not policy_file.exists():
        write_json(policy_file, policy)
    return changed, config, token_file, scope


def start_service(home, config, changed):
    client = Client(home, home / 'agent-a.token')
    try:
        status = client.request('status', {})
    except Error as error:
        if error.status != 503:
            raise
        status = None
    if status and changed:
        pidfile = home / 'server.pid'
        pid = int(pidfile.read_text()) if pidfile.exists() else 0
        cmdline = Path(f'/proc/{pid}/cmdline')
        expected = ['-m', 'makara.cli', '--home', str(home), 'serve', '--port', '18765']
        if not cmdline.exists() or cmdline.read_bytes().decode().strip('\0').split('\0')[1:] != expected:
            raise SystemExit('Configuration saved. Restart your managed Makara service, then rerun the installer.')
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            if not cmdline.exists() or not cmdline.read_bytes():
                break
            time.sleep(0.1)
        else:
            raise SystemExit('Timed out waiting for managed Makara process to stop')
        status = None
    if not status:
        if (home / 'compose.json').is_file():
            raise SystemExit('Managed Makara is offline. Start its Docker Compose service, then rerun the installer.')
        subprocess.run([sys.executable, '-m', 'makara.cli', '--home', str(home), 'start'],
                       cwd=home / 'runtime', check=True, stdout=subprocess.DEVNULL)
    status = client.request('status', {})
    if not status.get('ready') or status['memory_enabled'] != config['honcho']['enabled']:
        raise SystemExit('Service configuration differs; restart your Makara service before using the plugin')
    return status


def _install_skill(target_dir):
    target_dir = Path(target_dir).expanduser()
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO / 'plugins/makara/skills/makara', target_dir, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__'))


def _mcp_command(home, token_file, windows_app=False):
    launcher = home / 'runtime/scripts/run_mcp.py'
    launcher.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO / 'scripts/run_mcp.py', launcher)
    command = [sys.executable, str(launcher), '--home', str(home), '--token-file', str(token_file)]
    if windows_app:
        command = ['wsl.exe', '--distribution', os.environ['WSL_DISTRO_NAME'], '--user',
                   Path.home().name, '--exec', *command]
    return {'command': command[0], 'args': command[1:]}


def _write_mcp_snippet(home, provider, server_cfg):
    snippet = {'mcpServers': {'makara': server_cfg}}
    output = home / f'mcp-{provider}.json'
    write_json(output, snippet)
    return output, snippet


def _install_codex(home, destination, codex_bin, windows_app):
    if windows_app:
        distro = os.environ.get('WSL_DISTRO_NAME')
        if not distro:
            raise SystemExit('--windows-app runs inside the Windows app’s WSL distribution')
        profile = subprocess.check_output(['powershell.exe', '-NoProfile', '-Command',
                   "[Environment]::GetFolderPath('UserProfile')"], text=True).strip()
        windows_home = subprocess.check_output(['wslpath', '-u', profile], text=True).strip()
        subprocess.run([sys.executable, str(REPO / 'scripts/install_desktop_plugin.py'),
                        '--windows-home', windows_home, '--distro', distro,
                        '--codex-exe', codex_bin, '--wsl-codex-exe', codex_bin,
                        '--service-home', str(home)], check=True)
        return
    subprocess.run([codex_bin, 'plugin', 'marketplace', 'add', str(destination)], check=True)
    subprocess.run([codex_bin, 'plugin', 'add', 'makara@makara-local', '--json'], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client', choices=['auto', 'all', 'codex', 'agy', 'claude', 'opencode', 'gemini', 'mcp'], default='auto',
                        help='Target agent client (default: auto-detect installed agents and universal skills)')
    parser.add_argument('--provider', help='Model provider allowed to read this MCP connection')
    parser.add_argument('--project-dir', type=Path, help='Select a project scope (also opts into automatic capture where supported)')
    parser.add_argument('--vault', type=Path, help='Markdown directory on first installation')
    parser.add_argument('--honcho-url', help='Honcho API base URL, without /v3')
    parser.add_argument('--honcho-token-file', type=Path, help='Path to bearer token on the service machine')
    parser.add_argument('--allow-remote', action='store_true', help='Allow sending selected project data to a remote HTTPS Honcho')
    parser.add_argument('--windows-app', action='store_true', help='Install into the Windows desktop app using WSL')
    args = parser.parse_args()
    if args.provider and (not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', args.provider) or args.provider in {'local', 'owner', 'honcho'}):
        parser.error('--provider must be a valid model provider name, e.g. anthropic, google, openai')
    if args.client == 'mcp' and not args.provider:
        parser.error('--client mcp requires --provider with the actual model provider, e.g. anthropic')
    tools = {name: shutil.which(name) for name in ('codex', 'agy', 'claude', 'opencode', 'gemini')}
    if args.client in ('codex', 'agy', 'claude', 'opencode') and not tools[args.client]:
        parser.error(f'{args.client} CLI not found on PATH; use --client auto or --client mcp --provider PROVIDER')
    home = Path(os.environ.get('MAKARA_HOME', Path.home() / '.local/share/makara')).expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (home / '.install.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        changed, config, token_file, scope = prepare(home, args)
        runtime = home / 'runtime/makara'
        shutil.copytree(REPO / 'makara', runtime, dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__'))
        destination = home / 'plugin-marketplace'
        build(destination, archive=False)
        status = start_service(home, config, changed)

        targets = [args.client] if args.client not in ('auto', 'all') else [k for k, v in tools.items() if v]
        installed = []
        if not targets:
            provider = args.provider or 'openai'
            tf = mcp_identity(home, config, provider, scope)
            write_json(home / 'config.json', config)
            _install_skill('~/.agents/skills/makara')
            output, snippet = _write_mcp_snippet(home, provider, _mcp_command(home, tf, args.windows_app))
            print(f'Universal skill installed to ~/.agents/skills/makara')
            print(f'MCP connection settings: {output}')
            print(json.dumps(snippet, ensure_ascii=False, indent=2))
            installed.append('universal-mcp')
        for target in targets:
            if target == 'mcp':
                identity = Client(home, token_file).request('status', {})
                if identity.get('provider') != args.provider:
                    raise SystemExit('Restart Makara to load the selected provider identity')
                output, snippet = _write_mcp_snippet(home, args.provider, _mcp_command(home, token_file, args.windows_app))
                print(f'MCP connection settings: {output}')
                print(json.dumps(snippet, ensure_ascii=False, indent=2))
                installed.append('mcp')
            elif target == 'codex':
                _install_codex(home, destination, tools['codex'], args.windows_app)
                installed.append('codex')
            elif target == 'agy':
                subprocess.run([tools['agy'], 'plugin', 'install', str(destination / 'plugins/makara')], check=True)
                subprocess.run([tools['agy'], 'plugin', 'enable', 'makara'], check=False)
                installed.append('agy')
            elif target == 'claude':
                provider = args.provider or 'anthropic'
                tf = mcp_identity(home, config, provider, scope)
                write_json(home / 'config.json', config)
                cfg = _mcp_command(home, tf, args.windows_app)
                _install_skill('~/.claude/skills/makara')
                _write_mcp_snippet(home, provider, cfg)
                subprocess.run([tools['claude'], 'mcp', 'add-json', 'makara', json.dumps(cfg)], check=False)
                installed.append('claude')
            elif target == 'opencode':
                provider = args.provider or 'openai'
                tf = mcp_identity(home, config, provider, scope)
                write_json(home / 'config.json', config)
                cfg = _mcp_command(home, tf, args.windows_app)
                _install_skill('~/.config/opencode/skills/makara')
                _write_mcp_snippet(home, provider, cfg)
                subprocess.run([tools['opencode'], 'mcp', 'add', 'makara', '--', cfg['command'], *cfg['args']], check=False)
                installed.append('opencode')
            elif target == 'gemini':
                provider = args.provider or 'google'
                tf = mcp_identity(home, config, provider, scope)
                write_json(home / 'config.json', config)
                cfg = _mcp_command(home, tf, args.windows_app)
                _install_skill('~/.gemini/skills/makara')
                output, _ = _write_mcp_snippet(home, provider, cfg)
                print(f'Gemini skill installed to ~/.gemini/skills/makara; MCP config: {output}')
                installed.append('gemini')
        if args.client in ('auto', 'all'):
            _install_skill('~/.agents/skills/makara')
        print(f"Makara ready ({', '.join(installed)}). Vault: {config['vault']}. Honcho enabled: {status['memory_enabled']}.")


if __name__ == '__main__':
    try:
        main()
    except (Error, OSError) as error:
        sys.exit(str(error))
