"""Project identity and explicit local capture policy; no transcript discovery."""
import json
import os
from pathlib import Path
import re
import subprocess
import tomllib
from .core import digest


def plugin_enabled(policy, cwd=None):
    selector=policy.get('require_plugin')
    if not selector: return True
    config=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))/'config.toml'
    try: enabled=tomllib.loads(config.read_text()).get('plugins',{}).get(selector,{}).get('enabled') is True
    except (OSError,ValueError): return False
    if cwd is not None:
        directory=Path(cwd).resolve()
        for parent in [*reversed(directory.parents),directory]:
            local=parent/'.codex/config.toml'
            if not local.exists(): continue
            try:
                value=tomllib.loads(local.read_text()).get('plugins',{}).get(selector,{}).get('enabled')
                if value is not None: enabled=value is True
            except (OSError,ValueError): return False
    return enabled


def resolve_target(policy, cwd):
    cwd=Path(cwd).resolve()
    if not plugin_enabled(policy,cwd): return None
    for root in policy.get('excluded_paths',[]):
        excluded=Path(root).expanduser().resolve()
        if cwd==excluded or excluded in cwd.parents: return None
    projects=policy.get('projects',{})
    target=projects.get(str(cwd))
    if target is None:
        parents=[(root,item) for root,item in projects.items()
                 if item.get('include_children') and Path(root).resolve() in cwd.parents]
        if parents: target=max(parents,key=lambda pair:len(Path(pair[0]).parts))[1]
    if target is not None: return dict(target) if target.get('enabled') else None
    if policy.get('mode')!='all_projects': return None
    try:
        root=subprocess.check_output(['git','-C',str(cwd),'rev-parse','--show-toplevel'],stderr=subprocess.DEVNULL,text=True,timeout=2).strip()
    except (OSError,subprocess.SubprocessError): root=str(cwd)
    # A child of an explicitly configured root must use its existing project identity.
    if root in projects:
        target=projects[root]
        return dict(target) if target.get('enabled') else None
    return {'enabled':True,'project_root':root,'allowed_providers':policy.get('allowed_providers',['openai','honcho'])}


def project_scope(root):
    label=re.sub('[^a-zA-Z0-9_-]+','-',Path(root).name).strip('-').lower()[:40] or 'project'
    return 'projects/'+label+'-'+digest(root)[:10]


def sanitize(text):
    text=re.sub(r'<image\b[^>]*>.*?</image>','[이미지 첨부 생략]',text,flags=re.S)
    text=re.sub(r'(?is)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----','[비공개 키 제외]',text)
    text=re.sub(r'(?im)^.*(?:api[_ -]?key|service[_ -]?key|cert[_ -]?key|password|token|비밀번호)\s*[=:]\s*[^\s]+.*$','[인증정보가 포함될 수 있는 줄 제외]',text)
    text=re.sub(r'(?i)\bBearer\s+[a-z0-9._-]+|\bsk-[a-z0-9_-]{16,}|\beyJ[a-zA-Z0-9_-]{15,}\.[a-zA-Z0-9_.-]+','[인증정보 제외]',text)
    return text
