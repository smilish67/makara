import json
import threading
import time
from unittest.mock import patch
import test_workflow
import unittest
from makara import memory

class MemoryTests(unittest.TestCase):
    def setUp(self):
        test_workflow.test_core.KnowledgeTests.setUp(self)
        self.store = test_workflow.KnowledgeStore(self.store.vault,self.store.private,{}, {})
        self.owner['provider']='openai'
        self.store.principals={'owner':self.owner}
        self.store.config['honcho']={'enabled':True,'base_url':'http://127.0.0.1:9999','allowed_scopes':['projects/demo']}
    call = test_workflow.WorkflowTests.call
    source = test_workflow.WorkflowTests.source
    proposal = test_workflow.WorkflowTests.proposal

    def add(self,text='Original fact',key='m1'):
        return self.call('source',dict(scope='projects/demo',text=text,consent=True,idempotency_key=key,allowed_providers=['honcho','openai']))

    def test_wiki_edit_preserves_memory_and_source_history(self):
        source=self.add()
        query={'scope':'projects/demo','query':memory.DEFAULT_QUERY}
        with patch.object(memory.Honcho,'call',return_value={'representation':'Original fact','items':[]}) as network:
            memory.process_jobs(self.store)
            before=[json.loads(p.read_text()) for p in memory.states(self.store) if json.loads(p.read_text())['status']=='ready'][0]
            doc=self.call('approve',{'id':self.proposal(source)['id']})
            extra=self.add('Additional context','m2')
            self.call('approve',{'id':self.proposal(extra,body='A rewritten summary',version=doc['version'])['id']})
            memory.process_jobs(self.store)
            after=self.store.load('memory',before['id'])
            self.assertEqual(after['status'],'ready')
            self.assertEqual(set(after['source_ids']),{source['id'],extra['id']})
            self.assertFalse(any(c.args[0]=='DELETE' for c in network.call_args_list))
            self.assertEqual(self.call('memory',query)['status'],'ready')

    def test_explicit_correction_queues_rebuild_without_read(self):
        source=self.add()
        doc=self.call('approve',{'id':self.proposal(source)['id']})
        with patch.object(memory.Honcho,'call',return_value={'representation':'old','items':[]}):
            memory.process_jobs(self.store)
        correction=self.add('Corrected fact','m2')
        self.call('retire-source',{'id':source['id'],'replacement_id':correction['id'],'reason':'User correction'})
        self.assertEqual([s['id'] for s in memory.eligible(self.store,self.owner,'projects/demo')],[correction['id']])
        self.assertTrue(list((self.store.private/'memory-jobs').glob('*.json')))
        self.assertEqual(self.call('context',{'scopes':['projects/demo']})['documents'],[])
        with patch.object(memory.Honcho,'call',return_value={'representation':'new','items':[]}) as network:
            memory.process_jobs(self.store)
            payloads=[c.args[2] for c in network.call_args_list if len(c.args)>2 and '/messages' in c.args[1]]
            self.assertEqual([x['messages'][0]['content'] for x in payloads],['Corrected fact'])

    def test_missing_job_recovered_and_source_order_is_chronological(self):
        a=self.add('First','a');b=self.add('Second','b')
        for p in (self.store.private/'memory-jobs').glob('*.json'):p.unlink()
        with patch.object(memory.Honcho,'call',return_value={'representation':'observations','items':[]}) as network:
            memory.process_jobs(self.store)
            payloads=[c.args[2]['messages'][0]['content'] for c in network.call_args_list if len(c.args)>2 and '/messages' in c.args[1]]
            self.assertEqual(payloads,['First','Second'])
        self.assertTrue(list((self.store.private/'memory-jobs').glob('*.json')))

    def test_existing_observations_are_not_reported_fully_updated_while_deriving(self):
        self.add()
        def reply(method,path,*args,**kwargs):
            if path.endswith('/queue/status'):return {'pending_work_units':1}
            return {'representation':'older observations','items':[]}
        with patch.object(memory.Honcho,'call',side_effect=reply):
            memory.process_jobs(self.store)
        result=self.call('memory',{'scope':'projects/demo','query':memory.DEFAULT_QUERY})
        self.assertEqual(result['status'],'pending_derivation')

    def test_failed_wiki_conflict_does_not_invalidate(self):
        a=self.add()
        with patch.object(memory.Honcho,'call',return_value={'representation':'fact','items':[]}):
            memory.process_jobs(self.store)
        self.call('approve',{'id':self.proposal(a)['id']})
        with self.assertRaises(memory.Error):
            self.call('approve',{'id':self.proposal(a,body='stale')['id']})
        self.assertTrue(all(json.loads(p.read_text())['status']=='ready' for p in memory.states(self.store)))

    def test_legacy_exclusions_migrate_once(self):
        a=self.add('Old','a')
        doc=self.call('approve',{'id':self.proposal(a)['id']})
        b=self.add('New','b')
        self.call('approve',{'id':self.proposal(b,version=doc['version'])['id']})
        memory.migrate_legacy_history(self.store)
        self.assertTrue(self.store.load('sources',a['id']).get('memory_retired'))
        self.assertEqual([x['id'] for x in memory.eligible(self.store,self.owner,'projects/demo')],[b['id']])
        memory.migrate_legacy_history(self.store)

    def test_slow_model_does_not_hold_writer_and_deleted_result_is_discarded(self):
        source=self.add()
        entered=threading.Event();release=threading.Event()
        def slow(*args,**kwargs):
            if '/context?' in args[1]:
                entered.set();release.wait(3)
            return {'content':'private inference','items':[]}
        with patch.object(memory.Honcho,'call',side_effect=slow):
            worker=threading.Thread(target=memory.process_jobs,args=(self.store,));worker.start()
            self.assertTrue(entered.wait(2))
            start=time.monotonic()
            self.call('delete-source',{'id':source['id']})
            self.assertLess(time.monotonic()-start,.5)
            release.set();worker.join(3)
        self.assertEqual(list((self.store.private/'memory-jobs').glob('*.json')),[])

    def test_memory_failure_is_retryable_and_not_cached_success(self):
        self.add()
        with patch.object(memory.Honcho,'call',side_effect=memory.Error(503,'offline')): memory.process_jobs(self.store)
        jobs=list((self.store.private/'memory-jobs').glob('*.json'))
        self.assertEqual(json.loads(jobs[0].read_text())['status'],'retry_pending')

    def test_remote_memory_requires_explicit_configuration(self):
        with self.assertRaises(memory.Error): memory.Honcho({'base_url':'http://example.com'})
        with self.assertRaises(memory.Error): memory.Honcho({'base_url':'https://example.com'})
