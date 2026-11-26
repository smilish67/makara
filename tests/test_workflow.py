import io
import json
import sys
import uuid
from unittest.mock import patch
from makara.workflow import KnowledgeStore
from makara.core import Error, digest
from makara.client import Client, MUTATIONS
from makara.mcp import run
import test_core


class WorkflowTests(test_core.KnowledgeTests):
    def setUp(self):
        super().setUp()
        self.store = KnowledgeStore(self.store.vault,self.store.private,{},dict(automation={'projects/demo':{'kinds':['state','fact','interest'],'keys':['state','interests','profile']}}))

    def call(self, action, data, actor=None):
        if action in MUTATIONS: data=dict(data,request_id=data.get('request_id',str(uuid.uuid4())))
        return self.store.dispatch(actor or self.owner,action,data)

    def test_provider_policy_and_cross_subject(self):
        source=self.source()
        self.call('approve',{'id':self.proposal(source)['id']})
        cloud=dict(self.agent,provider='openai')
        self.assertEqual(self.call('context',{'scopes':['projects/demo']},cloud)['documents'],[])
        with self.assertRaises(Error): self.call('inspect-source',{'id':source['id']},cloud)
        other=dict(self.agent,subject='different-person')
        with self.assertRaises(Error): self.call('inspect-source',{'id':source['id']},other)

    def proposal(self, source, body='Use Markdown.', version=None, **kwargs):
        # Exercise legacy staged records for the inherited recovery tests.
        data = dict(scope='projects/demo', key='decisions', body=body, base_version=version,
                    kind='decision', confirmation='user_confirmed', sources=[source['id']], reason='User decision')
        data.update(kwargs)
        with self.store.lock():
            return self.store.propose(self.agent, data)

    def test_immediate_goal_and_idempotent_retry(self):
        source = self.source('My confirmed goal')
        data = dict(scope='projects/demo', key='goals', body=source['text'], base_version=None,
                    kind='goal', confirmation='user_confirmed', sources=[source['id']],
                    reason='Explicit user goal', request_id='direct-goal')
        result = self.call('propose', data, self.agent)
        self.assertEqual(result['status'], 'applied')
        self.assertTrue(result['applied'])
        doc = self.call('read', {'scope':'projects/demo','key':'goals'}, self.agent)
        self.assertEqual(doc['body'], source['text'])
        self.assertTrue(self.call('propose', data, self.agent)['replayed'])
        with self.assertRaises(Error) as error:
            self.call('propose', dict(data, request_id='stale', body='stale replacement'), self.agent)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.call('read', {'scope':'projects/demo','key':'goals'}, self.agent)['version'], doc['version'])

    def test_direct_update_permissions(self):
        source = self.source()
        data = dict(scope='projects/demo', key='state', body='Update', base_version=None,
                    kind='state', confirmation='observed', sources=[source['id']], reason='Evidence')
        for actor in [self.stranger, dict(self.agent, provider='openai'), dict(self.agent, subject='other')]:
            with self.assertRaises(Error):
                self.call('propose', data, actor)
        proposal = self.proposal(source)
        with self.assertRaises(Error):
            self.call('auto-apply', {'id':proposal['id']}, dict(self.agent,id='another-agent'))

    def test_direct_update_cannot_overwrite_provider_private_document(self):
        source = self.source()
        self.call('approve', {'id':self.proposal(source)['id']})
        cloud = dict(self.agent, provider='openai')
        shared = self.call('source', dict(scope='projects/demo',text='Allowed',consent=True,
                           idempotency_key='shared',allowed_providers=['openai']), cloud)
        old = self.call('read', {'scope':'projects/demo','key':'decisions'})
        with self.assertRaises(Error) as error:
            self.call('propose', dict(scope='projects/demo',key='decisions',body='Overwrite',
                      base_version=old['version'],kind='decision',confirmation='observed',
                      sources=[shared['id']],reason='Change'), cloud)
        self.assertEqual(error.exception.status, 403)

    def test_manual_edit_revision_and_source(self):
        source=self.source()
        doc=self.call('approve',{'id':self.proposal(source)['id']})
        result=self.call('manual-edit',{'scope':'projects/demo','key':'decisions','base_version':doc['version'],'body':'Owner correction.'})
        self.assertEqual(result['body'],'Owner correction.')
        with self.assertRaises(Error) as error:
            self.call('manual-edit',{'scope':'projects/demo','key':'decisions','base_version':doc['version'],'body':'Stale buffer'})
        self.assertEqual(error.exception.status,409)

    def test_mutation_receipts_do_not_restore_deleted_text(self):
        data=dict(scope='projects/demo',text='Sensitive old source',consent=True,idempotency_key='receipt',request_id='request-1')
        source=self.call('source',data)
        self.call('delete-source',{'id':source['id']})
        replay=self.call('source',data)
        self.assertNotIn('text',replay)
        self.assertTrue(replay['replayed'])
        self.assertFalse(self.store.hidden('sources',source['id']).exists())
        with self.assertRaises(Error): self.call('source',dict(data,text='Different body'))

    def test_budget_and_stale(self):
        self.store.config['stale_after_days']=-1
        self.call('approve',{'id':self.proposal(self.source(),body='x'*500)['id']})
        result=self.call('context',{'scopes':['projects/demo'],'max_chars':100})
        self.assertEqual(result['body_chars'],100)
        self.assertTrue(result['documents'][0]['stale'])
        self.assertTrue(result['truncated'])

    def test_verified_job(self):
        self.store.config['verification']={'projects/demo':{'check':{'argv':[sys.executable,'-c','print("checked")'],'cwd':str(self.store.private)}}}
        actor=dict(self.agent,verify=['projects/demo'])
        result=self.call('verify-work',{'scope':'projects/demo','job':'check'},actor)
        self.assertEqual(result['attestation']['actor'],'verification-service')
        with self.assertRaises(Error): self.call('verify-work',{'scope':'projects/demo','job':'arbitrary-command'},actor)

    def test_durable_outbox(self):
        token=self.store.private/'agent-a.token';token.write_text('test')
        client=Client(self.store.private,token)
        with patch.object(client,'request',side_effect=Error(503,'offline')):
            result=client.submit('source',dict(scope='projects/demo',text='hello',consent=True))
        self.assertTrue(result['queued'])
        self.assertFalse(result['saved'])
        files=list(client.queue.glob('*.json'));self.assertEqual(len(files),1)
        data=json.loads(files[0].read_text())['data']
        with patch.object(client,'request',return_value={'id':'done'}) as request:
            client.flush(force=True)
            self.assertEqual(request.call_args.args[1]['request_id'],data['request_id'])
        self.assertEqual(list(client.queue.glob('*.json')),[])

    def test_interrupted_deletion_can_resume(self):
        source=self.source()
        self.call('approve',{'id':self.proposal(source)['id']})
        self.store.save('tombstones',dict(id=source['id'],scope=source['scope'],subject='self',deleted_at='test'))
        self.assertEqual(self.call('context',{'scopes':['projects/demo']})['documents'],[])
        result=self.call('delete-source',{'id':source['id']})
        self.assertTrue(result['deleted'])
        self.assertFalse(self.store.hidden('sources',source['id']).exists())

    def test_conflicting_retry_is_retained_for_review(self):
        token=self.store.private/'agent-a.token';token.write_text('test')
        client=Client(self.store.private,token)
        with patch.object(client,'request',side_effect=Error(409,'conflict')):
            with self.assertRaises(Error): client.submit('approve',{'id':str(uuid.uuid4())})
        item=json.loads(next(client.queue.glob('*.json')).read_text())
        self.assertEqual(item['status'],'blocked')
        with patch.object(client,'request') as request:
            client.flush(force=True)
            request.assert_not_called()

    def test_source_feed_filters_and_pages(self):
        first = self.source('one')
        second = self.source('two', 'two')
        page = self.call('source-feed', {'scope':'projects/demo','limit':1}, self.agent)
        self.assertEqual([x['id'] for x in page['items']], [first['id']])
        self.assertTrue(page['has_more'])
        next_page = self.call('source-feed', {'scope':'projects/demo','after':page['next_cursor']}, self.agent)
        self.assertEqual([x['id'] for x in next_page['items']], [second['id']])
        self.assertEqual(self.call('source-feed', {'scope':'projects/demo'}, dict(self.agent,provider='openai'))['items'], [])
        with self.assertRaises(Error): self.call('source-feed', {'scope':'projects/demo'}, self.stranger)

    def test_retire_requires_same_scope_and_visible_replacement(self):
        old = self.source('Old')
        replacement = self.call('source', dict(scope='me',text='New',consent=True,idempotency_key='new'))
        with self.assertRaises(Error):
            self.call('retire-source', {'id':old['id'],'replacement_id':replacement['id'],'reason':'correction'})
        self.assertFalse(self.store.load('sources',old['id']).get('memory_retired'))

    def test_mcp_protocol(self):
        messages=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18'}},
                  {'jsonrpc':'2.0','method':'notifications/initialized'},
                  {'jsonrpc':'2.0','id':2,'method':'tools/list'},
                  {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'makara_status','arguments':{}}}]
        client=Client(self.store.private,self.store.private/'agent-a.token')
        output=io.StringIO()
        with patch.object(client,'submit',return_value={'ready':True}):
            run(client,io.StringIO('\n'.join(json.dumps(x) for x in messages)),output)
        responses=[json.loads(x) for x in output.getvalue().splitlines()]
        self.assertEqual(len(responses),3)
        names=[x['name'] for x in responses[1]['result']['tools']]
        self.assertIn('makara_context',names)
        self.assertNotIn('makara_approve',names)
