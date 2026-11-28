#!/usr/bin/env python3
"""Prepare pinned, loopback-only Honcho infrastructure without selecting a model provider."""
import argparse
import json
from pathlib import Path
import secrets
import hashlib
import hmac
import base64
import urllib.request
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from makara.core import atomic

REVISION='b94a416c56f16ee4c840541c19e50bc668124e5e'
parser=argparse.ArgumentParser()
parser.add_argument('--home',type=Path,default=Path.home()/'.local/share/makara')
parser.add_argument('--local-model', help='Explicit OpenAI-compatible model ID')
parser.add_argument('--embedding-model', default='text-embedding-nomic-embed-text-v1.5')
args=parser.parse_args()
root=args.home/'honcho';root.mkdir(parents=True,exist_ok=True,mode=0o700)
installed = root/'compose.json'
if installed.exists() and 'go-embeddings' in json.loads(installed.read_text()).get('services', {}):
    raise SystemExit('OpenCode Go embedding profile already installed. Preserving its database and model routing.')
source=root/'upstream'
if not source.exists():
    subprocess.run(['git','clone','--no-checkout','https://github.com/plastic-labs/honcho.git',str(source)],check=True)
    subprocess.run(['git','-C',str(source),'checkout',REVISION],check=True)
else:
    current=subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()
    if current!=REVISION: raise SystemExit('Existing Honcho checkout differs; preserved')
cache=root/'tokenizer-cache';cache.mkdir(exist_ok=True)
for encoding in ['o200k_base','cl100k_base']:
    url='https://openaipublic.blob.core.windows.net/encodings/'+encoding+'.tiktoken'
    cached=cache/hashlib.sha1(url.encode()).hexdigest()
    if not cached.exists():
        with urllib.request.urlopen(url,timeout=30) as response: cached.write_bytes(response.read())
    cached.chmod(0o644)
password_file=root/'database.password'
if not password_file.exists():atomic(password_file,secrets.token_urlsafe(32))
password=password_file.read_text().strip()
secret_file=root/'jwt.secret'
if not secret_file.exists():atomic(secret_file,secrets.token_urlsafe(48))
secret=secret_file.read_text().strip()
encode=lambda value:base64.urlsafe_b64encode(value).rstrip(b'=')
header=encode(json.dumps({'alg':'HS256','typ':'JWT'},separators=(',',':')).encode())
payload=encode(json.dumps({'ad':True,'t':''},separators=(',',':')).encode())
unsigned=header+b'.'+payload
signature=encode(hmac.new(secret.encode(),unsigned,hashlib.sha256).digest())
atomic(root/'admin.token',(unsigned+b'.'+signature).decode())
environment={
 'DB_CONNECTION_URI':f'postgresql+psycopg://makara:{password}@database:5432/honcho',
 'CACHE_ENABLED':'true','CACHE_URL':'redis://redis:6379/0?suppress=true',
 'EMBED_MESSAGES':'false','DERIVER_ENABLED':'false','SUMMARY_ENABLED':'false',
 'PEER_CARD_ENABLED':'false','DREAM_ENABLED':'false','TELEMETRY_ENABLED':'false','SENTRY_ENABLED':'false',
 'AUTH_USE_AUTH':'true','AUTH_JWT_SECRET':secret,'TIKTOKEN_CACHE_DIR':'/tokenizer-cache',
}
if args.local_model:
    environment.update({'LLM_OPENAI_API_KEY':'local-not-a-secret','LLM_OPENAI_BASE_URL':'http://model-gateway:8080/v1',
      'DERIVER_ENABLED':'true','SUMMARY_ENABLED':'true','PEER_CARD_ENABLED':'false','DREAM_ENABLED':'false',
      'SUMMARY_MAX_TOKENS_SHORT':'1024','SUMMARY_MAX_TOKENS_LONG':'2048','DERIVER_WORKERS':'1','DERIVER_REPRESENTATION_BATCH_MAX_AGE_SECONDS':'60',
      'DERIVER_MAX_INPUT_TOKENS':'6000','DERIVER_REPRESENTATION_BATCH_TARGET_INPUT_TOKENS':'1024',
      'EMBED_MESSAGES':'true','EMBEDDING_VECTOR_DIMENSIONS':'768',
      'EMBEDDING_MAX_INPUT_TOKENS':'1024','EMBEDDING_MAX_CONCURRENT_EMBEDDINGS':'1','EMBEDDING_MAX_PENDING_EMBED_TASKS':'0',
      'EMBEDDING_MODEL_CONFIG':json.dumps({'model':args.embedding_model,'transport':'openai','dimensions_mode':'never',
         'encoding_format_mode':'float','max_batch_size':1,'timeout':180,
         'overrides':{'base_url':'http://model-gateway:8080/v1','api_key':'local-not-a-secret'}}),
      'DIALECTIC_MAX_INPUT_TOKENS':'6000'})
    model={'model':args.local_model,'transport':'openai','thinking_effort':'none','structured_output_mode':'json_object','max_output_tokens':4096,
           'overrides':{'base_url':'http://model-gateway:8080/v1','api_key':'local-not-a-secret',
                        'provider_params':{'timeout':240}}}
    for section in ['DERIVER','SUMMARY','DREAM_DEDUCTION','DREAM_INDUCTION']:
        environment[section+'_MODEL_CONFIG']=json.dumps(model)
    environment['DIALECTIC_LEVELS']=json.dumps({level:{'MODEL_CONFIG':model,'MAX_TOOL_ITERATIONS':2,'MAX_OUTPUT_TOKENS':2048} for level in ['minimal','low','medium','high','max']})
