import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from aj_llm_layer.api import app
from aj_llm_layer import service_auth as a, replay, shadow

KEY = b'synthetic-hardening-only-0123456789abcdef'
PATH = '/internal/v1/shadow'
BODY = b'{"task_family":"light_text","estimated_input_tokens":0,"estimated_output_tokens":0}'
ARGS = dict(method='POST', path=PATH, body=BODY, env='staging', scope='shadow:write')


def signed(**over):
    return a.sign(**(ARGS | over), key=KEY, kid='k1', now=1000)


def verify(token, now=1000, **over):
    return a.verify(token, **(ARGS | over), keys={'k1':KEY}, replay=a.ReplayCache(), now=now)


def altered(**claims):
    c = json.loads(a._unb64(signed().split('.')[2])) | claims
    text = a.b64(a.canonical(c))
    return f'v1.k1.{text}.{a.mac(KEY,"k1",text)}'


@pytest.mark.parametrize('now,valid', [(999,True),(970,True),(969,False),(1029,True),(1030,False),(1031,False),(1060,False)])
def test_expiry_and_future_boundaries(now, valid):
    if valid:
        verify(signed(),now)
    else:
        with pytest.raises(a.InvalidCredential): verify(signed(),now)


@pytest.mark.parametrize('changes', [dict(iat=True),dict(iat='1000'),dict(exp=1030.5),dict(jti={'id':'x'*16}),
                                  dict(bh='é'),dict(jti='!'*22),dict(aud='wrong'),dict(iss='wrong'),
                                  dict(exp=1061),dict(exp=1000),dict(extra='unapproved')])
def test_strict_claims(changes):
    with pytest.raises(a.InvalidCredential): verify(altered(**changes))


@pytest.mark.parametrize('token', ['', 'x'*1025, 'v1.k1.é.x', 'v1.unknown.e30.x', 'v2.k1.e30.x', 'v1.k1.e30.bad'])
def test_malformed_and_forged(token):
    with pytest.raises(a.InvalidCredential): verify(token)


def test_rotation_configuration_is_unambiguous():
    assert a.parse_keys('k1:'+KEY.decode()+',k2:'+KEY.decode())
    assert not a.parse_keys('k1:'+KEY.decode()+',k1:'+KEY.decode())
    assert not a.parse_keys('k1:'+KEY.decode()+',broken')
    assert not a.parse_keys('k1:'+'é'*40)


def test_local_cache_concurrent_single_use():
    cache = a.ReplayCache()
    with ThreadPoolExecutor(max_workers=16) as pool:
        result = list(pool.map(lambda _: cache.add_if_absent('nonce',1030,1000),range(64)))
    assert sum(result)==1


@pytest.mark.parametrize('mode', ['static','both','signed','typo'])
def test_auth_modes_failures(monkeypatch,mode):
    monkeypatch.setenv('AJ_LLM_LAYER_AUTH_MODE',mode)
    monkeypatch.setenv('AJ_LLM_LAYER_SHADOW_TOKEN','a'*48)
    monkeypatch.setenv('AJ_LLM_LAYER_SIGNING_KEYS','k1:'+KEY.decode())
    monkeypatch.setenv('AJ_ENVIRONMENT','staging')
    with TestClient(app) as c:
        result=c.post(PATH,content=BODY,headers={'Authorization':'Bearer '+'a'*48})
        assert result.status_code == (200 if mode in ('static','both') else 503 if mode=='typo' else 401)
        result=c.post(PATH,content=BODY,headers={'Authorization':'Bearer '+'a'*48,'X-AJ-Service-Auth':'forged'})
        assert result.status_code == (200 if mode=='static' else 503 if mode=='typo' else 401)


def test_static_malformed_ascii_and_prefix(monkeypatch):
    monkeypatch.setenv('AJ_LLM_LAYER_SHADOW_TOKEN','a'*48)
    c=TestClient(app)
    assert c.post(PATH,content=BODY,headers=[(b'authorization',b'Bearer \xff')]).status_code==401
    assert c.post(PATH,content=BODY,headers={'Authorization':'a'*48}).status_code==401
    monkeypatch.setenv('AJ_LLM_LAYER_SHADOW_TOKEN','é'*48)
    assert c.post(PATH,content=BODY).status_code==503


