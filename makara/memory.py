"""Optional Honcho v3 adapter. Isolated generations; never a canonical writer."""
import json
import time
import fcntl
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
import uuid
from .core import Error, atomic, digest, now, require


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Honcho:
    def __init__(self, config):
        self.config = config
        self.base = config['base_url'].rstrip('/')
        url = urlsplit(self.base)
        require(url.scheme in ['http', 'https'] and not url.username and not url.password and not url.query and not url.fragment, 'Invalid Honcho URL')
        require(url.hostname in ['127.0.0.1', 'localhost', '::1'] or (url.hostname in config.get('local_hosts', []) and url.scheme == 'http') or (url.scheme == 'https' and config.get('allow_remote') is True), 'Remote Honcho requires explicit allow_remote and HTTPS')

    def call(self, method, path, data=None, missing_ok=False):
        headers = {'Content-Type':'application/json'}
        if self.config.get('token_file'):
            headers['Authorization'] = 'Bearer ' + Path(self.config['token_file']).read_text().strip()
        req = Request(self.base+'/v3'+path, data=json.dumps(data).encode() if data is not None else None, headers=headers, method=method)
        try:
            with build_opener(NoRedirect).open(req, timeout=self.config.get('timeout', 180)) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except HTTPError as exc:
            if missing_ok and exc.code == 404: return {}
            raise Error(502, f'Honcho returned {exc.code}; memory unavailable')
        except (URLError, TimeoutError, ConnectionError):
            raise Error(503, 'Honcho unavailable; canonical documents remain available')


def states(store):
    return (store.private/'memory').glob('*.json')


def invalidate(store, scope, subject):
    for job in (store.private/'memory-jobs').glob('*.json'):
        record = json.loads(job.read_text())
        if record['scope'] == scope and record['subject'] == subject:
            job.unlink()  # Purge query and derived text; in-flight result cannot be committed.
    for path in states(store):
        item = json.loads(path.read_text())
        if item['scope'] == scope and item['subject'] == subject and item['status'] != 'deleted':
            item['status'] = 'delete_pending'
            atomic(path, json.dumps(item))


def sweep(store):
    config = store.config.get('honcho', {})
    if not config.get('enabled'): return
    api = Honcho(config)
    for path in states(store):
        item = json.loads(path.read_text())
        if item['status'] not in ['delete_pending', 'deleting', 'building']: continue
        workspace = item['workspace']
        try:
            if item['status'] != 'deleting':
                for session in item.get('sessions', []):
                    api.call('DELETE', f'/workspaces/{workspace}/sessions/{session}', missing_ok=True)
                api.call('DELETE', f'/workspaces/{workspace}', missing_ok=True)
                item['status'] = 'deleting'
                atomic(path, json.dumps(item))
            listing = api.call('POST','/workspaces/list',{'filters':{'id':workspace}})
            if not listing.get('items'):
                item['status'] = 'deleted'
                item['deleted_at'] = now()
                atomic(path, json.dumps(item))
        except Error as exc:
            item['last_error'] = exc.message
            atomic(path, json.dumps(item))


def eligible(store, principal, scope):
    sources = []
    for path in (store.private/'sources').glob('*.json'):
        source = json.loads(path.read_text())
        if source['scope'] != scope or source['subject'] != principal['subject']: continue
        if source.get('memory_retired') or source.get('origin') == 'orca_notification': continue
        if 'honcho' not in source.get('allowed_providers', []): continue
        try: store.available(principal, source)
        except Error: continue
        sources.append(source)
    return sorted(sources, key=lambda x:(x.get('uttered_at') or x['recorded_at'], x['recorded_at'], x['id']))


