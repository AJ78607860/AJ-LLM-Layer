"""AJ LLM Layer execution gateway: one interface, many providers, automatic fallback.

In-process use (same Python service):

    from aj_llm_layer.gateway import Gateway, CompletionRequest, Message
    gateway = Gateway()  # reads keys and models from the environment
    result = gateway.complete(CompletionRequest(messages=(Message(role="user", content="Hi"),)),
                              app="aj_assistant")

Over HTTP from any app: see aj_llm_layer.client.LLMClient.
"""
from .config import GatewayConfig, ProviderConfig, load_config
from .errors import ErrorKind, GatewayError, ProviderError
from .health import MemoryStore, ProviderHealth, RedisStore
from .catalog import Catalog, CatalogModel, load_catalog
from .evidence import EvidenceLedger
from .router import Gateway
from .selector import Selector, SelectorSettings
from .types import Attempt, CompletionRequest, CompletionResult, Message, Usage

__all__ = ["Catalog", "CatalogModel", "EvidenceLedger", "Selector", "SelectorSettings", "load_catalog", "Attempt", "MemoryStore", "ProviderHealth", "RedisStore", "CompletionRequest", "CompletionResult", "ErrorKind",
           "Gateway", "GatewayConfig", "GatewayError", "Message", "ProviderConfig",
           "ProviderError", "Usage", "load_config"]