def test_body_bounds_before_validation(monkeypatch):
    c=TestClient(app)
    assert c.post(PATH,content=b'x'*1025).status_code==413
    # No Content-Length: generator upload is counted, too.
    assert c.post(PATH,content=iter([b'x'*700,b'x'*700])).status_code==413
    assert c.post(PATH,content=b'{"prompt":"private"}').status_code==503


def test_duplicate_headers_and_query_rejected(monkeypatch):
    monkeypatch.setenv('AJ_LLM_LAYER_SHADOW_TOKEN','a'*48)
    c=TestClient(app)
    h=[('Authorization','Bearer '+'a'*48)]*2
    assert c.post(PATH,content=BODY,headers=h).status_code==401
    assert c.post(PATH+'?extra=1',content=BODY).status_code==401


def test_allowlist_and_no_schema_leak(monkeypatch,caplog):
    monkeypatch.setenv('AJ_LLM_LAYER_SHADOW_TOKEN','a'*48)
    c=TestClient(app)
    bad=BODY.replace(b'light_text',b'customer_private_name')
    assert c.post(PATH,content=bad).status_code==401
    assert c.post(PATH,content=bad,headers={'Authorization':'Bearer '+'a'*48}).status_code==422
    assert 'customer_private_name' not in caplog.text


def test_storage_configuration_restrictions(monkeypatch):
    monkeypatch.setenv('AJ_ENVIRONMENT','staging')
    monkeypatch.setenv('AJ_LLM_LAYER_REPLAY_BACKEND','local')
    with pytest.raises(replay.ReplayUnavailable): replay.get_replay_store()
    monkeypatch.setenv('AJ_LLM_LAYER_REPLAY_BACKEND','redis')
    monkeypatch.setenv('AJ_LLM_LAYER_REPLAY_REDIS_URL','redis://localhost:6379')
    with pytest.raises(replay.ReplayUnavailable): replay.get_replay_store()
    monkeypatch.setenv('AJ_ENVIRONMENT','test')
    monkeypatch.setenv('AJ_LLM_LAYER_REPLAY_BACKEND','local')
    assert isinstance(replay.get_replay_store(),a.ReplayCache)


def test_storage_failure_is_503_without_details(monkeypatch,caplog):
    monkeypatch.setenv('AJ_LLM_LAYER_AUTH_MODE','signed')
    monkeypatch.setenv('AJ_LLM_LAYER_SIGNING_KEYS','k1:'+KEY.decode())
    monkeypatch.setenv('AJ_ENVIRONMENT','staging')
    class Broken:
        def eval(self,*args): raise ConnectionError('do not leak credentials')
    monkeypatch.setattr(shadow,'get_replay_store',lambda:replay.RedisReplayStore(Broken(),environment='staging'))
    token=a.sign(**ARGS,key=KEY,kid='k1')
    r=TestClient(app).post(PATH,content=BODY,headers={'X-AJ-Service-Auth':token})
    assert r.status_code==503
    assert 'do not leak' not in r.text+caplog.text


def test_slow_body_deadline(monkeypatch):
    monkeypatch.setattr(shadow,'BODY_TIMEOUT_SECONDS',0.01)
    async def run():
        sent=[]
        async def receive():
            await asyncio.sleep(0.1)
            return {'type':'http.request','body':b'', 'more_body':False}
        async def send(message): sent.append(message)
        scope={'type':'http','asgi':{'version':'3.0'},'method':'POST','path':PATH,'raw_path':PATH.encode(),
               'query_string':b'','headers':[],'scheme':'http','server':('test',80),'client':('test',1),'root_path':''}
        await app(scope,receive,send)
        assert sent[0]['status']==408
    asyncio.run(run())


def test_admission_and_bounded_metrics():
    from aj_llm_layer import observability as o
    held=[]
    try:
        for _ in range(16):
            assert o._slots.acquire(blocking=False)
            held.append(True)
        r=TestClient(app).post(PATH,content=BODY)
        assert r.status_code==503
        assert o.metrics_snapshot()['overloaded'] >= 1
        assert all('synthetic' not in name for name in o.metrics_snapshot())
    finally:
        for _ in held: o._slots.release()
