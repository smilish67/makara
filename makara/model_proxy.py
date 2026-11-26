"""Local-only Honcho/LM Studio transport compatibility; bounded serial inference."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.request import Request,build_opener
from urllib.error import HTTPError
from .memory import NoRedirect

UPSTREAM='http://host.docker.internal:1234'
MODELS={'gemma-4-26b-a4b-it-qat','text-embedding-nomic-embed-text-v1.5'}
SERIAL=threading.BoundedSemaphore(1)
PENDING=threading.BoundedSemaphore(8)


def adapt(payload):
    # Honcho includes its actual schema in the prompt and validates the response.
    # LM Studio rejects json_object, but supports a generic JSON-object grammar.
    if payload.get('response_format',{}).get('type')=='json_object':
        payload=dict(payload,response_format={'type':'json_schema','json_schema':{'name':'honcho_json','schema':{'type':'object'}}})
    return payload


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def send(self,status,body):
        self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers()
        try:self.wfile.write(body)
        except (BrokenPipeError,ConnectionResetError):pass
    def do_POST(self):
        if self.path not in ['/v1/chat/completions','/v1/embeddings']:
            return self.send(404,b'{"error":"Unknown endpoint"}')
        if not PENDING.acquire(blocking=False):return self.send(503,b'{"error":"Local model queue full"}')
        acquired=False
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=2000000:return self.send(413,b'{"error":"Invalid request size"}')
            payload=json.loads(self.rfile.read(size))
            if payload.get('model') not in MODELS:return self.send(400,b'{"error":"Model not allowed"}')
            if payload.get('stream'):return self.send(400,b'{"error":"Streaming is not enabled for background memory"}')
            payload=adapt(payload)
            acquired=SERIAL.acquire(timeout=180)
            if not acquired:return self.send(503,b'{"error":"Local model busy"}')
            request=Request(UPSTREAM+self.path,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
            with build_opener(NoRedirect).open(request,timeout=240) as response:self.send(response.status,response.read())
        except HTTPError as exc:
            # Return errors to Honcho but do not log request bodies or credentials.
            self.send(exc.code,exc.read())
        except (ValueError,TypeError):self.send(400,b'{"error":"Invalid JSON request"}')
        except Exception:self.send(503,b'{"error":"Local model unavailable"}')
        finally:
            if acquired:SERIAL.release()
            PENDING.release()


def main():
    global MODELS
    MODELS=set(json.loads(os.environ.get('MAKARA_LOCAL_MODELS',json.dumps(sorted(MODELS)))))
    ThreadingHTTPServer(('0.0.0.0',8080),Handler).serve_forever()

if __name__=='__main__':main()
