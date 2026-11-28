import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from makara.hooks import handle
from makara.client import Client

class HookTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)
        (self.home/'capture.json').write_text(json.dumps({'projects':{'/allowed':{'enabled':True,'scope':'projects/demo','allowed_providers':['openai','honcho']}}}))
    def test_capture_is_local_idempotent_and_scoped(self):
        event={'cwd':'/allowed','session_id':'session','turn_id':'one','hook_event_name':'UserPromptSubmit','prompt':'Synthetic interest, not a goal'}
        with patch.object(Client,'request',side_effect=AssertionError('Capture must not access network')):
            handle(self.home,event);handle(self.home,event)
            handle(self.home,dict(event,cwd='/private'))
        jobs=list((self.home/'outbox').rglob('*.json'))
        self.assertEqual(len(jobs),1)
        item=json.loads(jobs[0].read_text())
        self.assertEqual(item['data']['allowed_providers'],['openai','honcho'])
        self.assertTrue(item['data']['text'].startswith('[user]'))
    def test_tool_output_is_not_captured(self):
        handle(self.home,{'cwd':'/allowed','hook_event_name':'PostToolUse','tool_response':'secret'})
        self.assertFalse((self.home/'outbox').exists())
    def test_unregistered_session_reports_missing_capture_without_storing(self):
        with patch.object(Client,'request',side_effect=AssertionError('Unregistered project must not access service')):
            result=handle(self.home,{'cwd':'/private','hook_event_name':'SessionStart'})
        self.assertIn('자동 저장되지 않습니다',result['systemMessage'])
        self.assertFalse((self.home/'outbox').exists())
    def test_children_require_explicit_policy_and_exclude_siblings(self):
        event={'cwd':'/allowed/sub','hook_event_name':'UserPromptSubmit','prompt':'test'}
        handle(self.home,event)
        self.assertFalse((self.home/'outbox').exists())
        p=self.home/'capture.json';policy=json.loads(p.read_text())
        policy['projects']['/allowed']['include_children']=True
        p.write_text(json.dumps(policy))
        handle(self.home,event)
        handle(self.home,dict(event,cwd='/allowed-sibling'))
        self.assertEqual(len(list((self.home/'outbox').rglob('*.json'))),1)
    def test_antigravity_pre_invocation_and_session_start(self):
        unregistered=handle(self.home,{'cwd':'/private','hookEventName':'SessionStart'})
        self.assertIn('injectSteps',unregistered)
        self.assertIn('자동 저장되지 않습니다',unregistered['injectSteps'][0]['systemMessage']['systemMessage'])
        event={'cwd':'/allowed','conversationId':'conv-1','invocationNum':2,'hookEventName':'PreInvocation','lastUserInput':'Hello Antigravity'}
        with patch.object(Client,'request',side_effect=AssertionError('Capture must not access network')):
            result=handle(self.home,event)
            handle(self.home,event)
        self.assertEqual(result,{})
        jobs=list((self.home/'outbox').rglob('*.json'))
        self.assertEqual(len(jobs),1)
        item=json.loads(jobs[0].read_text())
        self.assertEqual(item['data']['conversation'],{'session_id':'conv-1','turn_id':2,'role':'user'})