# Internal-only network prevents accidental outbound model calls in this prepared profile.
services={
 'database':{'image':'pgvector/pgvector:pg15','environment':{'POSTGRES_USER':'makara','POSTGRES_PASSWORD':password,'POSTGRES_DB':'honcho'},'volumes':['pgdata:/var/lib/postgresql/data'],'healthcheck':{'test':['CMD-SHELL','pg_isready -U makara -d honcho'],'interval':'5s','timeout':'5s','retries':10},'restart':'unless-stopped'},
 'redis':{'image':'redis:8.2','restart':'unless-stopped'},
 'api':{'build':{'context':str(source)},'entrypoint':['sh','docker/entrypoint.sh'],'environment':environment,'depends_on':{'database':{'condition':'service_healthy'},'redis':{'condition':'service_started'}},'restart':'unless-stopped'},
 'worker':{'build':{'context':str(source)},'entrypoint':['/app/.venv/bin/python','-m','src.deriver'],'environment':environment,'depends_on':['api'],'restart':'unless-stopped'},
}
for name in ['api','worker']: services[name]['volumes']=[str(cache)+':/tokenizer-cache:ro']
proxy=root/'proxy/nginx.conf'
atomic(proxy, 'events {}\nhttp { access_log off; resolver 127.0.0.11 valid=10s; server { listen 8080; set $honcho_api api; location / { proxy_pass http://$honcho_api:8000; proxy_read_timeout 300s; } } }\n')
services['gateway']={'image':'nginx:1.28-alpine','ports':['127.0.0.1:18766:8080'],'volumes':[str(proxy.parent)+':/etc/nginx/makara:ro'],'command':['nginx','-c','/etc/nginx/makara/nginx.conf','-g','daemon off;'],'networks':['default','edge'],'depends_on':['api'],'restart':'unless-stopped'}
if args.local_model:
    services['model-gateway']={'image':'makara-local:0.2','entrypoint':['python3','-m','makara.model_proxy'],
        'environment':{'MAKARA_LOCAL_MODELS':json.dumps([args.local_model,args.embedding_model])},
        'networks':['default','edge'],'restart':'unless-stopped','user':'1000:1000',
        'security_opt':['no-new-privileges:true'],'cap_drop':['ALL']}
compose={'name':'makara-honcho' ,'services':services,'volumes':{'pgdata':{}},'networks':{'default':{'internal':True},'edge':{}}}
atomic(root/'compose.json',json.dumps(compose,indent=2))
config_path=args.home/'config.json'
if config_path.exists():
    config=json.loads(config_path.read_text())
    config['honcho'].setdefault('token_file',str(root/'admin.token'))
    atomic(config_path,json.dumps(config,indent=2))
print('Prepared '+str(root/'compose.json')+('; local inference configured through model-only gateway.' if args.local_model else '; inference disabled; outbound network blocked.'))
