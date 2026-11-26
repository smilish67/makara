import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


class Error(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)


def require(condition, message, status=400):
    if not condition:
        raise Error(status, message)


def atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Store:
    def __init__(self, vault, private, principals):
        self.vault, self.private = Path(vault).resolve(), Path(private).resolve()
        require(self.vault != self.private and self.vault not in self.private.parents,
                'Private storage must be outside vault')
        self.principals = principals
        self.vault.mkdir(parents=True, exist_ok=True)
        self.private.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextlib.contextmanager
    def lock(self):
        with (self.private / 'writer.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def auth(self, token):
        principal = self.principals.get(digest(token))
        require(principal is not None, 'Invalid credentials', 401)
        return principal

    def scope(self, principal, scope, action):
        require(isinstance(scope, str) and re.fullmatch(r'me|projects/[a-zA-Z0-9_-]+', scope), 'Invalid scope')
        require(scope in principal.get(action, []), 'Scope is not permitted', 403)

    def path(self, scope, key):
        require(isinstance(key, str) and re.fullmatch(r'(knowledge/)?[a-zA-Z0-9_-]+', key), 'Invalid document key')
        path = self.vault / scope / (key + '.md')
        require(not any(p.is_symlink() for p in [path, *path.parents]), 'Symlinks are not allowed')
        require(self.vault in path.resolve().parents, 'Invalid path')
        return path

    def unpack(self, text):
        try:
            header, body = text.split('\n-->\n', 1)
            return json.loads(header.removeprefix('<!-- makara\n')), body
        except (ValueError, TypeError):
            raise Error(409, 'Document metadata was edited; repair before updating')

    def pack(self, metadata, body):
        return '<!-- makara\n' + json.dumps(metadata, ensure_ascii=False, indent=2) + '\n-->\n' + body

    def read(self, principal, scope, key):
        self.scope(principal, scope, 'read')
        path = self.path(scope, key)
        require(path.exists(), 'Document not found', 404)
        text = path.read_text()
        metadata, body = self.unpack(text)
        require(metadata.get('subject') == principal['subject'], 'Subject mismatch', 403)
        require(not any(self.hidden('tombstones', ident).exists() for ident in metadata.get('sources', [])), 'Document references a deleted source', 410)
        return dict(metadata=metadata, body=body, version=digest(text), scope=scope, key=key,
                    observed_at=now(), modified_at=dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc).isoformat())

    def hidden(self, category, ident):
        require(isinstance(ident, str) and re.fullmatch(r'[a-f0-9-]{36}', ident), 'Invalid ID')
        return self.private / category / (ident + '.json')

    def load(self, category, ident):
        path = self.hidden(category, ident)
        require(path.exists(), 'Item not found', 404)
        return json.loads(path.read_text())

    def save(self, category, item):
        atomic(self.hidden(category, item['id']), json.dumps(item, ensure_ascii=False, indent=2))

    def source(self, principal, data):
        scope = data['scope']
        self.scope(principal, scope, 'write')
        require(data.get('consent') is True, 'Explicit collection consent is required')
        require(isinstance(data.get('text'), str) and data['text'].strip(), 'Source text is required')
        require(isinstance(data.get('idempotency_key'), str) and data['idempotency_key'], 'Idempotency key required')
        identity = [principal['subject'], principal['id'], scope, data['idempotency_key']]
        ident = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(identity)))
        fingerprint = digest(json.dumps({k:v for k,v in data.items() if k != 'request_id'}, sort_keys=True, ensure_ascii=False))
        require(not self.hidden('tombstones', ident).exists(), 'Source was deleted', 410)
        path = self.hidden('sources', ident)
        if path.exists():
            previous = self.load('sources', ident)
            require(previous['fingerprint'] == fingerprint, 'Idempotency key reused with different content', 409)
            return previous
        item = dict(id=ident, scope=scope, actor=principal['id'], subject=principal['subject'],
                    recorded_at=now(), uttered_at=data.get('uttered_at'), occurred_at=data.get('occurred_at'),
                    text=data['text'], fingerprint=fingerprint, consent=True)
        if isinstance(data.get('conversation'), dict):
            item['conversation'] = {k:str(v)[:200] for k,v in data['conversation'].items() if k in ['session_id','turn_id','role']}
        self.save('sources', item)
        return item

    def propose(self, principal, data):
        scope, key = data['scope'], data['key']
        self.scope(principal, scope, 'write')
        self.scope(principal, scope, 'read')
        self.path(scope, key)
        require(data.get('kind') in ['fact', 'interest', 'goal', 'inference', 'state', 'decision', 'preference'], 'Invalid kind')
        require(data.get('confirmation') in ['observed', 'user_confirmed', 'unverified'], 'Invalid confirmation')
        require(data['kind'] != 'goal' or data['confirmation'] == 'user_confirmed', 'Goals require explicit user confirmation')
        require(isinstance(data.get('body'), str) and data['body'].strip(), 'Body is required')
        require(isinstance(data.get('reason'), str) and data['reason'].strip(), 'Change reason is required')
        require(isinstance(data.get('sources'), list) and data['sources'], 'Source IDs required')
        for ident in data['sources']:
            source = self.load('sources', ident)
            require(not self.hidden('tombstones', ident).exists(), 'Source deleted', 410)
            require(source['scope'] == scope, 'Sources must belong to the same scope')
            require(source['subject'] == principal['subject'], 'Subject mismatch', 403)
        require('base_version' in data, 'base_version required; use null for new document')
        item = {k: data.get(k) for k in ['scope', 'key', 'kind', 'confirmation', 'body', 'reason', 'sources', 'base_version', 'occurred_at', 'uttered_at']}
        item.update(id=str(uuid.uuid4()), actor=principal['id'], subject=principal['subject'], recorded_at=now(), status='pending')
        self.save('proposals', item)
        return item

    def approve(self, principal, ident):
        item = self.load('proposals', ident)
        scope, key = item['scope'], item['key']
        self.scope(principal, scope, 'approve')
        self.scope(principal, scope, 'read')
        require(item['subject'] == principal['subject'], 'Subject mismatch', 403)
        path = self.path(scope, key)
        old = path.read_text() if path.exists() else None
        # Recovery after a process interruption between document and proposal writes.
        if old and self.unpack(old)[0].get('proposal_id') == ident:
            item['status'] = 'applied'
            self.save('proposals', item)
            return self.read(principal, scope, key)
        require(item['status'] == 'pending', 'Proposal is no longer pending', 409)
        require((digest(old) if old is not None else None) == item['base_version'], 'Document changed; read and propose again', 409)
        for source_id in item['sources']:
            self.load('sources', source_id)
            require(not self.hidden('tombstones', source_id).exists(), 'Source deleted', 410)
        previous = self.unpack(old)[0] if old else None
        if old:
            atomic(self.private / 'history' / scope / key / (digest(old) + '.md'), old)
        metadata = {k: item[k] for k in ['kind', 'confirmation', 'sources', 'subject', 'occurred_at', 'uttered_at']}
        metadata.update(id=str(uuid.uuid4()), proposal_id=ident, recorded_at=now(), approved_by=principal['id'],
                        visibility=scope, supersedes=previous['id'] if previous else None, reason=item['reason'])
        atomic(path, self.pack(metadata, item['body']))
        item['status'] = 'applied'
        self.save('proposals', item)
        return self.read(principal, scope, key)

    def context(self, principal, scopes, query=''):
        results = []
        for scope in scopes:
            self.scope(principal, scope, 'read')
            for path in sorted((self.vault / scope).rglob('*.md')):
                if not path.read_text().startswith('<!-- makara\n'):
                    continue
                key = str(path.relative_to(self.vault / scope).with_suffix(''))
                try:
                    doc = self.read(principal, scope, key)
                except Error as exc:
                    if exc.status == 410:
                        continue
                    raise
                if not query or query.casefold() in doc['body'].casefold():
                    results.append(doc)
        return dict(documents=results, retrieved_at=now(), memory_provider='disabled', freshness='Check modified_at and source dates; retrieval does not verify truth')

    def delete_source(self, principal, ident):
        source = self.load('sources', ident)
        self.scope(principal, source['scope'], 'delete')
        require(source['subject'] == principal['subject'], 'Subject mismatch', 403)
        # Tombstone first: an interrupted deletion can safely be retried, never reingested.
        self.save('tombstones', dict(id=ident, deleted_at=now(), scope=source['scope'], subject=source['subject']))
        for path in (self.private / 'proposals').glob('*.json'):
            item = json.loads(path.read_text())
            if ident in item['sources']:
                path.unlink()
        for root in [self.vault, self.private / 'history']:
            for path in root.rglob('*.md'):
                require(not path.is_symlink(), 'Symlinks are not allowed')
                text = path.read_text()
                if not text.startswith('<!-- makara\n'):
                    continue
                metadata, _ = self.unpack(text)
                if ident in metadata.get('sources', []):
                    path.unlink()
        # Pending retries must not retain deleted raw source contents.
        for path in (self.private / 'outbox').rglob('*.json'):
            queued = json.loads(path.read_text())
            data = queued.get('data', {})
            if ident in data.get('sources', []) or data.get('id') == ident or (queued.get('action') == 'source' and data.get('scope') == source['scope'] and data.get('text') == source['text']):
                path.unlink()
        self.hidden('sources', ident).unlink()
        return dict(deleted=True, propagation='Local documents, proposals and history purged; no external memory provider configured')

    def dispatch(self, principal, action, data):
        with self.lock():
            if action == 'source': return self.source(principal, data)
            if action == 'propose': return self.propose(principal, data)
            if action == 'approve': return self.approve(principal, data['id'])
            if action == 'read': return self.read(principal, data['scope'], data['key'])
            if action in ['context', 'search']: return self.context(principal, data['scopes'], data.get('query', ''))
            if action == 'delete-source': return self.delete_source(principal, data['id'])
            if action == 'inspect-source':
                item = self.load('sources', data['id'])
                self.scope(principal, item['scope'], 'read')
                require(not self.hidden('tombstones', data['id']).exists(), 'Source deleted', 410)
                return item
            if action == 'proposals':
                self.scope(principal, data['scope'], 'read')
                return [json.loads(p.read_text()) for p in (self.private / 'proposals').glob('*.json') if json.loads(p.read_text())['scope'] == data['scope']]
            raise Error(404, 'Unknown action')
