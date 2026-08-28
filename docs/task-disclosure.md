# Task disclosure policy

**Status:** Accepted and shipped (runtime enforcement is opt-in)
**Owner:** Product / founder
**Decisions:** ADR 0007, 0013, 0014
**Code:** [`local_model_router/disclosure/`](../local_model_router/disclosure/AGENTS.md)

Imperium is the endpoint through which tasks pass between agents and
harnesses. Every handoff is a disclosure decision: *this content*, to *that
executor*. This document is the operator policy for that decision, and the
packaged rules in `local_model_router/disclosure/disclosure.yaml` are its
machine-readable form.

## The rule in one line

A cloud model builds a generic skeleton against product requirements. We
integrate the data, the names, and the intent locally.

## Two axes

A disclosure decision needs both. Neither alone is sufficient: "it is only a
cloud model" says nothing about the content, and "it is only boilerplate" says
nothing about who receives it.

### Axis A — executor trust ladder

Most trusted first. An **ordering, not a routing chain**: content does not
have to pass through every rung, and the tier only caps who may receive it.

| # | Trust tier | Meaning |
| --- | --- | --- |
| 1 | `local_uncensored` | Local model, no alignment filtering. |
| 2 | `local_aligned` | Local model with vendor alignment. |
| 3 | `private_cloud` | Cloud provider under a privacy/zero-retention guarantee. |
| 4 | `other_provider` | Any other provider. |

Declare a tier where the executor's identity already lives:

- **Slots** — `trust_tier:` in the machine-local fleet YAML. Which local model
  is uncensored is machine-local knowledge and stays out of git, like the rest
  of model identity (`hot-cold-policy.md`).
- **Upstreams** — `trust_tier:` in `conf/upstreams.yaml`.
- **Agents** — agents inherit the tier of the slot or upstream that serves
  them; `trust_tier:` on an agent may cap it further, never raise it.

**An executor that declares nothing is treated as `other_provider`.** Missing
or misspelled configuration fails closed.

`private_cloud` is a claim about a contract, not a default. Nothing ships
declared as `private_cloud`; declare one only against recorded evidence of the
guarantee.

### Axis B — content class

Each class caps the least-trusted executor allowed, and fixes the form the
content takes when it is sent.

| Content class | Example | Least-trusted executor allowed | Form |
| --- | --- | --- | --- |
| `generic_scaffold` | boilerplate module, parser, adapter shell | `other_provider` | `skeleton_only` |
| `algorithm_generic` | scoring/caching logic on synthetic input | `other_provider` | `skeleton_only` |
| `product_feature` | a behavior in a product surface | `private_cloud` | `requirements_only` |
| `integration_glue` | wiring to a named third party | `private_cloud` | `requirements_only` |
| `routing_policy` | ranking, admission, failover logic | `local_aligned` | `redacted_context` |
| `operator_data` | real fleet config, model ids, hardware, telemetry | `local_uncensored` | `full` |
| `security_surface` | auth, keys, bind posture, installer trust | `local_uncensored` | `full` |

Forms, most open to most closed:

1. `skeleton_only` — an abstract spec; names, data, and wiring are added
   locally afterwards.
2. `requirements_only` — requirements and acceptance criteria only. No
   rationale, roadmap, business goal, or competitive intent.
3. `redacted_context` — minimal internal context with every identifier
   replaced by a placeholder (`SERVICE_A`, `MODEL_X`, `PORT_N`).
4. `full` — unrestricted, and only for the rung the class permits.

## Writing a brief

- State **what** must be true, never **why** the product wants it.
- Give interface shapes, not real names. Placeholders only.
- Supply synthetic examples. Real data is integrated locally afterwards.
- Never include file paths, hostnames, ports, model ids, hardware specs,
  customer names, secrets, or prompt bodies.
- Ask for a dependency-light generic implementation we can adapt.
- Declare the content class, and say what we will add locally after delivery.

Start from the template rather than a blank file:

```bash
local-model-router disclosure --template generic_scaffold > brief.md
# fill it in, then:
local-model-router disclosure --check brief.md --target <executor>
```

`--check` exits non-zero when a required section is missing, when forbidden
content is present, or when the target is below the content's cap. Findings
name the pattern and the line — **never the matched text**, so the output of a
leak check is itself safe to paste.

Other commands: `--list` prints the ladder and the classes; `--classify FILE`
reports the class and the evidence behind it; `--json` renders any of them as
structured output.

## Runtime behavior

The router evaluates the same policy where it forwards to a declared upstream.
Every such response carries:

| Header | Meaning |
| --- | --- |
| `x-a0-router-trust-tier` | the executor's resolved tier |
| `x-a0-router-disclosure` | `allow` or `deny` |
| `x-a0-router-disclosure-class` | the inferred content class |

A completed routing decision also carries an `executor_tier:<tier>` reason
code, so a caller can see how far its content travelled.

**The default is observe-only.** A denied handoff is reported and still
forwarded, so nothing about existing routing changes. Set
`A0_LMM_ROUTER_DISCLOSURE_ENFORCE=1` to make a denial a
`403 disclosure_policy_violation` instead. The flag is read per request.

Prompt bodies are never logged, stored, or returned by any of this. The
message text is classified and scanned in memory and dropped; only pattern
ids, counts, line numbers, and class names ever leave the evaluation.

## Overriding the rules

Place `conf/disclosure.yaml` next to `apps.yaml` to override the packaged
rules. A malformed override is rejected rather than silently replaced by more
permissive defaults — the CLI fails loudly, and the service logs a warning and
falls back to the packaged rules.

Content classes, trust tiers, and forbidden-pattern families are founder taste
(ADR 0007). Amend the rules file and this document together.

## Verification

- `python -m pytest tests/test_disclosure_policy.py tests/test_disclosure_trust.py tests/test_disclosure_cli.py tests/test_disclosure_gate.py -q`
- The full 7×4 content-class / trust-tier matrix is asserted cell by cell in
  `tests/test_disclosure_trust.py`.