def perform_recall(store, principal, data):
    scope = data['scope']
    store.scope(principal, scope, 'read')
    config = store.config.get('honcho', {})
    if not config.get('enabled'):
        return {'status':'disabled','authority':'inference','canonical_override':False}
    require(scope in config.get('allowed_scopes', []), 'Scope not approved for Honcho', 403)
    require(isinstance(data.get('query'), str) and 0 < len(data['query']) <= 2000, 'Invalid memory query')
    # Query text also goes to Honcho; scope policy grants that explicit tool operation.
    api = Honcho(config)
    sources = eligible(store, principal, scope)
    if not sources: return {'status':'empty','authority':'inference','canonical_override':False}
    fingerprint = digest(json.dumps(sorted((s['id'], s['fingerprint']) for s in sources)))
    group = (scope, principal['subject'], principal.get('provider','local'))
    active = None
    for path in states(store):
        item = json.loads(path.read_text())
        if (item['scope'],item['subject'],item['provider']) != group: continue
        if item['status'] in ['delete_pending','deleting','building']:
            return {'status':'blocked_pending_deletion','authority':'inference','canonical_override':False}
        if item['status'] == 'ready':
            if item['fingerprint'] != fingerprint:
                if not set(item['source_ids']).issubset({s['id'] for s in sources}):
                    with store.lock(): invalidate(store,scope,principal['subject'])
                    return {'status':'blocked_pending_rebuild','authority':'inference','canonical_override':False}
                for source in sources:
                    if source['id'] in item['source_ids']: continue
                    require(len(source['text']) <= 25000, 'Source too large for Honcho; split explicitly')
                    api.call('POST',f"/workspaces/{item['workspace']}/sessions/sources/messages",{'messages':[{'peer_id':'agent' if source['text'].startswith('[assistant] ') else 'self','content':source['text'],'metadata':{'source_id':source['id']}}]})
                    item['source_ids'].append(source['id'])
                with store.lock():
                    persisted=store.load('memory',item['id'])
                    require(persisted['status']=='ready','Memory invalidated during upload',409)
                    item['fingerprint']=fingerprint
                    store.save('memory',item)
            active = item
    if active is None:
        ident = str(uuid.uuid4())
        active = dict(id=ident,workspace='makara-'+ident,scope=scope,subject=principal['subject'],provider=group[2],
                      fingerprint=fingerprint,status='building',sessions=['sources'],source_ids=[s['id'] for s in sources])
        with store.lock(): store.save('memory',active) # Persist remote identity before the first network side effect.
        workspace = active['workspace']
        try:
            workspace_data = {'id':workspace}
            if config.get('custom_instructions'):
                workspace_data['configuration'] = {'custom_instructions':config['custom_instructions']}
            api.call('POST','/workspaces',workspace_data)
            api.call('POST',f'/workspaces/{workspace}/peers',{'id':'self'})
            api.call('POST',f'/workspaces/{workspace}/peers',{'id':'agent','configuration':{'observe_me':False}})
            api.call('POST',f'/workspaces/{workspace}/sessions',{'id':'sources','peers':{'self':{'observe_me':True},'agent':{'observe_me':False,'observe_others':False}}})
            for source in sources:
                require(len(source['text']) <= 25000, 'Source too large for Honcho; split explicitly')
                api.call('POST',f'/workspaces/{workspace}/sessions/sources/messages',{'messages':[{'peer_id':'agent' if source['text'].startswith('[assistant] ') else 'self','content':source['text'],'metadata':{'source_id':source['id'],'uttered_at':source['uttered_at'],'occurred_at':source['occurred_at']}}]})
            with store.lock():
                persisted = store.load('memory', active['id'])
                require(persisted['status'] == 'building', 'Memory invalidated during upload', 409)
                active['status']='ready'
                store.save('memory',active)
        except Exception:
            with store.lock():
                active['status']='delete_pending'
                store.save('memory',active)
            raise
    context=api.call('GET',f"/workspaces/{active['workspace']}/peers/self/context?max_conclusions=8")
    progress=api.call('GET',f"/workspaces/{active['workspace']}/queue/status")
    if progress.get('pending_work_units') or progress.get('in_progress_work_units'):
        return {'status':'pending_derivation','authority':'inference','canonical_override':False, 'progress':progress}
    if 'representation' in context and not context['representation']:
        state='pending_derivation'
        if progress.get('completed_work_units',0)>0 and not progress.get('pending_work_units') and not progress.get('in_progress_work_units'):
            state='no_observations'
        return {'status':state,'authority':'inference','canonical_override':False}
    if data['query'] == 'Summarize recent interests, preferences, and unfinished work. Distinguish inferences from facts.':
        result=context
    else:
        result=api.call('POST',f"/workspaces/{active['workspace']}/peers/self/chat",{'query':data['query'],'reasoning_level':'minimal'})
    return {'status':'ready','authority':'inference','canonical_override':False,'compare_with_canonical':True,
            'source_ids':active['source_ids'],'result':result,'retrieved_at':now()}


