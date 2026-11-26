"""Durable, credential-free request outbox and bounded local HTTP client."""
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import uuid
from .core import atomic, Error, digest

READS = {'health', 'context', 'search', 'read', 'inspect-source', 'proposals', 'memory', 'status', 'source-feed'}
MUTATIONS = {'source', 'capture-source', 'propose', 'approve', 'delete-source', 'manual-edit', 'attest', 'verify-work', 'auto-apply', 'reject', 'retire-source'}


class Client:
    def __init__(self, home, token_file, port=18765):
        self.home, self.token_file, self.port = Path(home), Path(token_file).resolve(), port
        self.queue = self.home / 'outbox' / digest(str(self.token_file))[:16]

    def request(self, action, data):
        raw = json.dumps(data, ensure_ascii=False).encode()
        req = Request(f'http://127.0.0.1:{self.port}/v1/{action}', data=raw,
                      headers={'Authorization': 'Bearer ' + self.token_file.read_text().strip(), 'Content-Type': 'application/json'})
        try:
            with urlopen(req, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            try:
                message = json.load(exc).get('error', 'Request failed')
            except ValueError:
                message = 'Request failed'
            raise Error(exc.code, message)
        except (URLError, TimeoutError, ConnectionError, OSError):
            raise Error(503, 'Server unavailable; save is not confirmed')

    def submit(self, action, data):
        if action not in MUTATIONS:
            return self.request(action, data)
        data = dict(data)
        data.setdefault('request_id', str(uuid.uuid4()))
        if action == 'source':
            data.setdefault('idempotency_key', data['request_id'])
        ident = digest(action + json.dumps(data, sort_keys=True))
        path = self.queue / (ident + '.json')
        item = dict(action=action, data=data, attempts=0, next_attempt=0, status='pending')
        atomic(path, json.dumps(item, ensure_ascii=False))
        return self._send(path, item)

    def _send(self, path, item):
        try:
            result = self.request(item['action'], item['data'])
            path.unlink(missing_ok=True)
            return result
        except Error as exc:
            item['attempts'] += 1
            item['next_attempt'] = time.time() + min(300, 2 ** min(item['attempts'], 8))
            item['status'] = 'pending' if exc.status >= 500 or exc.status == 429 else 'blocked'
            item['error'] = {'status': exc.status, 'message': exc.message}
            atomic(path, json.dumps(item, ensure_ascii=False))
            if item['status'] == 'blocked':
                raise
            return {'saved': False, 'queued': True, 'request_id': item['data']['request_id'], 'status': 'pending', 'error': exc.message}

    def flush(self, force=False):
        import fcntl
        self.queue.mkdir(parents=True, exist_ok=True, mode=0o700)
        results = []
        with (self.queue / '.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            for path in sorted(self.queue.glob('*.json')):
                item = json.loads(path.read_text())
                if item['status'] != 'pending' or (not force and item['next_attempt'] > time.time()):
                    continue
                try:
                    results.append(self._send(path, item))
                except Error as exc:
                    results.append({'blocked': True, 'error': exc.message})
        return results
