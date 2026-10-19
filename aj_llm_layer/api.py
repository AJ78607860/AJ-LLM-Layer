"""Public-safe service skeleton; no inference or user-content endpoints."""
from fastapi import FastAPI
from pydantic import BaseModel, Field

from .ranking import Candidate, rank

app = FastAPI(title="AJ LLM Layer", version="0.1.0", docs_url=None, redoc_url=None)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "service": "aj-llm-layer"}


class BenchmarkCandidate(BaseModel):
    provider: str
    model: str
    cost_per_validated_success_usd: float = Field(ge=0)
    validated_success_lower_bound: float = Field(ge=0, le=1)
    latency_p95_ms: float = Field(ge=0)


# No public ranking endpoint yet: internal authentication, tenant isolation,
# data provenance and provider approval must precede any externally callable route.


# Metadata-only internal shadow endpoint; no model execution.
from .shadow import router as shadow_router
app.include_router(shadow_router)
from .observability import ReceiverEnvelope
app.add_middleware(ReceiverEnvelope)

# Staging-only synthetic certification (404 outside staging/development; signed credentials only;
# off unless AJ_LLM_LAYER_CERTIFICATION_ENABLED=true). Never executes inference.
from .certification.api import router as certification_router
app.include_router(certification_router)
