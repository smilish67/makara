import argparse
import json
import os
from pathlib import Path
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from .core import Store, Error, atomic, digest
from .workflow import KnowledgeStore
from .client import Client
import uuid
import threading


def serve(config_path, port, bind="127.0.0.1"):
    config = json.loads(config_path.read_text())
    store = KnowledgeStore(config['vault'], config_path.parent, config['principals'], config)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log credentials or submitted content.

        def reply(self, status, body):
            raw = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            self.connection.settimeout(10)
            try:
                if self.headers.get('Origin'):
                    raise Error(403, 'Browser origins are not supported')
                token = self.headers.get('Authorization', '').removeprefix('Bearer ')
                principal = store.auth(token)
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 1024 * 1024:
                    raise Error(413, 'Body must be between 1 byte and 1 MiB')
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise Error(400, 'Expected JSON object')
                if self.path == '/v1/health':
                    result = {'status': 'ready', 'external_transmission': config.get('honcho', {}).get('enabled', False), 'vault': config['vault']}
                else:
                    if not self.path.startswith('/v1/'):
                        raise Error(404, 'Unknown endpoint')
                    data.setdefault('request_id', str(uuid.uuid4()))
                    result = store.dispatch(principal, self.path[4:], data)
                self.reply(200, result)
            except Error as exc:
                self.reply(exc.status, {'error': exc.message})
            except (ValueError, KeyError, TypeError):
                self.reply(400, {'error': 'Invalid request fields'})
            except Exception:
                self.reply(500, {'error': 'Local storage error; request may need retry'})

    server = ThreadingHTTPServer((bind, port), Handler)
    stop = threading.Event()
    from .runtime import start_worker
    worker = start_worker(store, server.server_port, stop)
    print(f'Makara listening on http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', type=Path, default=Path.home() / '.local/share/makara')
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init')
    init.add_argument('--vault', type=Path, required=True)
    init.add_argument('--project', default='demo')
    server = sub.add_parser('serve')
    server.add_argument('--port', type=int, default=18765)
    server.add_argument('--bind', choices=['127.0.0.1','0.0.0.0'], default='127.0.0.1')
    for name in ['start','status']:
        command = sub.add_parser(name)
        command.add_argument('--port',type=int,default=18765)
    flush = sub.add_parser('flush')
    flush.add_argument('--token-file',type=Path,required=True)
    flush.add_argument('--port',type=int,default=18765)
    call = sub.add_parser('call')
    call.add_argument('action')
    call.add_argument('--data', type=Path, required=True)
    call.add_argument('--token-file', type=Path, required=True)
    call.add_argument('--port', type=int, default=18765)
    args = parser.parse_args()
    home = args.home.resolve()
    config_path = home / 'config.json'
    if args.command == 'init':
        if config_path.exists():
            parser.error('Already initialized; existing configuration was preserved')
        store = Store(args.vault, home, {})
        scope = 'projects/' + args.project
        store.scope({'write': [scope]}, scope, 'write')
        principals = {}
        for name in ['owner', 'agent-a', 'agent-b']:
            token = secrets.token_urlsafe(32)
            principal = dict(id=name, subject='self', read=['me', scope], write=['me', scope], approve=[], delete=[])
            if name == 'owner':
                principal.update(approve=['me', scope], delete=['me', scope])
            principals[digest(token)] = principal
            atomic(home / (name + '.token'), token)
        atomic(config_path, json.dumps(dict(vault=str(args.vault.resolve()), principals=principals, providers=['openai','anthropic','honcho'], stale_after_days=30, automation={}, verification={}, honcho={'enabled':False,'base_url':'http://127.0.0.1:18766','allowed_scopes':[],'allow_remote':False}), indent=2))
        for directory in ['me', scope + '/knowledge']:
            (args.vault / directory).mkdir(parents=True, exist_ok=True)
        print(f'Initialized. Credentials and private data: {home}. Vault: {args.vault.resolve()}')
    elif args.command == 'serve':
        serve(config_path, args.port, args.bind)
    elif args.command == 'start':
        from .runtime import start
        print(json.dumps(start(home,args.port)))
    elif args.command == 'status':
        print(json.dumps(Client(home,home/'owner.token',args.port).request('status',{})))
    elif args.command == 'flush':
        print(json.dumps(Client(home,args.token_file,args.port).flush(force=True)))
    else:
        data = json.loads(args.data.read_text())
        try:
            result = Client(home,args.token_file,args.port).submit(args.action,data)
            print(json.dumps(result,ensure_ascii=False))
            if isinstance(result,dict) and result.get('queued'):
                sys.exit(2)
        except Error as exc:
            print(json.dumps({'status':exc.status,'error':exc.message}),file=sys.stderr)
            if exc.status == 503:
                print('Not confirmed saved',file=sys.stderr)
            sys.exit(2 if exc.status >= 500 else 1)


if __name__ == '__main__':
    try:
        main()
    except Error as exc:
        print(json.dumps({'status':exc.status,'error':exc.message}),file=sys.stderr)
        sys.exit(1)
