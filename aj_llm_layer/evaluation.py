"""Objective evaluation of identical synthetic tasks, without LLM judges.

Inspired by LiveBench's objective scoring approach. Synthetic fixtures only;
not a copy of LiveBench's dataset or evaluator.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from math import isfinite


@dataclass(frozen=True)
class TaskCase:
    id: str
    task_family: str
    prompt: str
    expected: str
    evaluator: str


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _same_json(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same_json(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_same_json(a, b) for a, b in zip(left, right))
    if isinstance(left, float) and (not isfinite(left) or not isfinite(right)):
        return False
    return left == right


def validate(case: TaskCase, response: str) -> bool:
    if not isinstance(response, str) or not isinstance(case.expected, str):
        return False
    if len(response) > 65536 or len(case.expected) > 65536:
        return False
    if case.evaluator == "exact":
        return response.strip() == case.expected.strip()
    if case.evaluator == "json":
        try:
            return _same_json(
                json.loads(response, parse_constant=_reject_constant, object_pairs_hook=_unique_object),
                json.loads(case.expected, parse_constant=_reject_constant, object_pairs_hook=_unique_object),
            )
        except (ValueError, TypeError, RecursionError):
            return False
    raise ValueError("unsupported independent evaluator")


SYNTHETIC_CASES = (
    TaskCase("arithmetic-1", "math", "What is 17 + 25? Answer with only the number.", "42", "exact"),
    TaskCase("json-1", "classification", 'Return JSON {"label":"positive"} for: I loved it.',
             '{"label":"positive"}', "json"),
)
