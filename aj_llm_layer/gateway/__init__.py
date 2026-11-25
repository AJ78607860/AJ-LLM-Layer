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
from .router import CircuitBreaker, Gateway
from .types import Attempt, CompletionRequest, CompletionResult, Message, Usage

__all__ = ["Attempt", "CircuitBreaker", "CompletionRequest", "CompletionResult", "ErrorKind",
           "Gateway", "GatewayConfig", "GatewayError", "Message", "ProviderConfig",
           "ProviderError", "Usage", "load_config"]
