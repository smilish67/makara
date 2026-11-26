import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError


class HttpTests(unittest.TestCase):
    def test_server_and_cli(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / 'private'
            subprocess.run([sys.executable, '-m', 'makara.cli', '--home', str(home), 'init', '--vault', str(root / 'vault')], check=True, capture_output=True)
            self.assertEqual((home / 'owner.token').stat().st_mode & 0o777, 0o600)
            server = subprocess.Popen([sys.executable, '-m', 'makara.cli', '--home', str(home), 'serve', '--port', '0'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                base = server.stdout.readline().strip().split()[-1]
                token = (home / 'agent-a.token').read_text()
                def call(action, data, credential=token):
                    request = Request(base + '/v1/' + action, data=json.dumps(data).encode(), headers={'Authorization': 'Bearer ' + credential})
                    with urlopen(request, timeout=5) as response:
                        return json.load(response)
                self.assertEqual(call('health', {})['status'], 'ready')
                with self.assertRaises(HTTPError) as error:
                    call('health', {}, 'bad-token')
                self.assertEqual(error.exception.code, 401)
                source = call('source', dict(scope='projects/demo', text='A real HTTP source', consent=True, idempotency_key='http-1'))
                self.assertEqual(call('inspect-source', {'id': source['id']})['text'], 'A real HTTP source')
                payload = root / 'context.json'
                payload.write_text('{"scopes":["projects/demo"]}')
                command = [sys.executable, '-m', 'makara.cli', 'call', 'context', '--data', str(payload), '--token-file', str(home / 'agent-b.token'), '--port', base.rsplit(':', 1)[1]]
                result = subprocess.run(command, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)['documents'], [])
                server.terminate()
                server.wait(timeout=5)
                result = subprocess.run(command, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 2)
                self.assertIn('Not confirmed saved', result.stderr)
            finally:
                if server.poll() is None:
                    server.terminate()
                server.communicate(timeout=5)
