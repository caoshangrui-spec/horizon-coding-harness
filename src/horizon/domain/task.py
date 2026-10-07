from __future__ import annotations

from decimal import Decimal
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import Field, StrictInt, field_validator, model_serializer, model_validator

from horizon.domain.common import Contract, digest

Identifier = Annotated[str, Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.-]+$")]
Text = Annotated[str, Field(min_length=1)]
PositiveInt = Annotated[StrictInt, Field(gt=0)]
NonNegativeInt = Annotated[StrictInt, Field(ge=0)]
Mode = Literal["read_only", "plan_only", "workspace_write"]
READ_ONLY_MODEL_TOOLS = ("search_repo", "read_file", "retrieve_code")
WORKSPACE_WRITE_MODEL_TOOLS = (
    "search_repo",
    "read_file",
    "retrieve_code",
    "replace_text",
    "apply_patch",
    "create_file",
    "run_check",
)


def model_tools_for_mode(mode: Mode) -> tuple[str, ...]:
    if mode == "workspace_write":
        return WORKSPACE_WRITE_MODEL_TOOLS
    return READ_ONLY_MODEL_TOOLS


def relative_pattern(value: str) -> str:
    parts = value.split("/")
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or any(part in {"", ".", ".."} for part in parts)
        or PurePosixPath(value).is_absolute()
    ):
        raise ValueError("Paths must be relative POSIX paths without traversal or drive prefixes")
    return value


class Repository(Contract):
    source: Literal["local", "git"]
    base_commit: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
    path: str | None = None
    url: str | None = None

    @model_validator(mode="after")
    def check_source(self) -> Self:
        if self.source == "local" and (not self.path or self.url is not None):
            raise ValueError("A local repository needs path and no url")
        if self.source == "git" and (not self.url or self.path is not None):
            raise ValueError("A git repository needs url and no path")
        return self


class Constraints(Contract):
    allowed_paths: tuple[str, ...] = ("**",)
    denied_paths: tuple[str, ...] = (".git/**", ".env", ".env.*", "secrets/**")
    allowed_tools: tuple[Identifier, ...] | None = None
    network: Literal["deny", "allow"] = "deny"
    requirements: tuple[str, ...] = ()

    @field_validator("allowed_paths", "denied_paths")
    @classmethod
    def check_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            relative_pattern(value)
        return values

    @field_validator("allowed_tools")
    @classmethod
    def check_allowed_tools(
        cls,
        values: tuple[Identifier, ...] | None,
    ) -> tuple[Identifier, ...] | None:
        if values is not None and (not values or len(values) != len(set(values))):
            raise ValueError("Tool allowlist must be nonempty and unique when provided")
        return values

    @model_serializer(mode="wrap")
    def omit_legacy_tool_default(self, handler):
        """Keep historical TaskSpec hashes stable when no tool allowlist was supplied."""

        data = handler(self)
        if self.allowed_tools is None:
            data.pop("allowed_tools", None)
        return data


class AcceptanceCheck(Contract):
    id: Identifier
    kind: Literal["command"] = "command"
    command: Text
    timeout_seconds: PositiveInt = 300
    required: bool = True


class BudgetSpec(Contract):
    max_steps: PositiveInt
    max_model_calls: PositiveInt
    max_tool_calls: PositiveInt
    max_wall_time_seconds: PositiveInt
    max_cost_usd: Annotated[Decimal, Field(gt=0)]
    max_input_tokens: PositiveInt = 200_000
    max_output_tokens: PositiveInt = 30_000
    max_repair_cycles: NonNegativeInt = 4
    soft_ratio: Annotated[float, Field(gt=0, lt=1)] = 0.8
    unknown_cost_policy: Literal["block", "reserve"] = "block"


class TaskSpec(Contract):
    schema_version: Literal["1.0"] = "1.0"
    spec_version: PositiveInt = 1
    task_id: Identifier
    title: Text
    objective: Text
    repository: Repository
    constraints: Constraints = Constraints()
    acceptance: Annotated[tuple[AcceptanceCheck, ...], Field(min_length=1)]
    budgets: BudgetSpec
    task_kind: Literal["bugfix", "feature", "refactor", "tests", "explain", "review", "unknown"] = (
        "unknown"
    )
    execution_mode: Mode = "read_only"
    authority_scope: Mode = "read_only"
    model_policy_id: Identifier = "unconfigured"
    memory_scope: Identifier = "isolated"

    @model_validator(mode="after")
    def check_contract(self) -> Self:
        ids = [check.id for check in self.acceptance]
        if len(set(ids)) != len(ids):
            raise ValueError("Acceptance IDs must be unique")
        if not any(check.required for check in self.acceptance):
            raise ValueError("At least one required acceptance check is needed")
        if self.execution_mode == "workspace_write" and self.authority_scope != "workspace_write":
            raise ValueError("Execution mode cannot grant write authority")
        if self.task_kind in {"explain", "review"} and self.execution_mode == "workspace_write":
            raise ValueError("Explanation and review tasks cannot implicitly grant writes")
        if self.execution_mode == "workspace_write" and not self.constraints.allowed_paths:
            raise ValueError("Write mode requires an explicit nonempty path scope")
        allowed_tools = self.constraints.allowed_tools
        if allowed_tools is not None and not set(allowed_tools) <= set(
            model_tools_for_mode(self.execution_mode)
        ):
            raise ValueError("Tool allowlist exceeds the task execution mode")
        return self

    @property
    def sha256(self) -> str:
        return digest(self)
