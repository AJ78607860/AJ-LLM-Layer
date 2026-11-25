# AJ-LLM-Layer
Open-source adaptive AI routing infrastructure. Intelligent model selection, cost optimization, latency monitoring, benchmark intelligence and multi-provider orchestration.

## Execution gateway (v0.3)

Apps (AJ Assistant, Calibre, ...) send model calls to `POST /v1/complete` or use
`aj_llm_layer.client.LLMClient`. The Layer picks the model, with no provider preferred,
from evidence of correct answers and price per task type. It skips providers that are
out of credit, falls back on failures, and learns from graded answers (`/v1/feedback`,
leaderboard at `/v1/models`). Off by default. See [docs/GATEWAY.md](docs/GATEWAY.md).
