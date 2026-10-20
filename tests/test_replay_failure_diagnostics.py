"""Operational failures retain fixed categories without exposing connection secrets."""
import json
import ssl

import pytest
from redis.exceptions import AuthenticationError, ConnectionError, TimeoutError

from aj_llm_layer.certification import diagnostics
from aj_llm_layer.replay import RedisReplayStore


@pytest.mark.parametrize("error,category", [
    (AuthenticationError("rediss://user:do-not-log@private-host"), "authentication"),
    (TimeoutError("rediss://user:do-not-log@private-host"), "timeout"),
    (ConnectionError("rediss://user:do-not-log@private-host"), "connection"),
])
def test_probe_preserves_category_not_exception_message(monkeypatch, error, category):
    class Client:
        def ping(self):
            raise error
    monkeypatch.setattr("aj_llm_layer.replay.get_replay_store",
                        lambda: RedisReplayStore(Client(), environment="staging"))
    result = diagnostics._replay_status()
    assert result == ("unreachable", category)
    assert "do-not-log" not in json.dumps(result)
    assert "private-host" not in json.dumps(result)


def test_wrapped_tls_verification_failure_is_identified():
    outer = ConnectionError("rediss://user:do-not-log@private-host")
    outer.__cause__ = ssl.SSLCertVerificationError("sensitive certificate detail")
    assert diagnostics._failure_category(outer) == "tls_certificate"


def test_unknown_exception_and_cycles_remain_bounded():
    outer = ValueError("private connection details")
    outer.__cause__ = outer
    assert diagnostics._failure_category(outer) == "unknown"
