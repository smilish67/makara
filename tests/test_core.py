import concurrent.futures
import tempfile
import unittest
from pathlib import Path
from makara.core import Store, Error


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.store = Store(root / 'vault', root / 'private', {})
        self.owner = dict(id='owner', subject='self', read=['me', 'projects/demo'], write=['me', 'projects/demo'], approve=['me', 'projects/demo'], delete=['me', 'projects/demo'])
        self.agent = dict(self.owner, id='agent', approve=[], delete=[])
        self.stranger = dict(id='other', subject='self', read=['me'], write=['me'])

    def call(self, action, data, actor=None):
        return self.store.dispatch(actor or self.owner, action, data)

    def source(self, text='User explicitly chose Markdown.', key='message-1'):
        return self.call('source', dict(scope='projects/demo', text=text, consent=True, idempotency_key=key))

    def proposal(self, source, body='Use Markdown.', version=None, **kwargs):
        data = dict(scope='projects/demo', key='decisions', body=body, base_version=version, kind='decision', confirmation='user_confirmed', sources=[source['id']], reason='User decision')
        data.update(kwargs)
        return self.call('propose', data, self.agent)

    def test_shared_context_and_provenance(self):
        source = self.source()
        proposal = self.proposal(source)
        self.call('approve', {'id': proposal['id']})
        result = self.call('context', {'scopes': ['projects/demo']}, self.agent)
        self.assertEqual(result['documents'][0]['metadata']['sources'], [source['id']])
        self.assertEqual(self.call('inspect-source', {'id': source['id']}, self.agent)['text'], source['text'])
        self.assertIsNone(source['occurred_at'])

    def test_goal_cannot_be_inferred(self):
        with self.assertRaises(Error) as error:
            self.proposal(self.source(), kind='goal', confirmation='unverified')
        self.assertEqual(error.exception.status, 400)

    def test_scope_and_approval(self):
        proposal = self.proposal(self.source())
        for action, data, actor in [('approve', {'id': proposal['id']}, self.agent), ('search', {'scopes': ['projects/demo'], 'query': ''}, self.stranger)]:
            with self.assertRaises(Error) as error:
                self.call(action, data, actor)
            self.assertEqual(error.exception.status, 403)

    def test_correction_and_supersession(self):
        source = self.source()
        first = self.call('approve', {'id': self.proposal(source)['id']})
        correction = self.source('Correction: use plain Markdown without Obsidian plugins.', 'message-2')
        second = self.call('approve', {'id': self.proposal(correction, 'Plain Markdown only.', first['version'])['id']})
        self.assertEqual(second['metadata']['supersedes'], first['metadata']['id'])
        self.assertEqual(self.call('read', {'scope': 'projects/demo', 'key': 'decisions'}, self.agent)['body'], 'Plain Markdown only.')

    def test_concurrent_updates(self):
        source = self.source()
        proposals = [self.proposal(source, body=text) for text in ['First', 'Second']]
        def apply(item):
            try:
                self.call('approve', {'id': item['id']})
                return 200
            except Error as error:
                return error.status
        with concurrent.futures.ThreadPoolExecutor() as executor:
            self.assertEqual(sorted(executor.map(apply, proposals)), [200, 409])

    def test_obsidian_edit_conflicts(self):
        source = self.source()
        first = self.call('approve', {'id': self.proposal(source)['id']})
        proposal = self.proposal(source, 'New text', first['version'])
        path = self.store.path('projects/demo', 'decisions')
        path.write_text(path.read_text() + '\nHuman edit')
        with self.assertRaises(Error) as error:
            self.call('approve', {'id': proposal['id']})
        self.assertEqual(error.exception.status, 409)
        self.assertIn('Human edit', path.read_text())

    def test_idempotency_and_deletion(self):
        source = self.source()
        self.assertEqual(source['id'], self.source()['id'])
        with self.assertRaises(Error):
            self.source('Different content')
        self.call('approve', {'id': self.proposal(source)['id']})
        self.call('delete-source', {'id': source['id']})
        self.assertEqual(self.call('context', {'scopes': ['projects/demo']})['documents'], [])
        with self.assertRaises(Error) as error:
            self.source()
        self.assertEqual(error.exception.status, 410)

    def test_path_escape(self):
        with self.assertRaises(Error):
            self.call('read', {'scope': 'projects/demo', 'key': '../../secret'})


if __name__ == '__main__':
    unittest.main()
