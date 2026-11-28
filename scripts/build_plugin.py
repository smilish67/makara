#!/usr/bin/env python3
"""Build a credential-free local Codex plugin and its marketplace."""
import argparse
import json
from pathlib import Path
import shutil
import zipfile

def build(destination, archive=True):
    repo=Path(__file__).resolve().parents[1]
    root=destination/'plugins/makara'
    shutil.copytree(repo/'plugins/makara',root,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(repo/'makara',root/'lib/makara',dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(repo/'LICENSE',root/'LICENSE')
    def write(path,value):
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    write(root/'plugin.json',{'$schema':'https://agent-plugins.org/schemas/1.0.0/plugin.schema.json','name':'makara','version':'0.4.3',
        'description':'프로젝트의 결정과 근거를 찾아 다음 시안에 활용합니다. 허용한 대화를 수집하고 요청한 내용을 위키에 정리합니다.',
        'extensions':{'com.openai':{'requires_local_executor':True,'hooks':'./hooks/hooks.json',
            'interface':{'displayName':'Makara','shortDescription':'프로젝트 경험과 판단 기억을 이어서 활용','category':'Productivity',
                         'logo':'./assets/makara-mark.png','composerIcon':'./assets/makara-mark.png',
                         'defaultPrompt':['마카라에서 이 프로젝트의 이전 판단을 불러와줘','오늘 작업 마무리하고 마카라 위키를 업데이트해줘']}}}})
    write(root/'mcp.json',{'$schema':'https://agent-plugins.org/schemas/1.0.0/mcp.schema.json','mcpServers':{'makara':{'type':'stdio','command':'python3','args':['${PLUGIN_ROOT}/scripts/bridge.py','mcp']}}})
    write(root/'hooks/hooks.json',{'hooks':{event:[{'hooks':[{'type':'command','command':'python3 "${PLUGIN_ROOT}/scripts/bridge.py" hook','timeout':5}]}] for event in ['SessionStart','UserPromptSubmit','Stop']}})
    write(destination/'.agents/plugins/marketplace.json',{'name':'makara-local','interface':{'displayName':'Makara Local'},'plugins':[{'name':'makara','source':{'source':'local','path':'./plugins/makara'},'policy':{'installation':'AVAILABLE','authentication':'ON_INSTALL'},'category':'Productivity'}]})
    if not archive:
        return root
    archive=destination/'makara-plugin.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for path in sorted(root.rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts:z.write(path,path.relative_to(root))
    return root

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    print(build(parser.parse_args().output.resolve()))
