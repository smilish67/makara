#!/usr/bin/env python3
"""Apply reviewed instructions without deleting or replaying memories."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from makara.core import atomic
from makara.memory import Honcho

parser = argparse.ArgumentParser()
parser.add_argument('--home', type=Path, default=Path.home()/'.local/share/makara')
parser.add_argument('--prompt', type=Path, default=Path(__file__).resolve().parents[1]/'docs/HONCHO_MEMORY_PROMPT.txt')
parser.add_argument('--base-url', help='Host-side API address, without changing service config')
args = parser.parse_args()
prompt = args.prompt.read_text().strip()
if not prompt or len(prompt) > 10000:
    raise SystemExit('Prompt must contain 1–10000 characters')
config_path = args.home/'config.json'
config = json.loads(config_path.read_text())
allowed_scopes = set(config['honcho']['allowed_scopes'])
registry = args.home/'capture-projects.json'
capture = config.get('capture', {})
if registry.exists() and 'honcho' in capture.get('allowed_providers', []):
    for record in json.loads(registry.read_text()).values():
        if any(p['id'] == record['actor'] and p['subject'] == record['subject']
               and p['id'] in capture.get('agents', []) for p in config['principals'].values()):
            allowed_scopes.add(record['scope'])
api_config = dict(config['honcho'])
if args.base_url:
    api_config['base_url'] = args.base_url
api = Honcho(api_config)
count = 0
for path in sorted((args.home/'memory').glob('*.json')):
    state = json.loads(path.read_text())
    if state.get('status') != 'ready' or state.get('scope') not in allowed_scopes:
        continue
    workspace = state['workspace']
    listing = api.call('POST', '/workspaces/list', {'filters':{'id':workspace}})
    items = listing.get('items', [])
    if len(items) != 1:
        raise RuntimeError('Expected one active workspace: '+state['scope'])
    settings = dict(items[0].get('configuration') or {})
    settings['custom_instructions'] = prompt
    result = api.call('PUT', '/workspaces/'+workspace, {'configuration':settings})
    if result.get('configuration', {}).get('custom_instructions') != prompt:
        raise RuntimeError('Prompt verification failed: '+state['scope'])
    count += 1
    print('Verified workspace instructions:', state['scope'])
backup = config_path.with_name('config.before-memory-prompt.json')
if not backup.exists():
    atomic(backup, config_path.read_text())
config['honcho']['custom_instructions'] = prompt
atomic(config_path, json.dumps(config, ensure_ascii=False, indent=2))
print(f'Applied to {count} workspaces; config saved for future workspaces. Restart Makara to reload config. Existing memories were not rebuilt.')
