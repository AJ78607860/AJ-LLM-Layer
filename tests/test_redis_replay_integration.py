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
from aj_llm_layer.replay import QUARANTINE_SECONDS, RedisReplayStore, ReplayUnavailable


def marker(client, at):
    return 'epoch:%d:%s' % (at, client.info('server')['run_id'])


def settled(client, environment='test'):
    """Mark the registry as having existed (on this Redis run) for longer than one credential lifetime."""
    client.zadd('aj:llm:replay:v1:' + environment, {marker(client, int(time.time()) - 1000): float('inf')})
    return client


@pytest.fixture
def server():
    executable=os.getenv('AJ_TEST_REDIS_SERVER')
    if not executable:
        pytest.skip('set AJ_TEST_REDIS_SERVER to execute real Redis integration')
    with tempfile.TemporaryDirectory(prefix='ajr-',dir='/tmp') as folder:
        sock=str(Path(folder)/'r.sock')
        def start(persistent=True, policy='noeviction'):
            durability=['--appendonly','yes','--appendfsync','always'] if persistent else ['--appendonly','no']
            proc=subprocess.Popen([executable,'--port','0','--unixsocket',sock,'--dir',folder,
                                   *durability,'--save','',
                                   '--maxmemory','16mb','--maxmemory-policy',policy],
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
    settled(server['client'])
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
    members=server['client'].zrangebyscore(store.key,'-inf','(+inf')
    assert len(members)==2 and all(len(m)==64 for m in members)
    assert b'a'*22 not in members
    # No TTL: a volatile-* eviction policy can never pick the registry.
    assert server['client'].ttl(store.key) == -1


def test_replay_across_receiver_processes_and_restart(server):
    import sys
    code='''
import sys,time,redis
from aj_llm_layer.replay import RedisReplayStore
s=RedisReplayStore(redis.Redis(unix_socket_path=sys.argv[1]),environment='test')
n=int(time.time())
try:
    print(int(s.add_if_absent('shared-synthetic-nonce',n+30,n)))
except Exception as exc:
    print(type(exc).__name__)
'''
    def worker():
        return subprocess.check_output([sys.executable,'-c',code,server['socket']],text=True).strip()
    settled(server['client'])
    assert worker()=='1'
    assert worker()=='0'
    server['client'].shutdown(save=True)
    server['process'].wait(timeout=5)
    proc,client=server['start']()
    server.update(process=proc,client=client)
    # A restarted Redis (new run_id) starts a new epoch: the replay is refused by the quarantine (503), even
    # though persistence kept the nonce.
    assert worker()=='ReplayUnavailable'
    proc.terminate();proc.wait(timeout=5)
    store=RedisReplayStore(client,environment='test')
    now=int(time.time())
    with pytest.raises(ReplayUnavailable): store.add_if_absent('new-synthetic-nonce',now+30,now)


def test_atomic_expired_registry_cleanup(server):
    c=settled(server['client']); s=RedisReplayStore(c,environment='test',capacity=1)
    c.zadd(s.key,{'old-digest':int(time.time())-1})
    now=int(time.time())
    assert s.add_if_absent('fresh-synthetic-nonce',now+30,now)
    assert c.zcard(s.key)==2          # the fresh nonce + the epoch marker; the expired digest is gone


# ---------------------------------------------------------------- registry loss (certification 20 Oct)
def _restart(server, **kwargs):
    server['client'].shutdown(nosave=True)
    server['process'].wait(timeout=5)
    proc,client=server['start'](**kwargs)
    server.update(process=proc,client=client)
    return client


def test_restart_without_persistence_never_accepts_a_nonce_seen_before(server):
    """Staging Key Value runs with persistence off. Before the epoch marker, a restart forgot every nonce and the
    same signed credential was accepted again inside its lifetime."""
    client=_restart(server, persistent=False)
    settled(client)
    store=RedisReplayStore(client,environment='test')
    now=int(time.time())
    assert store.add_if_absent('seen-before-restart',now+30,now)
    client=_restart(server, persistent=False)
    store=RedisReplayStore(client,environment='test')
    with pytest.raises(ReplayUnavailable):
        store.add_if_absent('seen-before-restart',now+30,now)          # the replay
    with pytest.raises(ReplayUnavailable):
        store.add_if_absent('brand-new-but-in-quarantine',now+30,now)   # fail closed for one lifetime


def test_flush_starts_a_new_epoch_and_quarantines(server):
    c=settled(server['client']); store=RedisReplayStore(c,environment='test')
    now=int(time.time())
    assert store.add_if_absent('before-flush',now+30,now)
    c.flushall()
    with pytest.raises(ReplayUnavailable):
        store.add_if_absent('before-flush',now+30,now)
    found=c.zrangebyscore(store.key,'+inf','+inf')
    assert len(found)==1 and found[0].startswith(b'epoch:')


def test_quarantine_ends_after_one_credential_lifetime(server):
    c=server['client']; store=RedisReplayStore(c,environment='test')
    now=int(time.time())
    with pytest.raises(ReplayUnavailable):
        store.add_if_absent('first-ever-use',now+30,now)               # brand-new registry
    current=c.zrangebyscore(store.key,'+inf','+inf')[0]
    # A credential that expires after epoch + QUARANTINE_SECONDS was used after the epoch: accepted.
    c.zrem(store.key,current)
    c.zadd(store.key,{marker(c, now - QUARANTINE_SECONDS + 29): float('inf')})
    assert store.add_if_absent('after-quarantine',now+30,now)
    assert not store.add_if_absent('after-quarantine',now+30,now)


def test_corrupt_marker_fails_closed(server):
    c=server['client']; store=RedisReplayStore(c,environment='test')
    c.zadd(store.key,{'epoch:not-a-number': float('inf')})
    now=int(time.time())
    with pytest.raises(ReplayUnavailable):
        store.add_if_absent('any',now+30,now)


@pytest.mark.parametrize('policy',['volatile-lru','volatile-ttl','allkeys-lru'])
def test_eviction_can_not_resurrect_a_nonce(server, policy):
    """Under memory pressure the registry is either kept (volatile-*: it has no TTL) or evicted whole with its
    epoch marker (allkeys-*), which quarantines. Either way a seen nonce is never accepted again."""
    client=_restart(server, persistent=False, policy=policy)
    settled(client)
    store=RedisReplayStore(client,environment='test')
    now=int(time.time())
    assert store.add_if_absent('seen-before-pressure',now+30,now)
    client.config_set('maxmemory','2mb')
    filler=b'x'*4096
    for i in range(4000):
        try:
            client.set(f'filler:{i}',filler,ex=600)
        except redis.RedisError:
            break
    accepted=None
    try:
        accepted=store.add_if_absent('seen-before-pressure',now+30,now)
    except ReplayUnavailable:
        accepted=False
    assert accepted is False


def test_restore_of_older_state_after_restart_never_accepts_a_used_nonce(server):
    """Reviewer finding: persistence that restores OLDER state (RDB snapshot, AOF tail, async replica) keeps the
    marker but loses the newest nonces. The restarted Redis has a new run_id, so a new epoch quarantines."""
    c=settled(server['client']); store=RedisReplayStore(c,environment='test')
    now=int(time.time())
    assert store.add_if_absent('before-snapshot',now+30,now)
    c.config_set('appendonly','no'); c.save()                     # the snapshot on disk
    assert store.add_if_absent('after-snapshot',now+30,now)       # used, but never persisted
    c.shutdown(nosave=True); server['process'].wait(timeout=5)   # crash: reload the older snapshot
    proc,client=server['start'](persistent=False)
    server.update(process=proc,client=client)
    store=RedisReplayStore(client,environment='test')
    assert client.zrangebyscore(store.key,'+inf','+inf')          # the stale marker survived the restore
    with pytest.raises(ReplayUnavailable):
        store.add_if_absent('after-snapshot',now+30,now)


def test_loss_in_the_same_second_as_the_use_still_quarantines(server):
    """Reviewer finding: exp == epoch + 90 (use, loss and replay in one Redis second) was accepted."""
    c=settled(server['client']); store=RedisReplayStore(c,environment='test')
    t=int(c.time()[0]); exp=t+90
    c.flushall()
    c.zadd(store.key,{marker(c, t): float('inf')})               # the epoch recreated in that same second
    with pytest.raises(ReplayUnavailable):
        store.add_if_absent('edge',exp,t)


def test_ttl_left_by_older_code_is_cleared(server):
    """Reviewer finding: a key written by the previous script kept EXPIRE 180, so the marker expired with it and a
    second quarantine followed. The script now PERSISTs the key."""
    c=settled(server['client']); store=RedisReplayStore(c,environment='test')
    c.expire(store.key,180)
    now=int(time.time())
    assert store.add_if_absent('after-deploy',now+30,now)
    assert c.ttl(store.key)==-1


def test_old_marker_format_fails_closed(server):
    c=server['client']; store=RedisReplayStore(c,environment='test')
    c.zadd(store.key,{'epoch:%d' % (int(time.time()) - 1000): float('inf')})
    now=int(time.time())
    with pytest.raises(ReplayUnavailable):
        store.add_if_absent('any',now+30,now)
