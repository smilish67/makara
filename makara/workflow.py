"""Owner attestations, deterministic automation, journaled requests and scoped context."""
import datetime as dt
import json
from pathlib import Path
import re
import subprocess
import time
import uuid
from .core import Store, Error, atomic, digest, now, require


class KnowledgeStore(Store):
    def __init__(self, vault, private, principals, config=None):
        super().__init__(vault, private, principals)
        self.config = config or {}
        registry=self.private/'capture-projects.json'
        if registry.exists():
            for record in json.loads(registry.read_text()).values():
                for principal in self.principals.values():
                    if principal['id']==record['actor'] and principal['subject']==record['subject'] and principal['id'] in self.config.get('capture',{}).get('agents',[]):
                        self.grant_capture(principal,record['scope'])

    def grant_capture(self, principal, scope):
        policy=self.config.get('capture',{})
        require(principal['id'] in policy.get('agents',[]), 'Automatic project capture is not enabled for this agent',403)
        for action in ['read','write']:
            if scope not in principal.setdefault(action,[]): principal[action].append(scope)
        for owner in self.principals.values():
            if owner['id']=='owner' and owner['subject']==principal['subject']:
                for action in ['read','write','approve','delete']:
                    if scope not in owner.setdefault(action,[]):owner[action].append(scope)
        honcho=self.config.setdefault('honcho',{})
        if 'honcho' in policy.get('allowed_providers',[]) and scope not in honcho.setdefault('allowed_scopes',[]):
            honcho['allowed_scopes'].append(scope)

    def register_capture(self, principal, data):
        from .capture import project_scope
        policy=self.config.get('capture',{})
        require(policy.get('enabled') and principal['id'] in policy.get('agents',[]), 'Automatic project capture is not enabled for this agent',403)
        root=data.get('project_root')
        require(isinstance(root,str) and root.startswith('/') and len(root)<=4096 and '\x00' not in root and '..' not in Path(root).parts, 'Invalid project root')
        root=str(Path(root))
        for excluded in policy.get('excluded_paths',[]):
            require(root!=excluded and Path(excluded) not in Path(root).parents,'Project excluded from capture',403)
        scope=project_scope(root)
        self.grant_capture(principal,scope)
        path=self.private/'capture-projects.json'
        records=json.loads(path.read_text()) if path.exists() else {}
        key=digest(principal['subject']+':'+principal['id']+':'+root)
        records[key]={'actor':principal['id'],'subject':principal['subject'],'scope':scope,'project_root':root}
        atomic(path,json.dumps(records,ensure_ascii=False,indent=2))
        return records[key]

    def available(self, principal, source):
        require(source['subject'] == principal['subject'], 'Subject mismatch', 403)
        provider = principal.get('provider', 'local')
        if provider != 'local':
            require(provider in source.get('allowed_providers', []), 'Source is not approved for this model provider', 403)
        require(not self.hidden('tombstones', source['id']).exists(), 'Source deleted', 410)

    def read(self, principal, scope, key):
        doc = super().read(principal, scope, key)
        for ident in doc['metadata']['sources']:
            self.available(principal, self.load('sources', ident))
        age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(doc['metadata']['recorded_at'])).total_seconds()
        doc['stale'] = age > self.config.get('stale_after_days', 30) * 86400
        doc['corrections'] = [{'source_id':ident, **self.load('sources', ident)['memory_retired']}
                              for ident in doc['metadata']['sources'] if self.load('sources', ident).get('memory_retired')]
        if doc['corrections']: doc['stale'] = True
        doc['authority'] = 'canonical_markdown'
        return doc

    def source(self, principal, data):
        require(set(data.get('allowed_providers', [])) <= set(self.config.get('providers', ['openai', 'anthropic', 'honcho'])), 'Unknown provider')
        item = super().source(principal, data)
        if 'allowed_providers' not in item:
            from .capture import is_orca_notice
            if is_orca_notice(item):
                item['origin'] = 'orca_notification'
            item['allowed_providers'] = data.get('allowed_providers', [])
            self.save('sources', item)
        return item

    def propose(self, principal, data):
        for ident in data.get('sources', []):
            source = self.load('sources', ident)
            self.available(principal, source)
            require(not source.get('memory_retired'), 'Use replacement source for corrected knowledge', 409)
        if data.get('request_id'):
            ident = str(uuid.uuid5(uuid.NAMESPACE_URL, principal['id'] + data['request_id']))
            existing = self.hidden('proposals', ident)
            if existing.exists():
                item = self.load('proposals', ident)
                require(item.get('request_hash') == digest(json.dumps(data, sort_keys=True)), 'Request ID reused', 409)
                return item
        item = super().propose(principal, data)
        if data.get('request_id'):
            original = self.hidden('proposals', item['id'])
            item['id'] = ident
            item['request_hash'] = digest(json.dumps(data, sort_keys=True))
            self.save('proposals', item)
            original.unlink()
        return item

    def attest(self, principal, data):
        source = self.load('sources', data['id'])
        self.scope(principal, source['scope'], 'approve')
        self.available(principal, source)
        require(data['kind'] in ['fact', 'interest', 'state', 'goal', 'decision', 'preference'], 'Invalid attestation kind')
        source['attestation'] = dict(kind=data['kind'], actor=principal['id'], at=now())
        self.save('sources', source)
        return source

    def approve(self, principal, ident):
        item = self.load('proposals', ident)
        self.scope(principal, item['scope'], 'approve')
        require(item['subject'] == principal['subject'], 'Subject mismatch', 403)
        for source_id in item['sources']:
            source = self.load('sources', source_id)
            self.available(principal, source)
            require(not source.get('memory_retired'), 'Use replacement source for corrected knowledge', 409)
        # Markdown is a projection, not a command to erase conversational memory.
        result = super().approve(principal, ident)
        return result

    def auto_apply(self, principal, data):
        item = self.load('proposals', data['id'])
        self.scope(principal, item['scope'], 'write')
        self.scope(principal, item['scope'], 'read')
        require(item['actor'] == principal['id'], 'Only own proposals may be automated', 403)
        for ident in item['sources']:
            self.available(principal, self.load('sources', ident))
        # A writer may publish only its own changes within its existing scope.
        # Reading the current document enforces provider/subject restrictions too.
        if self.path(item['scope'], item['key']).exists():
            self.read(principal, item['scope'], item['key'])
        service = dict(principal, id='policy:' + principal['id'], approve=[item['scope']])
        return self.approve(service, item['id'])

    def manual_edit(self, principal, data):
        scope, key = data['scope'], data['key']
        self.scope(principal, scope, 'approve')
        old = self.read(principal, scope, key)
        require(old['version'] == data['base_version'], 'Document changed; reload before saving', 409)
        provider_sets = [set(self.load('sources', ident).get('allowed_providers', [])) for ident in old['metadata']['sources']]
        allowed = sorted(set.intersection(*provider_sets)) if provider_sets else []
        source = self.source(principal, dict(scope=scope, consent=True, text=data['body'],
                            idempotency_key='edit:' + data['request_id'],
                            allowed_providers=allowed))
        proposal = self.propose(principal, dict(scope=scope, key=key, body=data['body'],
                       kind=old['metadata']['kind'], confirmation='user_confirmed', sources=[source['id']],
                       reason='Explicit owner edit', base_version=old['version'], request_id=data['request_id']))
        return self.approve(principal, proposal['id'])

    def retire_source(self, principal, data):
        """Explicit correction, separate from editing a wiki page."""
        old = self.load('sources', data['id'])
        new = self.load('sources', data['replacement_id'])
        self.scope(principal, old['scope'], 'write')
        self.scope(principal, old['scope'], 'read')
        self.available(principal, old)
        self.available(principal, new)
        require(old['id'] != new['id'] and old['scope'] == new['scope'], 'Correction must reference a different source in the same scope')
        require(not new.get('memory_retired'), 'Replacement source is retired', 409)
        require(set(old.get('allowed_providers', [])) <= set(new.get('allowed_providers', [])),
                'Replacement must be visible to the original providers; do not broaden consent to bypass this', 403)
        require(isinstance(data.get('reason'), str) and data['reason'].strip(), 'Correction reason required')
        if old.get('memory_retired'):
            require(old['memory_retired'].get('replacement_id') == new['id'], 'Source already replaced', 409)
            return {'id':old['id'], 'retired':True}
        from .memory import invalidate, ensure_jobs
        invalidate(self, old['scope'], old['subject'])
        old['memory_retired'] = dict(replacement_id=new['id'], reason=data['reason'], at=now())
        self.save('sources', old)
        ensure_jobs(self, old['scope'])
        return {'id':old['id'], 'retired':True, 'replacement_id':new['id'], 'memory':'rebuild_queued'}

    def verify_work(self, principal, data):
        scope = data['scope']
        self.scope(principal, scope, 'verify')
        job = self.config.get('verification', {}).get(scope, {}).get(data['job'])
        require(job is not None, 'Unknown verification job')
        # Commands are owner-configured, never taken from the agent request.
        with subprocess.Popen(job['argv'], cwd=job['cwd'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              start_new_session=True) as process:
            try:
                output, _ = process.communicate(timeout=min(job.get('timeout', 60), 120))
            except subprocess.TimeoutExpired:
                import os, signal
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                raise Error(408, 'Verification timed out')
        require(process.returncode == 0, 'Verification failed; no completion was recorded', 409)
        text = '# 검증 결과\n\n' + data['job'] + ' 명령이 성공했습니다.\n\n' + '```text\n' + output.decode(errors='replace')[-12000:] + '\n```\n'
        source = self.source(principal, dict(scope=scope, text=text, consent=True,
                         idempotency_key='verification:' + data['request_id'], allowed_providers=job.get('allowed_providers', [])))
        source['attestation'] = dict(kind='state', actor='verification-service', at=now(), job=data['job'])
        self.save('sources', source)
        return source

    def context(self, principal, scopes, query='', max_chars=16000, limit=12):
        require(isinstance(scopes, list) and len(scopes) <= 20, 'Invalid scopes')
        require(isinstance(query, str) and len(query) <= 1000, 'Invalid query')
        require(isinstance(max_chars, int) and 100 <= max_chars <= 100000, 'Invalid character budget')
        require(isinstance(limit, int) and 1 <= limit <= 100, 'Invalid limit')
        docs = []
        for scope in scopes:
            self.scope(principal, scope, 'read')
            for path in sorted((self.vault / scope).rglob('*.md')):
                require(not path.is_symlink(), 'Symlinks are not allowed')
                if not path.read_text().startswith('<!-- makara\n'):
                    continue
                key = str(path.relative_to(self.vault / scope).with_suffix(''))
                try:
                    doc = self.read(principal, scope, key)
                except Error as exc:
                    if exc.status in [403, 410]:
                        continue
                    raise
                terms = query.casefold().split()
                score = sum(doc['body'].casefold().count(term) for term in terms)
                if doc.get('corrections'): continue
                if query and not score:
                    continue
                doc['relevance'] = score
                docs.append(doc)
        docs.sort(key=lambda d: (-d['relevance'], d['stale'], d['key'] not in ['now', 'state', 'goals', 'decisions']))
        result, used = [], 0
        for doc in docs[:limit]:
            remaining = max_chars - used
            if remaining <= 0:
                break
            doc['body_truncated'] = len(doc['body']) > remaining
            doc['body'] = doc['body'][:remaining]
            used += len(doc['body'])
            result.append(doc)
        return dict(documents=result, retrieved_at=now(), truncated=len(result) < len(docs) or any(x['body_truncated'] for x in result),
                    body_chars=used, memory_provider='separate_optional_layer', freshness='stale is age-based, not a truth guarantee')

    def delete_source(self, principal, ident):
        if self.hidden('tombstones', ident).exists() and not self.hidden('sources', ident).exists():
            tombstone = self.load('tombstones', ident)
            self.scope(principal, tombstone['scope'], 'delete')
            require(tombstone['subject'] == principal['subject'], 'Subject mismatch', 403)
            return {'deleted': True}
        source = self.load('sources', ident)
        self.scope(principal, source['scope'], 'delete')
        require(source['subject'] == principal['subject'], 'Subject mismatch', 403)
        from .memory import invalidate
        # Only sources approved for Honcho can enter a memory generation.
        sent_to_memory = 'honcho' in source.get('allowed_providers', [])
        if sent_to_memory:
            invalidate(self, source['scope'], source['subject'])
        result = super().delete_source(principal, ident)
        from .memory import ensure_jobs
        if sent_to_memory:
            ensure_jobs(self, source['scope'])
        # Receipts contain no source body; purge retries/proposals via source tombstone checks.
        result['propagation'] = ('Local purge complete; external memory blocked until rebuild/deletion finishes'
                                 if sent_to_memory else 'Local purge complete; source excluded from Honcho')
        return result

    def dispatch(self, principal, action, data):
        from .client import MUTATIONS
        with self.lock():
            receipt = None
            if action in MUTATIONS:
                require(isinstance(data.get('request_id'), str) and 1 <= len(data['request_id']) <= 200, 'request_id required')
                ident = str(uuid.uuid5(uuid.NAMESPACE_URL, principal['id'] + ':' + data['request_id']))
                receipt = self.hidden('receipts', ident)
                fingerprint = digest(json.dumps([action, data], sort_keys=True))
                if receipt.exists():
                    record = json.loads(receipt.read_text())
                    require(record['fingerprint'] == fingerprint, 'Request ID reused', 409)
                    return dict(replayed=True, applied=True, id=record.get('result_id'), result_id=record.get('result_id'), action=action)
            if action == 'capture-register': return self.register_capture(principal,data)
            if action == 'capture-source':
                registered=self.register_capture(principal,data)
                require(set(data.get('allowed_providers',[]))<=set(self.config['capture'].get('allowed_providers',[])), 'Capture provider not permitted',403)
                data=dict(data,scope=registered['scope'])
            if action in ['source','capture-source']:
                result = self.source(principal, data)
                if 'honcho' in result.get('allowed_providers', []) and self.config.get('honcho', {}).get('enabled') and data['scope'] in self.config.get('honcho', {}).get('allowed_scopes', []):
                    from .memory import recall
                    recall(self, principal, {'scope':data['scope'],'query':'Summarize recent interests, preferences, and unfinished work. Distinguish inferences from facts.'})
            elif action == 'propose':
                result = self.propose(principal, data)
                document = self.auto_apply(principal, {'id': result['id']})
                result = dict(self.load('proposals', result['id']), applied=True, version=document['version'])
            elif action == 'approve': result = self.approve(principal, data['id'])
            elif action == 'auto-apply': result = self.auto_apply(principal, data)
            elif action == 'attest': result = self.attest(principal, data)
            elif action == 'manual-edit': result = self.manual_edit(principal, data)
            elif action == 'verify-work': result = self.verify_work(principal, data)
            elif action == 'retire-source': result = self.retire_source(principal, data)
            elif action == 'delete-source': result = self.delete_source(principal, data['id'])
            elif action == 'read': result = self.read(principal, data['scope'], data['key'])
            elif action in ['context', 'search']:
                result = self.context(principal, data['scopes'], data.get('query', ''), data.get('max_chars', 16000), data.get('limit', 12))
            elif action == 'inspect-source':
                result = self.load('sources', data['id'])
                self.scope(principal, result['scope'], 'read')
                self.available(principal, result)
            elif action == 'source-feed':
                self.scope(principal, data['scope'], 'read')
                limit = data.get('limit', 30)
                require(isinstance(limit, int) and 1 <= limit <= 100, 'Invalid limit')
                cursor = data.get('after', '')
                require(isinstance(cursor, str), 'Invalid cursor')
                items = []
                for path in (self.private/'sources').glob('*.json'):
                    item = json.loads(path.read_text())
                    if item['scope'] != data['scope'] or item.get('memory_retired'): continue
                    try: self.available(principal, item)
                    except Error: continue
                    item_cursor = item['recorded_at'] + '|' + item['id']
                    if item_cursor <= cursor: continue
                    item = dict(item, cursor=item_cursor, text_truncated=len(item['text']) > 12000)
                    item['text'] = item['text'][:12000]
                    items.append(item)
                items.sort(key=lambda x:x['cursor'])
                chosen = items[:limit]
                result = {'items':chosen, 'next_cursor':chosen[-1]['cursor'] if chosen else cursor,
                          'has_more':len(items)>limit}
            elif action == 'proposals':
                self.scope(principal, data['scope'], 'read')
                result = []
                for p in (self.private / 'proposals').glob('*.json'):
                    item = json.loads(p.read_text())
                    if item['scope'] != data['scope'] or item['subject'] != principal['subject']:
                        continue
                    try:
                        for source in item['sources']:
                            self.available(principal, self.load('sources', source))
                    except Error:
                        continue
                    result.append(item)
            elif action == 'reject':
                result = self.load('proposals', data['id'])
                self.scope(principal, result['scope'], 'approve')
                require(result['status'] == 'pending', 'Only pending proposals can be rejected', 409)
                result.update(status='rejected', rejection_reason=data['reason'])
                self.save('proposals', result)
            elif action == 'memory':
                from .memory import recall
                result = recall(self, principal, data)
            elif action == 'status':
                from .memory import queue_status, pipeline_status
                result = dict(ready=True, memory_enabled=self.config.get('honcho', {}).get('enabled', False),
                              identity=principal['id'], scopes=principal.get('read', []), provider=principal.get('provider', 'local'), memory_queue=queue_status(self, principal), pipeline=pipeline_status(self, principal))
                registry=self.private/'capture-projects.json'
                records=json.loads(registry.read_text()).values() if registry.exists() else []
                result['capture']={'automatic_projects':bool(self.config.get('capture',{}).get('enabled') and principal['id'] in self.config.get('capture',{}).get('agents',[])),
                                   'projects':[x for x in records if x['subject']==principal['subject'] and x['scope'] in principal.get('read',[])],
                                   'wiki_updates':'user_requested'}
            else: raise Error(404, 'Unknown action')
            if receipt:
                atomic(receipt, json.dumps(dict(fingerprint=fingerprint, action=action, result_id=result.get('id') if isinstance(result, dict) else None, at=now())))
            return result
