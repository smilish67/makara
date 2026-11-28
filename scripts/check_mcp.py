#!/usr/bin/env python3
"""Live two-client check using the official MCP SDK, without model calls."""
import asyncio
import json
from pathlib import Path
import sys
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from makara.client import Client

async def main():
    home=Path.home()/'.local/share/makara'
    owner=Client(home,home/'owner.token')
    launcher=str(home/'runtime/scripts/run_mcp.py')
    source_id=None
    try:
        async with stdio_client(StdioServerParameters(command=sys.executable,args=[launcher,'--home',str(home),'--token-file',str(home/'agent-a.token')])) as (read,write):
            async with ClientSession(read,write) as first:
                await first.initialize()
                tools=await first.list_tools()
                assert any(tool.name=='makara_context' for tool in tools.tools)
                response=await first.call_tool('makara_source',dict(scope='projects/makara',text='Integration check: shared Markdown with provenance.',consent=True,idempotency_key='mcp-check-'+str(uuid.uuid4()),allowed_providers=['openai','anthropic']))
                assert not response.is_error,response
                source_id=json.loads(response.content[0].text)['id']
                response=await first.call_tool('makara_propose',dict(scope='projects/makara',key='knowledge/mcp-check',body='Integration check: shared Markdown with provenance.',kind='state',confirmation='observed',sources=[source_id],base_version=None,reason='Temporary integration test'))
                assert not response.is_error,response
                proposal=json.loads(response.content[0].text)
                assert proposal['applied'] and proposal['status']=='applied',proposal
        async with stdio_client(StdioServerParameters(command=sys.executable,args=[launcher,'--home',str(home),'--token-file',str(home/'agent-b.token')])) as (read,write):
            async with ClientSession(read,write) as second:
                await second.initialize()
                response=await second.call_tool('makara_context',{'scopes':['projects/makara'],'query':'Integration check'})
                assert not response.is_error,response
                docs=json.loads(response.content[0].text)['documents']
                assert any(doc['key']=='knowledge/mcp-check' and source_id in doc['metadata']['sources'] for doc in docs),docs
                response=await second.call_tool('makara_inspect_source',{'id':source_id})
                assert not response.is_error
                response=await second.call_tool('makara_search',{'scopes':['projects/private'],'query':''})
                assert response.is_error
        print('PASS: MCP initialize/tools; agent A immediate update without owner approval; agent B context and provenance; scope denial.')
    finally:
        if source_id: owner.submit('delete-source',{'id':source_id})

asyncio.run(main())
