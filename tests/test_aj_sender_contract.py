"""Exercise actual AJ sender source, with only its HTTP boundary substituted.

Set AJ_SENDER_SOURCE to a complete checkout; no model/provider modules imported.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import types

import pytest
from fastapi.testclient import TestClient
from aj_llm_layer.api import app
from aj_llm_layer import service_auth as receiver

KEY='synthetic-cross-repo-dummy-key-0123456789abcdef'
PATH='/internal/v1/shadow'


@pytest.fixture
def sender(monkeypatch):
    root=Path(os.getenv('AJ_SENDER_SOURCE',str(Path(__file__).resolve().parents[2]/'aj-sender-git')))
    if not (root/'llm_router/service_auth.py').is_file():
        pytest.skip('AJ_SENDER_SOURCE must identify the real AJ source checkout')
    # Avoid importing AJ's unrelated application/provider runtime during this contract test.
    package=types.ModuleType('llm_router'); package.__path__=[str(root/'llm_router')]
    monkeypatch.setitem(sys.modules,'llm_router',package)
    for name in ('service_auth','llm_layer_shadow'):
        spec=importlib.util.spec_from_file_location('llm_router.'+name,root/'llm_router'/f'{name}.py')
        module=importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules,spec.name,module)
        spec.loader.exec_module(module)
        setattr(package,name,module)
    s=package.llm_layer_shadow
    monkeypatch.setenv('AJ_LLM_LAYER_AUTH_MODE','signed')
    monkeypatch.setenv('AJ_LLM_LAYER_SIGNING_KEYS','contract:'+KEY)
    monkeypatch.setenv('AJ_LLM_LAYER_SIGNING_KID','contract')
    monkeypatch.setenv('AJ_ENVIRONMENT','staging')
    monkeypatch.setenv('AJ_LLM_LAYER_SHADOW_ENABLED','true')
    monkeypatch.setenv('AJ_LLM_LAYER_SHADOW_URL','https://synthetic.invalid'+PATH)
    monkeypatch.setattr(s,'_check_resolution',lambda *args:'8.8.8.8')
    return package


def bridge(monkeypatch,sender,transform=None):
    calls=[]
    class Response:
        def __init__(self,r): self.r=r; self.status_code=r.status_code; self.headers=r.headers
        def iter_content(self,chunk_size): yield self.r.content
        def close(self): pass
    def post(url,**kwargs):
        assert kwargs['stream'] is True and kwargs['allow_redirects'] is False
        assert url=='https://synthetic.invalid'+PATH
        headers=dict(kwargs['headers']);body=kwargs.get('data')
        assert isinstance(body,bytes)
        calls.append((body,dict(headers)))
        if transform: headers,body=transform(headers,body)
        return Response(TestClient(app).post(PATH,content=body,headers=headers))
    monkeypatch.setattr(sender.llm_layer_shadow,'_http_post',post)
    return calls


def test_real_sender_receiver_success_and_privacy(sender,monkeypatch):
    calls=bridge(monkeypatch,sender)
    assert sender.llm_layer_shadow.observe('light_text',correlation_id='req_0123456789abcdef')
    body,headers=calls[0]
    assert set(json.loads(body))=={'task_family','estimated_input_tokens','estimated_output_tokens'}
    assert headers['X-AJ-Correlation-ID']=='req_0123456789abcdef'
    r=TestClient(app).post(PATH,content=body,headers=headers)
    assert r.status_code==401  # exact accepted sender request cannot be reused


@pytest.mark.parametrize('failure',['key','env','aud','scope','method','path','body','expired','future'])
def test_real_sender_mutations_rejected(sender,monkeypatch,failure):
    def change(headers,body):
        token=headers['X-AJ-Service-Auth'];v,kid,c64,sig=token.split('.')
        claims=json.loads(sender.service_auth._unb64(c64))
        if failure=='key': sig='A'*43
        elif failure=='body': body=body.replace(b'light_text',b'llm_tools')
        else:
            import time
            now=int(time.time())
            changes={'env':{'env':'production'},'aud':{'aud':'elsewhere'},'scope':{'scope':'inference:execute'},
                     'method':{'m':'GET'},'path':{'p':'/elsewhere'},
                     'expired':{'iat':now-31,'exp':now-1},'future':{'iat':now+31,'exp':now+60}}
            claims.update(changes[failure]);c64=sender.service_auth.b64(sender.service_auth.canonical(claims))
            sig=sender.service_auth.mac(KEY.encode(),kid,c64)
        headers['X-AJ-Service-Auth']='.'.join((v,kid,c64,sig))
        return headers,body
    bridge(monkeypatch,sender,change)
    assert not sender.llm_layer_shadow.observe('light_text')
    assert sender.llm_layer_shadow.metrics_snapshot()['authentication_failure']==1


@pytest.mark.parametrize('failure',['unavailable','timeout','invalid_response'])
def test_real_sender_failure_isolation(sender,monkeypatch,failure):
    s=sender.llm_layer_shadow
    import requests
    count=[]
    class Invalid:
        status_code=200; headers={}
        def iter_content(self,chunk_size): yield b'{bad json'
        def close(self): pass
    def post(*args,**kwargs):
        count.append(1)
        if failure=='timeout': raise requests.Timeout()
        if failure=='unavailable': raise requests.ConnectionError()
        return Invalid()
    monkeypatch.setattr(s,'_http_post',post)
    for _ in range(4): assert s.observe('light_text') is False
    assert len(count)==3 and s.metrics_snapshot()['circuit_open']==1


def test_golden_fixture_identity(sender):
    root=Path(sender.service_auth.__file__).parents[1]
    assert (root/'tests/fixtures/llm_layer_service_auth_vectors.json').read_bytes()==(
        Path(__file__).parent/'fixtures/llm_layer_service_auth_vectors.json').read_bytes()


def test_key_rotation_and_missing_config(sender,monkeypatch):
    monkeypatch.setenv('AJ_LLM_LAYER_SIGNING_KEYS','old:'+KEY+',new:'+KEY+'new')
    monkeypatch.setenv('AJ_LLM_LAYER_SIGNING_KID','new')
    calls=bridge(monkeypatch,sender)
    assert sender.llm_layer_shadow.observe('light_text')
    assert calls[0][1]['X-AJ-Service-Auth'].startswith('v1.new.')
    monkeypatch.setenv('AJ_LLM_LAYER_SIGNING_KID','missing')
    assert sender.llm_layer_shadow.observe('light_text') is False
    assert len(calls)==1  # no downgraded HTTP request
