"""Staging-only synthetic certification: decide -> reserve -> grant -> outcome -> settle.

The Layer never executes inference and never receives prompt or response text.
AJ executes an issued grant through its own provider client after its own
policy check, then reports content-free usage and a deterministic validation
result. Every path fails closed. Production environments cannot reach it.
"""
