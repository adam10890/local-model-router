"""Resolve an executor to its declared trust tier.

An *executor* is anything Imperium can hand a task to: a local fleet slot, a
configured upstream, or a library agent. Each declares ``trust_tier`` where
its identity already lives — slots in the machine-local fleet YAML, upstreams
in ``conf/upstreams.yaml``, agents in ``conf/agents.yaml``.

An executor that declares nothing resolves to the policy's
``default_executor_tier``. That default is the *least* trusted rung: missing
configuration must never widen who may receive content.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from .policy import DisclosurePolicy

KIND_SLOT = "slot"
KIND_UPSTREAM = "upstream"
KIND_AGENT = "agent"
KIND_UNKNOWN = "unknown"


@dataclass(frozen=True)
class Executor:
    """One resolved handoff target."""

    id: str
    kind: str
    tier: str
    declared: bool
    rank: int

    def describe(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "trust_tier": self.tier,
            "declared": self.declared,
            "rank": self.rank,
        }


def _declared_tier(source: Any) -> str:
    """Read a ``trust_tier`` declaration off a config mapping or object."""
    if isinstance(source, Mapping):
        raw = source.get("trust_tier")
    else:
        raw = getattr(source, "trust_tier", None)
    return str(raw or "").strip().lower()


def resolve_executor(
    policy: DisclosurePolicy,
    *,
    executor_id: str,
    kind: str = KIND_UNKNOWN,
    config: Any = None,
) -> Executor:
    """Resolve one executor against *policy*.

    A declared tier the policy does not know is treated as undeclared, so a
    typo cannot invent a more trusted rung than the ladder contains.
    """
    declared = _declared_tier(config)
    known = policy.tier(declared) if declared else None
    tier = known.id if known is not None else policy.default_executor_tier
    return Executor(
        id=str(executor_id or "").strip() or "unknown",
        kind=str(kind or KIND_UNKNOWN).strip().lower() or KIND_UNKNOWN,
        tier=tier,
        declared=known is not None,
        rank=policy.rank_of(tier),
    )


def resolve_upstream(policy: DisclosurePolicy, upstream: Any) -> Executor:
    """Resolve a ``UpstreamConfig`` (or its ``describe()`` mapping)."""
    name = upstream.get("name") if isinstance(upstream, Mapping) else getattr(upstream, "name", "")
    return resolve_executor(policy, executor_id=str(name or ""), kind=KIND_UPSTREAM, config=upstream)


def resolve_slot(policy: DisclosurePolicy, slot: Any) -> Executor:
    """Resolve a fleet slot config mapping."""
    if isinstance(slot, Mapping):
        slot_id = slot.get("id") or slot.get("slot_id") or ""
    else:
        slot_id = getattr(slot, "slot_id", "") or getattr(slot, "id", "")
    return resolve_executor(policy, executor_id=str(slot_id or ""), kind=KIND_SLOT, config=slot)


def resolve_agent(policy: DisclosurePolicy, agent: Any) -> Executor:
    """Resolve a library agent definition."""
    agent_id = agent.get("id") if isinstance(agent, Mapping) else getattr(agent, "id", "")
    return resolve_executor(policy, executor_id=str(agent_id or ""), kind=KIND_AGENT, config=agent)


def find_upstream_executor(
    policy: DisclosurePolicy, upstreams: Any, name: Optional[str]
) -> Executor:
    """Resolve the named upstream from an iterable, or an undeclared fallback."""
    target = str(name or "").strip().lower()
    for upstream in upstreams or ():
        candidate = (
            upstream.get("name") if isinstance(upstream, Mapping) else getattr(upstream, "name", "")
        )
        if str(candidate or "").strip().lower() == target and target:
            return resolve_upstream(policy, upstream)
    return resolve_executor(policy, executor_id=target, kind=KIND_UPSTREAM, config=None)
