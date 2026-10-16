"""All receiver tests use synthetic configuration and in-process replay by injection.

Production configuration tests explicitly undo this fixture's store injection.
"""
import pytest
from aj_llm_layer import shadow, service_auth


@pytest.fixture(autouse=True)
def synthetic_receiver(monkeypatch):
    for name in ('AJ_LLM_LAYER_AUTH_MODE', 'AJ_LLM_LAYER_SIGNING_KEYS', 'AJ_ENVIRONMENT',
                 'AJ_LLM_LAYER_SHADOW_TOKEN', 'AJ_LLM_LAYER_REPLAY_BACKEND',
                 'AJ_LLM_LAYER_REPLAY_REDIS_URL'):
        monkeypatch.delenv(name, raising=False)
    cache = service_auth.ReplayCache()
    monkeypatch.setattr(shadow, 'get_replay_store', lambda: cache)
