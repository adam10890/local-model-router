# 0013 - Executor trust ladder

## Status

Accepted

## Context

Imperium is the endpoint through which tasks pass between agents and
harnesses: the founder assigns work to one of them and Imperium brokers the
handoff. Every handoff hands content to an executor, but nothing said which
executors were trusted with what. Local-vs-cloud was the only distinction the
router made, and it was about routing capacity, not confidentiality.

## Decision

Executors are ranked on an explicit trust ladder, most trusted first:

1. `local_uncensored` — local model, no alignment filtering.
2. `local_aligned` — local model with vendor alignment.
3. `private_cloud` — cloud provider under a privacy/zero-retention guarantee.
4. `other_provider` — any other provider.

This is an **ordering, not a routing chain**: content does not have to pass
through every rung. The tier only caps who may receive it.

Each executor declares its tier where its identity already lives — slots in
the machine-local fleet YAML, upstreams in `conf/upstreams.yaml`, agents in
`conf/agents.yaml`. An executor that declares nothing resolves to the
least-trusted rung.

## Consequences

- Missing or misspelled configuration fails closed. Silence never widens who
  may receive content.
- Which local model is uncensored stays machine-local knowledge, out of git,
  like the rest of model identity (`docs/hot-cold-policy.md`).
- `private_cloud` is a claim about a contract, not a default. No upstream
  ships declared as `private_cloud`; the founder declares one only against
  recorded evidence of the guarantee.
- The ladder is founder taste (ADR 0007). Agents propose amendments to
  `local_model_router/disclosure/disclosure.yaml`; they do not invent rungs.