def source_fingerprint(store, principal, scope):
    return digest(json.dumps(sorted((s['id'], s['fingerprint']) for s in eligible(store, principal, scope))))


def recall(store, principal, data):
    """Called under writer lock: enqueue only, never wait for an LLM or network."""
    scope = data['scope']
    store.scope(principal, scope, 'read')
    config = store.config.get('honcho', {})
    base = dict(authority='inference', canonical_override=False)
    if not config.get('enabled'): return dict(base, status='disabled')
    require(scope in config.get('allowed_scopes', []), 'Scope not approved for Honcho', 403)
    query = data.get('query')
    require(isinstance(query, str) and 0 < len(query) <= 2000, 'Invalid memory query')
    if not eligible(store, principal, scope): return dict(base, status='empty')
    fingerprint = source_fingerprint(store, principal, scope)
    ident = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([principal['id'],scope,query])))
    path = store.hidden('memory-jobs', ident)
    if path.exists():
        job = json.loads(path.read_text())
        if job['fingerprint'] == fingerprint:
            if job['status'] == 'ready' and time.time()-job.get('completed_epoch',0) < 300:
                return dict(job['result'], cached=True, generated_at=job['updated_at'])
            if job['status'] != 'ready':
                return dict(base, status=job['status'], job_id=ident, queued_at=job['created_at'], last_error=job.get('last_error'))
    job = dict(id=ident, principal_id=principal['id'], scope=scope, subject=principal['subject'], query=query,
               fingerprint=fingerprint, status='pending', created_at=now(), next_attempt=0)
    store.save('memory-jobs', job)
    return dict(base, status='pending', job_id=ident, queued_at=job['created_at'])


def process_jobs(store):
    """Single background consumer. Network/LLM calls never hold writer.lock."""
    if not store.config.get('honcho', {}).get('enabled'): return
    with (store.private/'memory-worker.lock').open('a') as lease:
        try: fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return
        with store.lock():
            ensure_jobs(store)
        sweep(store)
        for path in sorted((store.private/'memory-jobs').glob('*.json')):
            with store.lock():
                if not path.exists(): continue
                job = json.loads(path.read_text())
                if job['status'] in ['ready','no_observations'] or job.get('next_attempt',0) > time.time(): continue
                principal = next((p for p in store.principals.values() if p['id']==job['principal_id']), None)
                if principal is None:
                    path.unlink(); continue
                job.update(status='processing', updated_at=now())
                store.save('memory-jobs', job)
            try:
                result = perform_recall(store, principal, {'scope':job['scope'],'query':job['query']})
            except Exception as exc:
                result = {'status':'retry_pending'}
                job['last_error'] = exc.message if isinstance(exc, Error) else 'Memory worker failed'
            with store.lock():
                live_ids={s['id'] for s in eligible(store, principal, job['scope'])}
                # A deletion can race the very first remote workspace creation.
                # Block and purge any generation created after the original invalidation.
                for generation_path in states(store):
                    generation=json.loads(generation_path.read_text())
                    if generation['scope']==job['scope'] and generation['subject']==job['subject'] and generation['provider']==principal.get('provider','local') and generation['status'] in ('ready', 'building') and not set(generation['source_ids']).issubset(live_ids):
                        generation['status']='delete_pending'
                        atomic(generation_path,json.dumps(generation))
                if not path.exists(): continue
                current = json.loads(path.read_text())
                if current['fingerprint'] != job['fingerprint']: continue
                if source_fingerprint(store, principal, job['scope']) != job['fingerprint']:
                    path.unlink(); continue
                if result['status'] == 'ready':
                    job.update(status='ready', result=result, completed_epoch=time.time())
                else:
                    job.update(status=result.get('status','retry_pending'), next_attempt=time.time()+60)
                job['updated_at']=now()
                store.save('memory-jobs', job)
            break



DEFAULT_QUERY = 'Summarize recent interests, preferences, and unfinished work. Distinguish inferences from facts.'


