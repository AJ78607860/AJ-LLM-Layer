from typing import Literal
from .tasks import Contract, Task, Classification

class Complexity(Contract):
    version: str = 'aj.complexity.v1'
    band: Literal['simple', 'moderate', 'complex', 'high_stakes', 'unknown']
    reasons: tuple[str, ...]
    uncertainty: tuple[str, ...]


def assess(task: Task, classification: Classification) -> Complexity:
    reasons = []
    if task.criticality == 'high_stakes' or 'financial_analysis' in classification.families:
        band = 'high_stakes'
        reasons.append('HIGH_STAKES_REQUIREMENTS')
    elif classification.unknown or task.criticality == 'unknown':
        band = 'unknown'
        reasons.append('INCOMPLETE_METADATA')
    elif (task.steps or 0) >= 8 or len(classification.capabilities) >= 3:
        band = 'complex'
        reasons.append('MULTISTEP_OR_MULTICAPABILITY')
    elif classification.capabilities & {'reasoning', 'code', 'math', 'tools', 'vision', 'multimodal'}:
        band = 'moderate'
        reasons.append('SPECIALIST_CAPABILITY')
    else:
        band = 'simple'
        reasons.append('ROUTINE_TASK')
    if task.input_tokens + task.output_tokens > 32000:
        reasons.append('LARGE_CONTEXT_RESOURCE_REQUIREMENT')
    return Complexity(band=band, reasons=tuple(reasons),
                      uncertainty=('STEPS_UNSPECIFIED',) if task.steps is None else ())
