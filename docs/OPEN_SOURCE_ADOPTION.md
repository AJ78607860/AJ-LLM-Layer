# Open-source consolidation review (initial)

This implementation is independently written and does not copy third-party
source code, benchmark datasets, model weights or licensed API content.

## LiteLLM — provider execution
Reference: https://github.com/BerriAI/litellm
Adopt the provider-neutral interface concept; defer the full proxy until
dependency, operational overhead, licensing and security review.
Do not deploy LiteLLM, add credentials, or grant data egress automatically.

## LiveBench — objective evaluation
Reference: https://github.com/LiveBench/LiveBench
Adopt objectively checkable evaluation principles. Current cases are original
synthetic fixtures, not LiveBench results. Real benchmark ingestion requires
dataset license and usage checks, dataset/version provenance, and contamination
controls.

## Vowpal Wabbit — contextual learning
Reference: https://github.com/VowpalWabbit/vowpal_wabbit
Adopt online-feedback statistical principles, initially via local aggregates.
No Vowpal Wabbit dependency or copied algorithm is claimed. Before upgrading
to contextual bandits: implement propensity logging, counterfactual offline
evaluation, tenant isolation, constrained exploration, quality floors, and
drift detection. No unapproved production exploration.

## Non-goals of this PR
No third-party package installation, no external API requests, no production
database, no live model benchmark results, and no AJ Personal Assistant
routing change.
