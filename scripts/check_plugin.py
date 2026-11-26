#!/usr/bin/env python3
"""Read-only verification of an installed bundle from an unrelated directory."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

parser=argparse.ArgumentParser();parser.add_argument('root',type=Path)
root=parser.parse_args().root.resolve()
manifest=json.loads((root/'plugin.json').read_text())
server=json.loads((root/'mcp.json').read_text())['mcpServers']['makara']
args=[x.replace('${PLUGIN_ROOT}',str(root)) for x in server['args']]
requests=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{}},
          {'jsonrpc':'2.0','id':2,'method':'tools/list'},
          {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'makara_status','arguments':{}}}]
with tempfile.TemporaryDirectory() as directory:
    process=subprocess.run([server['command'],*args],input='\n'.join(json.dumps(x) for x in requests)+'\n',text=True,capture_output=True,timeout=30,cwd=directory,check=True)
results={x['id']:x for x in map(json.loads,process.stdout.splitlines())}
assert 'user requests' in results[1]['result']['instructions']
assert any(x['name']=='makara_source_feed' for x in results[2]['result']['tools'])
assert results[3]['result']['isError'] is False
status=json.loads(results[3]['result']['content'][0]['text'])
assert status['ready'] and status['identity']=='agent-a'
assert status['capture']['wiki_updates']=='user_requested'
assert (root/'skills/makara/SKILL.md').exists()
assert (root/'hooks/hooks.json').exists()
print('PASS: installed plugin MCP initialize, tools, authenticated status and requested-update policy from unrelated cwd')
