"""Objective evaluation of identical synthetic tasks, without LLM judges.

Inspired by LiveBench's objective scoring approach. Synthetic fixtures only;
not a copy of LiveBench's dataset or evaluator.
"""
from __future__ import annotations

from dataclasses import dataclass
import json


@dataclass(frozen=True)
class TaskCase:
    id: str
    task_family: str
    prompt: str
    expected: str
    evaluator: str


def validate(case: TaskCase, response: str) -> bool:
    if case.evaluator == "exact":
        return response.strip() == case.expected.strip()
    if case.evaluator == "json":
        try:
            return json.loads(response) == json.loads(case.expected)
        except (ValueError, TypeError):
            return False
    raise ValueError("unsupported independent evaluator")


SYNTHETIC_CASES = (
    TaskCase("arithmetic-1", "math", "What is 17 + 25? Answer with only the number.", "42", "exact"),
    TaskCase("json-1", "classification", 'Return JSON {"label":"positive"} for: I loved it.',
             '{"label":"positive"}', "json"),
)
