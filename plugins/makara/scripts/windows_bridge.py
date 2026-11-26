#!/usr/bin/env python3
"""Run inside WSL, launched by the Windows ChatGPT desktop host."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'lib'))


def linux_path(value):
    if value.startswith('/'):
        return value
    normalized=value.replace('\\','/')
    for prefix in ['//wsl.localhost/','//wsl$/']:
        if normalized.lower().startswith(prefix):
            distro,_,rest=normalized[len(prefix):].partition('/')
            if distro.lower()!=os.environ.get('WSL_DISTRO_NAME','').lower():
                raise ValueError('Hook path belongs to a different WSL distribution')
            return '/'+rest
    return subprocess.check_output(['wslpath','-u',value],text=True,timeout=3).strip()


def main():
    settings=json.loads((ROOT/'desktop.json').read_text())
    os.environ['CODEX_HOME']=settings['windows_codex_home_wsl']
    os.environ['MAKARA_HOME']=settings['service_home']
    mode=sys.argv[1] if len(sys.argv)>1 else 'status'
    if mode in ('hook', 'plugin-hook'):
        from makara.hooks import handle
        event=json.load(sys.stdin)
        if mode == 'plugin-hook':
            legacy = Path(settings['windows_codex_home_wsl']) / 'hooks.json'
            groups = json.loads(legacy.read_text()).get('hooks', {}).get(event.get('hook_event_name'), []) if legacy.exists() else []
            if any('makara/scripts/windows_bridge.py' in h.get('command', '') for group in groups for h in group.get('hooks', [])):
                print('{}')
                return
        event['cwd']=linux_path(event.get('cwd',settings['windows_home']))
        print(json.dumps(handle(Path(settings['service_home']),event),ensure_ascii=False))
        return
    import bridge
    bridge.main()


if __name__=='__main__':
    try: main()
    except Exception as exc:
        if len(sys.argv)>1 and sys.argv[1] in ('hook','plugin-hook'):
            print(json.dumps({'systemMessage':'Makara desktop capture failed; save is not confirmed.'}))
        else:
            print(json.dumps({'saved':False,'error':str(exc)}));sys.exit(1)
