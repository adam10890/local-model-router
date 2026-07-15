"""Built-in agent catalog and router-backed Pydantic AI runner."""
from __future__ import annotations

import asyncio
import os
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from local_model_router.routing.catalog import VALID_STRATEGIES

AGENT_BASE_URL_ENV = "A0_LMM_ROUTER_AGENT_BASE_URL"
AGENT_API_KEY_ENV = "A0_LMM_ROUTER_API_KEY"
AGENT_INPUT_MAX_BYTES = 64 * 1024
AGENT_RUN_TIMEOUT_SECONDS = 120
_SUPPORTED_ROLES = frozenset({"chat", "utility", "scribe"})


class AgentConfigError(ValueError):
    """Raised when the local agent catalog is invalid."""


class AgentRunnerUnavailable(RuntimeError):
    """Raised when the optional runner or its self-call URL is unavailable."""


class AgentRunTimeout(RuntimeError):
    """Raised when an agent run exceeds its request budget."""


class AgentRunFailed(RuntimeError):
    """Raised when the model provider cannot complete an agent run."""


class AgentRoutingIntent(BaseModel):
    """Routing metadata sent to the router's OpenAI-compatible endpoint."""

    model_config = ConfigDict(extra="forbid")

    role: str
    task_type: str
    routing_strategy: str = "balanced_local"
    local_only: bool = False

    @field_validator("role", "task_type", "routing_strategy")
    @classmethod
    def _normalized_value(cls, value: str) -> str:
        normalized = value.strip().lower().replace("-", "_")
        if not normalized:
            raise ValueError("routing values must not be empty")
        return normalized

    @field_validator("role")
    @classmethod
    def _supported_role(cls, value: str) -> str:
        if value not in _SUPPORTED_ROLES:
            raise ValueError(f"role must be one of {sorted(_SUPPORTED_ROLES)}")
        return value

    @field_validator("routing_strategy")
    @classmethod
    def _supported_strategy(cls, value: str) -> str:
        if value not in VALID_STRATEGIES:
            raise ValueError(f"routing_strategy must be one of {sorted(VALID_STRATEGIES)}")
        return value


class AgentDefinition(BaseModel):
    """One operator-defined, prompt-backed agent entry."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    system_prompt: str = Field(min_length=1)
    routing: AgentRoutingIntent

    @field_validator("id")
    @classmethod
    def _safe_id(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in normalized):
            raise ValueError("id must contain only lowercase letters, numbers, and hyphens")
        return normalized

    @field_validator("name", "description", "system_prompt")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("agent text must not be blank")
        return value.strip()

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "routing": self.routing.model_dump(),
        }

    def routing_payload(self) -> dict[str, Any]:
        return {
            "agent_id": self.id,
            "agent_type": "custom",
            **self.routing.model_dump(),
        }


class AgentCatalog:
    """Small immutable catalog loaded once during app construction."""

    def __init__(self, definitions: list[AgentDefinition]) -> None:
        self._definitions = {definition.id: definition for definition in definitions}

    @classmethod
    def load(cls, path: str | Path) -> "AgentCatalog":
        source = Path(path)
        if not source.exists():
            return cls([])
        try:
            raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise AgentConfigError("could not load agents catalog") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("agents"), list):
            raise AgentConfigError("agents catalog must contain an 'agents' list")
        try:
            definitions = [AgentDefinition.model_validate(item) for item in raw["agents"]]
        except ValidationError as exc:
            raise AgentConfigError("agents catalog contains an invalid agent") from exc
        if len({definition.id for definition in definitions}) != len(definitions):
            raise AgentConfigError("agents catalog contains duplicate ids")
        return cls(definitions)

    @classmethod
    def load_packaged(cls) -> "AgentCatalog":
        with as_file(files("local_model_router.service").joinpath("agents.yaml")) as path:
            return cls.load(path)

    def get(self, agent_id: str) -> Optional[AgentDefinition]:
        return self._definitions.get(agent_id)

    def public_list(self) -> list[dict[str, Any]]:
        return [self._definitions[agent_id].public_dict() for agent_id in sorted(self._definitions)]


def model_settings(definition: AgentDefinition) -> dict[str, Any]:
    """Pydantic AI settings that preserve the router's intent contract."""
    return {
        "extra_body": {"routing": definition.routing_payload()},
        "extra_headers": {"X-App-Id": "agent_library"},
    }


def _base_url() -> str:
    base_url = os.environ.get(AGENT_BASE_URL_ENV, "").strip().rstrip("/")
    if not base_url or not base_url.endswith("/v1"):
        raise AgentRunnerUnavailable()
    return base_url


async def run_agent(definition: AgentDefinition, user_input: str) -> str:
    """Run an agent through this router's Chat Completions endpoint."""
    base_url = _base_url()
    try:
        from pydantic_ai import Agent
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
    except ImportError as exc:
        raise AgentRunnerUnavailable() from exc

    try:
        model = OpenAIChatModel(
            "auto",
            provider=OpenAIProvider(
                base_url=base_url,
                api_key=os.environ.get(AGENT_API_KEY_ENV, "").strip() or "local",
            ),
        )
        agent = Agent(model, system_prompt=definition.system_prompt)
        result = await asyncio.wait_for(
            agent.run(user_input, model_settings=model_settings(definition)),
            timeout=AGENT_RUN_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise AgentRunTimeout() from exc
    except Exception as exc:
        raise AgentRunFailed() from exc
    return str(result.output)
