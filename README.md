# AJ-LLM-Layer
Open-source adaptive AI routing infrastructure. Intelligent model selection, cost optimization, latency monitoring, benchmark intelligence and multi-provider orchestration.

## Execution gateway (v0.2)

Apps (AJ Assistant, Calibre, ...) send model calls to `POST /v1/complete` or use
`aj_llm_layer.client.LLMClient`. OpenAI and Anthropic are supported with automatic
fallback (out of credits, rate limits, outages) and a personal-data approval gate.
Off by default. See [docs/GATEWAY.md](docs/GATEWAY.md).
