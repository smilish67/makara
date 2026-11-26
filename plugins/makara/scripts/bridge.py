#!/usr/bin/env python3
"""Bundled stdlib-only entry point; credentials remain in the local service home."""
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
from makara.client import Client
from makara.core import Error


def main():
    home=Path(os.environ.get('MAKARA_HOME',str(Path.home()/'.local/share/makara')))
    token=home/'agent-a.token'
    action=sys.argv[1] if len(sys.argv)>1 else 'status'
    if action=='mcp':
        from makara.mcp import run
        run(Client(home,token));return
    if action=='hook':
        from makara.hooks import handle
        event=json.load(sys.stdin)
        # Preserve the already trusted legacy hook during migration; do not double-inject context.
        legacy=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))/'hooks.json'
        if legacy.exists():
            groups=json.loads(legacy.read_text()).get('hooks',{}).get(event.get('hook_event_name'),[])
            if any('makara/runtime/scripts/run_hook.py' in h.get('command','') for g in groups for h in g.get('hooks',[])):
                print('{}');return
        print(json.dumps(handle(home,event),ensure_ascii=False));return
    client=Client(home,token)
    if action=='capture-status':
        from makara.capture import resolve_target,plugin_enabled
        policy=json.loads((home/'capture.json').read_text())
        print(json.dumps({'plugin_enabled':plugin_enabled(policy,os.getcwd()),'mode':policy.get('mode','allowlist'),
                          'target':resolve_target(policy,os.getcwd()),'excluded_paths':policy.get('excluded_paths',[]),
                          'wiki_updates':'user_requested'},ensure_ascii=False));return
    if action=='status': result=client.request('status',{})
    elif action=='request': result=client.submit(sys.argv[2],json.load(sys.stdin))
    else: raise SystemExit('Use status, capture-status, request ACTION, mcp, or hook')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    try: main()
    except (Error,OSError,ValueError) as exc:
        if len(sys.argv)>1 and sys.argv[1]=='hook':
            print(json.dumps({'systemMessage':'Makara 연결/수집 실패. 저장을 확인하지 못했습니다.'},ensure_ascii=False))
        else:
            print(json.dumps({'saved':False,'error':str(exc)},ensure_ascii=False));sys.exit(1)
