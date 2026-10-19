"""Versioned, metadata-only classification. Labels never establish privacy trust."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

TAXONOMY_VERSION = 'aj.tasks.v1'
FAMILIES = {
    'conversation': {'text'}, 'reasoning': {'reasoning'}, 'research': {'research'},
    'coding': {'code'}, 'mathematics': {'math', 'reasoning'},
    'writing': {'text'}, 'extraction': {'structured'}, 'translation': {'translation'},
    'structured_data': {'structured'}, 'planning': {'reasoning'},
    'tool_planning': {'tools'}, 'financial_analysis': {'reasoning', 'finance'},
    'travel_planning': {'planning'}, 'document_understanding': {'documents'},
    'image_understanding': {'vision'}, 'multimodal': {'multimodal'},
}

class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)

class Task(Contract):
    schema_version: Literal['aj.tasks.v1'] = TAXONOMY_VERSION
    families: tuple[str, ...] = Field(default=(), max_length=16)
    input_tokens: int = Field(default=0, ge=0, le=2_000_000)
    output_tokens: int = Field(default=0, ge=0, le=200_000)
    cached_tokens: int = Field(default=0, ge=0, le=2_000_000)
    context_tokens: int = Field(default=0, ge=0, le=2_200_000)
    steps: int | None = Field(default=None, ge=1, le=10000)
    structured: bool = False
    tools: bool = False
    modalities: frozenset[Literal['text', 'image', 'audio', 'document']] = frozenset({'text'})
    criticality: Literal['normal', 'high_stakes', 'unknown'] = 'unknown'

    @field_validator('families')
    @classmethod
    def bounded_labels(cls, labels):
        if any(len(label) > 64 for label in labels):
            raise ValueError('category label too long')
        return labels

class Classification(Contract):
    version: str = TAXONOMY_VERSION
    families: tuple[str, ...]
    capabilities: frozenset[str]
    unknown: bool
    uncertainty: tuple[str, ...]
    provenance: str = 'deterministic-metadata-rules.v1'


def classify(task: Task) -> Classification:
    known = tuple(sorted(set(task.families) & FAMILIES.keys()))
    unknown = not known or any(f not in FAMILIES for f in task.families)
    caps = set().union(*(FAMILIES[f] for f in known)) if known else set()
    if task.structured:
        caps.add('structured')
    if task.tools:
        caps.add('tools')
    caps.update({'image': 'vision', 'audio': 'audio', 'document': 'documents'}[m]
                for m in task.modalities if m != 'text')
    return Classification(families=known, capabilities=frozenset(caps), unknown=unknown,
                          uncertainty=('UNKNOWN_TASK',) if unknown else ())
