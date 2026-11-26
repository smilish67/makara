import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from makara.capture import resolve_target, project_scope, sanitize
from makara.workflow import KnowledgeStore
from makara.core import Error, digest


class CapturePolicyTests(unittest.TestCase):
    def test_global_scope_is_stable_and_exclusions_win(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'project').mkdir()
            policy={'mode':'all_projects','excluded_paths':[str(root/'private')]}
            self.assertIsNone(resolve_target(policy,root/'private'/'sub'))
            target=resolve_target(policy,root/'project')
            self.assertEqual(target['project_root'],str(root/'project'))
            self.assertEqual(project_scope(target['project_root']),project_scope(str(root/'project')))
            self.assertNotEqual(project_scope(str(root/'project')),project_scope('/else/project'))

    def test_plugin_disabled_stops_registered_and_global_capture(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ',{'CODEX_HOME':tmp}):
            path=Path(tmp)/'config.toml'
            policy={'require_plugin':'makara@makara-local','mode':'all_projects','projects':{'/known':{'enabled':True,'scope':'projects/known'}}}
            path.write_text('[plugins."makara@makara-local"]\nenabled = false\n')
            self.assertIsNone(resolve_target(policy,'/known'))
            self.assertIsNone(resolve_target(policy,tmp))
            path.write_text('[plugins."makara@makara-local"]\nenabled = true\n')
            self.assertEqual(resolve_target(policy,'/known')['scope'],'projects/known')
            local=Path(tmp)/'project/.codex';local.mkdir(parents=True)
            (local/'config.toml').write_text('[plugins."makara@makara-local"]\nenabled = false\n')
            self.assertIsNone(resolve_target(policy,local.parent))

    def test_new_project_capture_does_not_write_wiki_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            p={'id':'agent-a','subject':'self','provider':'openai','read':[],'write':[]}
            principals={digest('test-token'):p}
            config={'capture':{'enabled':True,'agents':['agent-a'],'allowed_providers':['openai','honcho']}}
            store=KnowledgeStore(Path(tmp)/'vault',Path(tmp)/'private',principals,config)
            data={'project_root':'/new/project','text':'[user] I chose B because of the evidence','consent':True,
                  'idempotency_key':'test','request_id':'test-request','allowed_providers':['openai','honcho']}
            result=store.dispatch(p,'capture-source',data)
            scope=project_scope('/new/project')
            self.assertEqual(result['scope'],scope)
            self.assertFalse(list(store.vault.rglob('*.md')))
            self.assertIn(scope,p['read'])
            p2=dict(p,read=[],write=[])
            reloaded=KnowledgeStore(store.vault,store.private,{digest('test-token'):p2},config)
            self.assertIn(scope,p2['read'])
            self.assertEqual(reloaded.dispatch(p2,'source-feed',{'scope':scope})['items'][0]['id'],result['id'])
            unauthorized=dict(p2,id='other',read=[],write=[])
            with self.assertRaises(Error):reloaded.dispatch(unauthorized,'capture-register',{'project_root':'/private'})
            with self.assertRaises(Error):reloaded.dispatch(p2,'capture-source',dict(data,request_id='new',allowed_providers=['anthropic']))
            config['capture']['enabled']=False
            with self.assertRaises(Error):reloaded.dispatch(p2,'capture-register',{'project_root':'/newer'})

    def test_redaction_keeps_nonsecret_reasoning(self):
        text='API_KEY=secret-value\nI prefer B because it fits the user.\nBearer abcdefghijklmnopqrstuvwxyz'
        cleaned=sanitize(text)
        self.assertNotIn('secret-value',cleaned)
        self.assertNotIn('abcdefghijklmnopqrstuvwxyz',cleaned)
        self.assertIn('I prefer B because it fits the user.',cleaned)
