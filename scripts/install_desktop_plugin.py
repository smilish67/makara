#!/usr/bin/env python3
"""Build the Windows variant and install through its native Codex CLI."""
import argparse
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from build_plugin import build

p=argparse.ArgumentParser()
p.add_argument('--service-home', type=Path, default=Path.home()/'.local/share/makara')
p.add_argument('--windows-home',type=Path,required=True)
p.add_argument('--codex-exe',required=True)
p.add_argument('--distro',required=True)
p.add_argument('--wsl-codex-exe',help='App WSL runtime executable when the desktop agent environment is WSL')
a=p.parse_args()
home=a.windows_home.resolve()
destination=home/'.codex/makara-marketplace'
root=build(destination, archive=False)
winhome=subprocess.check_output(['wslpath','-w',str(home)],text=True).strip()
settings={'windows_codex_home_wsl':str(home/'.codex'),'service_home':str(a.service_home.resolve()),'windows_home':winhome}
(root/'desktop.json').write_text(json.dumps(settings,indent=2))
args=['--distribution',a.distro,'--user',Path.home().name,'--exec','/usr/bin/python3',str(root/'scripts/windows_bridge.py')]
(root/'mcp.json').write_text(json.dumps({'$schema':'https://agent-plugins.org/schemas/1.0.0/mcp.schema.json','mcpServers':{'makara':{'type':'stdio','command':'wsl.exe','args':args+['mcp']}}},indent=2))
command='wsl.exe '+subprocess.list2cmdline(args+['hook'])
(root/'hooks/hooks.json').write_text(json.dumps({'hooks':{event:[{'hooks':[{'type':'command','command':command.removesuffix('hook')+'plugin-hook','timeout':10}]}] for event in ['SessionStart','UserPromptSubmit','Stop']}},indent=2))
# Do not publish the generic Linux ZIP as the desktop package.
(destination/'makara-plugin.zip').unlink(missing_ok=True)
skill=root/'skills/makara/SKILL.md'
skill.write_text(skill.read_text()+'\n## Windows PC 앱 실행\n\n이 설치본은 WSL의 기존 서비스를 사용한다. 로컬 MCP가 없으면 다음 명령의 마지막 인자를 status, capture-status, 또는 request <action>으로 바꿔 사용한다.\n\n```powershell\n'+command.removesuffix('hook')+'status\n```\n')
market=home/'.agents/plugins/marketplace.json';market.parent.mkdir(parents=True,exist_ok=True)
catalog=json.loads(market.read_text()) if market.exists() else {'name':'makara-local','interface':{'displayName':'Makara Local'},'plugins':[]}
name=catalog['name']
entry={'name':'makara','source':{'source':'local','path':'./.codex/makara-marketplace/plugins/makara'},'policy':{'installation':'AVAILABLE','authentication':'ON_INSTALL'},'category':'Productivity'}
catalog['plugins']=[x for x in catalog.get('plugins',[]) if x.get('name')!='makara']+[entry]
if market.exists():shutil.copy2(market,market.with_suffix('.json.before-makara'))
market.write_text(json.dumps(catalog,ensure_ascii=False,indent=2))
environment=dict(os.environ)
environment.pop('CODEX_HOME',None)  # Native CLI must use the Windows user's home, not inherited WSL paths.
runtime=a.wsl_codex_exe or a.codex_exe
if a.wsl_codex_exe:
    environment['CODEX_HOME']=str(home/'.codex')
    marketplace_root=str(destination)
else:
    marketplace_root=subprocess.check_output(['wslpath','-w',str(destination)],text=True).strip()
# Explicitly register with the same runtime/home the desktop app actually uses.
subprocess.run([runtime,'plugin','marketplace','add',marketplace_root],check=True,cwd=home,env=environment)
subprocess.run([runtime,'plugin','add','makara@makara-local','--json'],check=True,cwd=home,env=environment)
config=home/'.codex/config.toml'
shutil.copy2(config,config.with_suffix('.toml.before-makara-launcher'))
text=config.read_text()
block='[mcp_servers.makara]\ncommand = "wsl.exe"\nargs = '+json.dumps(args+['mcp'])+'\n\n'
if re.search(r'^\[mcp_servers\.makara\]$',text,re.M):
    text=re.sub(r'(?ms)^\[mcp_servers\.makara\]\n.*?(?=^\[|\Z)',lambda _:block,text)
else:text+='\n'+block
config.write_text(text)
hookpath=home/'.codex/hooks.json'
hooks=json.loads(hookpath.read_text()) if hookpath.exists() else {'hooks':{}}
if hookpath.exists():shutil.copy2(hookpath,hookpath.with_suffix('.json.before-makara'))
for event in ['SessionStart','UserPromptSubmit','Stop']:
    groups=hooks.setdefault('hooks',{}).setdefault(event,[])
    for group in groups:
        group['hooks']=[x for x in group.get('hooks',[]) if 'makara/scripts/windows_bridge.py' not in x.get('command','')]
    groups[:]=[x for x in groups if x.get('hooks')]
    groups.append({'hooks':[{'type':'command','command':command,'timeout':10}]})
hookpath.write_text(json.dumps(hooks,ensure_ascii=False,indent=2))
print('Windows hooks registered. Review/trust in the app is required; no trust hashes were modified.')
print('Windows marketplace and WSL launchers installed; original service data and tokens remain in WSL.')
