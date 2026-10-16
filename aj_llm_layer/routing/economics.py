"""Estimates, never actual charges or budget reservations. All attempts are charged."""
from decimal import Decimal, localcontext, ROUND_CEILING
from .contracts import Model
from .tasks import Task, Contract
from .eligibility import quality_lower

class Estimate(Contract):
    pricing_version: str
    currency: str
    expected_request_cost: Decimal
    worst_case_cost: Decimal
    expected_cost_per_success: Decimal
    measured_cost_per_success: Decimal | None
    lower_cost: Decimal
    upper_cost: Decimal
    is_estimate: bool = True


def estimate(task: Task, model: Model, retries: int, currency: str) -> Estimate:
    if type(retries) is not int or not 0 <= retries <= 5:
        raise ValueError('invalid retries')
    p, e = model.price, model.evidence
    if p is None or e is None or p.currency != currency or e.currency != currency:
        raise ValueError('missing price or currency mismatch')
    if task.cached_tokens > task.input_tokens or e.successes > e.samples:
        raise ValueError('invalid estimate inputs')
    q = quality_lower(model)
    if q <= 0:
        raise ValueError('unknown successful completion cost')
    with localcontext() as ctx:
        ctx.prec = 40
        unit = ((task.input_tokens-task.cached_tokens)*p.input_per_million
                + task.cached_tokens*p.cached_per_million
                + task.output_tokens*p.output_per_million) / Decimal(1000000)
        failure = Decimal(str(e.failure_rate)) + Decimal(str(e.timeout_rate))
        if failure > 1:
            raise ValueError('invalid failure distribution')
        expected = unit * sum((failure**i if i else Decimal(1)) for i in range(retries+1))
        upper = unit*(retries+1)
        quant = lambda v: v.quantize(Decimal('.00000001'), rounding=ROUND_CEILING)
        return Estimate(pricing_version=p.version, currency=currency,
                        expected_request_cost=quant(expected), worst_case_cost=quant(upper),
                        expected_cost_per_success=quant(expected/Decimal(str(q))),
                        measured_cost_per_success=quant(e.measured_cost/e.successes) if e.successes else None,
                        lower_cost=quant(unit), upper_cost=quant(upper))
