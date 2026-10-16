"""Bounded, seeded synthetic comparison; no real-world savings claims."""
from decimal import Decimal
from random import Random
from pydantic import Field
from .tasks import Contract, Task, FAMILIES
from .contracts import Snapshot, Authorization
from .policies import Policy
from .engine import route

class SimulationRequest(Contract):
    version: str = 'aj.scenarios.v1'
    seed: int = Field(default=42, ge=0, le=2**32-1)
    tasks: int = Field(default=100, ge=1, le=1000)
    failure_multiplier: float = Field(default=1, ge=0, le=5, allow_inf_nan=False)


def simulate(request: SimulationRequest, registry: Snapshot, auth: Authorization,
             *, now: int) -> dict:
    report = {'label': 'SYNTHETIC SIMULATION ONLY', 'request': request.model_dump(),
              'registry_version': registry.version, 'authorization_version': auth.version,
              'timestamp': now, 'strategies': {}}
    # Same workload and random uniforms across policies for paired comparison.
    rng = Random(request.seed)
    workload = [(Task(families=(rng.choice(tuple(FAMILIES)),), criticality='normal',
                      input_tokens=rng.randint(100, 5000), output_tokens=rng.randint(50, 1000)),
                 rng.random(), rng.random()) for _ in range(request.tasks)]
    models = {m.id:m for m in registry.models}
    for strategy in ('baseline', 'quality_first', 'balanced', 'cost_efficient', 'latency_sensitive'):
        policy = Policy(version='synthetic.policy.v1.'+strategy, strategy='quality_first' if strategy == 'baseline' else strategy,
                        approved=True, enabled=True)
        remaining, total = auth.budget_remaining, Decimal(0)
        successes = rejected = failures = latency = 0
        distribution = {}
        decisions = []
        for i, (task, uniform, delay) in enumerate(workload):
            current = auth.model_copy(update={'budget_remaining': remaining})
            candidates = registry
            if strategy == 'baseline':
                # Explicit fixed-model baseline; it still traverses every mandatory gate.
                candidates = registry.model_copy(update={'models': registry.models[:1]})
            decision = route(task.model_dump(), candidates, current, policy, now=now,
                             correlation_id=f'synthetic-{i}', simulation=True)
            decisions.append(decision.decision_id)
            if decision.selected is None:
                rejected += 1
                continue
            model = models[decision.selected]
            distribution[model.id] = distribution.get(model.id, 0)+1
            cost = decision.estimates[model.id].worst_case_cost
            total += cost
            remaining -= cost
            e = model.evidence
            failure = min(1, (e.failure_rate+e.timeout_rate)*request.failure_multiplier)
            success = uniform < min(e.successes/e.samples, 1-failure)
            successes += int(success)
            failures += int(not success)
            latency += int(e.latency_p95_ms*(.5+delay))
        report['strategies'][strategy] = {
            'estimated_total_cost': str(total), 'currency': auth.currency,
            'estimated_cost_per_success': str(total/successes) if successes else None,
            'simulated_successes': successes, 'simulated_failures': failures,
            'rejections': rejected, 'rejection_rate': rejected/request.tasks,
            'estimated_latency_total_ms': latency,
            'budget_utilization': str(total/auth.budget_remaining) if auth.budget_remaining else '0',
            'model_distribution': distribution, 'decision_ids': decisions,
        }
    return report
