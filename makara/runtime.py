"""Background retries, private review projections and process lifecycle."""
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from .client import Client
from .core import atomic


def tick(store, port):
    # Only retry requests that belong to a configured token path. Never persist tokens in queue records.
    for token in store.private.glob('*.token'):
        Client(store.private, token, port).flush()


def start_worker(store, port, stop):
    from .memory import migrate_legacy_history, separate_orca_notices
    with store.lock():
        migrate_legacy_history(store)
        separate_orca_notices(store)
    def loop():
        while not stop.wait(5):
            try:
                tick(store, port)
            except Exception:
                # A private status marker without source contents or credential details.
                atomic(store.private/'worker-status.json', json.dumps({'status':'retry_pending','at':time.time()}))
    def memory_loop():
        from .memory import process_jobs
        while not stop.wait(5):
            try: process_jobs(store)
            except Exception:
                atomic(store.private/'memory-worker-status.json', json.dumps({'status':'retry_pending','at':time.time()}))
    threading.Thread(target=memory_loop, daemon=True).start()
    thread=threading.Thread(target=loop,daemon=True)
    thread.start()
    return thread


def start(home, port):
    from .core import Error
    home=Path(home)
    client=Client(home,home/'owner.token',port)
    try:
        return client.request('health',{})
    except Error:
        pass
    log=home/'server.log'
    if not log.exists(): atomic(log,'')
    with log.open('ab') as stream:
        process=subprocess.Popen([sys.executable,'-m','makara.cli','--home',str(home),'serve','--port',str(port)],
                                 stdout=stream,stderr=stream,stdin=subprocess.DEVNULL,start_new_session=True)
    atomic(home/'server.pid',str(process.pid))
    for _ in range(30):
        if process.poll() is not None: raise Error(503,'Server exited; inspect private server.log')
        try: return client.request('health',{})
        except Error: time.sleep(.1)
    raise Error(503,'Server startup timed out')
