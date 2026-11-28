#!/usr/bin/env python3
"""Validate Honcho v3 storage/deletion with synthetic data; never calls a model."""
from pathlib import Path
import sys
import time
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from makara.memory import Honcho
api=Honcho({'base_url':'http://127.0.0.1:18766','token_file':str(Path.home()/'.local/share/makara/honcho/admin.token')})
workspace='makara-check-'+str(uuid.uuid4())
try:
    api.call('POST','/workspaces',{'id':workspace})
    api.call('POST',f'/workspaces/{workspace}/peers',{'id':'self'})
    api.call('POST',f'/workspaces/{workspace}/sessions',{'id':'sources','peers':{'self':{'observe_me':True}}})
    result=api.call('POST',f'/workspaces/{workspace}/sessions/sources/messages',{'messages':[{'peer_id':'self','content':'Synthetic Makara storage check; no personal information.','metadata':{'source_id':'synthetic'}}]})
    assert result[0]['content'].startswith('Synthetic'),result
finally:
    api.call('DELETE',f'/workspaces/{workspace}/sessions/sources',missing_ok=True)
    api.call('DELETE',f'/workspaces/{workspace}',missing_ok=True)
for _ in range(30):
    result=api.call('POST','/workspaces/list',{'filters':{'id':workspace}})
    if not result['items']:
        print('PASS: live Honcho workspace/peer/session/message storage and confirmed asynchronous deletion. No model call.')
        break
    time.sleep(1)
else:
    raise SystemExit('Deletion accepted but not confirmed within 30 seconds')
