"""Real disposable Redis, including distinct receiver processes and durable restart.

AJ_TEST_REDIS_SERVER points to a local redis-server binary. Never uses live services.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

redis = pytest.importorskip('redis')
from aj_llm_layer.replay import RedisReplayStore, ReplayUnavailable


@pytest.fixture
def server():
    executable=os.getenv('AJ_TEST_REDIS_SERVER')
    if not executable:
        pytest.skip('set AJ_TEST_REDIS_SERVER to execute real Redis integration')
    with tempfile.TemporaryDirectory(prefix='ajr-',dir='/tmp') as folder:
        sock=str(Path(folder)/'r.sock')
        def start():
            proc=subprocess.Popen([executable,'--port','0','--unixsocket',sock,'--dir',folder,
                                   '--appendonly','yes','--appendfsync','always','--save','',
                                   '--maxmemory','16mb','--maxmemory-policy','noeviction'],
                                  stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            client=redis.Redis(unix_socket_path=sock,socket_timeout=0.5)
            for _ in range(100):
                try:
                    if client.ping(): return proc,client
                except redis.RedisError: pass
                time.sleep(0.01)
            proc.terminate();proc.wait(timeout=5)
            raise RuntimeError('test Redis failed to start')
        proc,client=start()
        state={'process':proc,'client':client,'socket':sock,'start':start}
        try: yield state
        finally:
            process=state['process']
            if process.poll() is None:
                process.terminate();process.wait(timeout=5)


def test_atomic_across_independent_clients_and_capacity(server):
    now=int(time.time())
    def attempt(_):
        client=redis.Redis(unix_socket_path=server['socket'])
        try:
            return RedisReplayStore(client,environment='test',capacity=2).add_if_absent('a'*22,now+30,now)
        finally: client.close()
    with ThreadPoolExecutor(max_workers=16) as pool:
        assert sum(pool.map(attempt,range(64)))==1
    store=RedisReplayStore(server['client'],environment='test',capacity=2)
    assert store.add_if_absent('b'*22,now+30,now)
    with pytest.raises(ReplayUnavailable): store.add_if_absent('c'*22,now+30,now)
    members=server['client'].zrange(store.key,0,-1)
    assert len(members)==2 and all(len(m)==64 for m in members)
    assert b'a'*22 not in members
    assert 1 <= server['client'].ttl(store.key) <= 180


def test_replay_across_receiver_processes_and_restart(server):
    import sys
    code='''
import sys,time,redis
from aj_llm_layer.replay import RedisReplayStore
s=RedisReplayStore(redis.Redis(unix_socket_path=sys.argv[1]),environment='test')
n=int(time.time())
print(int(s.add_if_absent('shared-synthetic-nonce',n+30,n)))
'''
    def worker():
        return subprocess.check_output([sys.executable,'-c',code,server['socket']],text=True).strip()
    assert worker()=='1'
    assert worker()=='0'
    server['client'].shutdown(save=True)
    server['process'].wait(timeout=5)
    proc,client=server['start']()
    server.update(process=proc,client=client)
    assert worker()=='0'
    proc.terminate();proc.wait(timeout=5)
    store=RedisReplayStore(client,environment='test')
    now=int(time.time())
    with pytest.raises(ReplayUnavailable): store.add_if_absent('new-synthetic-nonce',now+30,now)


def test_atomic_expired_registry_cleanup(server):
    c=server['client']; s=RedisReplayStore(c,environment='test',capacity=1)
    c.zadd(s.key,{'old-digest':int(time.time())-1})
    now=int(time.time())
    assert s.add_if_absent('fresh-synthetic-nonce',now+30,now)
    assert c.zcard(s.key)==1