def ensure_jobs(store, scope=None):
    """Under writer.lock; repair missing/stale jobs without a user recall request."""
    config = store.config.get('honcho', {})
    if not config.get('enabled'): return
    seen = set()
    for principal in store.principals.values():
        # Do not create owner/local copies of every cloud-visible memory.
        if principal.get('provider') not in ['openai', 'anthropic']: continue
        group = (principal['subject'], principal['provider'])
        for target in config.get('allowed_scopes', []):
            if scope is not None and scope != target: continue
            if target not in principal.get('read', []): continue
            if (*group, target) in seen: continue
            seen.add((*group, target))
            recall(store, principal, {'scope':target, 'query':DEFAULT_QUERY})


def queue_status(store, principal):
    counts = {}
    oldest = None
    for path in (store.private/'memory-jobs').glob('*.json'):
        item=json.loads(path.read_text())
        if item['principal_id'] != principal['id']: continue
        if item['scope'] not in principal.get('read', []): continue
        counts[item['status']]=counts.get(item['status'],0)+1
        if item['status'] != 'ready': oldest=min(oldest or item['created_at'],item['created_at'])
    return {'jobs':counts,'oldest_pending_at':oldest,'worker_concurrency':1,'blocks_canonical_reads':False}


def separate_orca_notices(store):
    """Run under the writer lock before serving. Keep original text and old workspaces."""
    from .capture import is_orca_notice
    marker = store.private / 'orca-notices-v1.json'
    if marker.exists():
        return
    excluded = set()
    for path in (store.private / 'sources').glob('*.json'):
        source = json.loads(path.read_text())
        if is_orca_notice(source):
            excluded.add(source['id'])
            if source.get('origin') != 'orca_notification':
                source['origin'] = 'orca_notification'
                store.save('sources', source)
    for path in states(store):
        generation = json.loads(path.read_text())
        if generation['status'] == 'ready' and excluded.intersection(generation['source_ids']):
            generation['status'] = 'superseded'
            store.save('memory', generation)
    # recall() compares the eligible fingerprint before returning any cached
    # result. Explicit source deletion still invalidates superseded workspaces.
    atomic(marker, json.dumps({'at': now(), 'notices': len(excluded)}))


def migrate_legacy_history(store):
    """Preserve exclusions made by the old document-coupled policy once, not forever."""
    marker = store.private / 'memory-decoupled-v1.json'
    if marker.exists(): return
    current, historic = set(), set()
    for root, target in [(store.vault, current), (store.private/'history', historic)]:
        for path in root.rglob('*.md'):
            text = path.read_text()
            if text.startswith('<!-- makara\n'):
                target.update(store.unpack(text)[0].get('sources', []))
    retired = []
    for ident in historic-current:
        path = store.hidden('sources', ident)
        if not path.exists(): continue
        source = store.load('sources', ident)
        if source.get('memory_retired'): continue
        source['memory_retired'] = {'reason':'Preserved legacy history-only exclusion', 'at':now()}
        store.save('sources', source)
        retired.append(ident)
    atomic(marker, json.dumps({'at':now(), 'retired_ids':retired}))


def pipeline_status(store, principal):
    result = {}
    for scope in principal.get('read', []):
        sources = eligible(store, principal, scope)
        generations = [json.loads(p.read_text()) for p in states(store)]
        generations = [g for g in generations if g['scope']==scope and g['subject']==principal['subject']
                       and g['provider']==principal.get('provider','local') and g['status']!='deleted']
        jobs = [json.loads(p.read_text()) for p in (store.private/'memory-jobs').glob('*.json')]
        jobs = [j for j in jobs if j['principal_id']==principal['id'] and j['scope']==scope]
        result[scope] = dict(eligible_sources=len(sources), latest_source_at=max((x['recorded_at'] for x in sources), default=None),
            uploaded_sources=max((len(g['source_ids']) for g in generations if g['status']=='ready'), default=0),
            generations=[{'status':g['status']} for g in generations],
            last_memory_check_at=max((j.get('updated_at',j['created_at']) for j in jobs),default=None),
            jobs=[{'status':j['status'],'last_error':j.get('last_error')} for j in jobs])
    return result
