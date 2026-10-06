from collections.abc import Callable, Iterable
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from horizon.domain.events import Event, NewEvent
from horizon.domain.model import (
    CampaignAttempt,
    CampaignBudget,
    CampaignSummary,
    ModelRequest,
    ModelResponse,
    ToolDefinition,
)
from horizon.domain.plan import WorkItem
from horizon.domain.retrieval import EvidencePack
from horizon.domain.run import Run
from horizon.domain.task import AcceptanceCheck, TaskSpec
from horizon.domain.tools import (
    AcceptanceResult,
    CreateRecoveryAssessment,
    PatchRecoveryAssessment,
    ReplaceRecoveryAssessment,
    ToolOutcome,
)


class EventStorePort(Protocol):
    clock: Callable[[], datetime]

    def create(self, task: TaskSpec, key: str) -> Run: ...
    def get(self, run_id: str, at: int | None = None) -> Run: ...
    def events(self, run_id: str, at: int | None = None) -> list[Event]: ...
    def command(
        self,
        run_id: str,
        key: str,
        request: dict[str, Any],
        decide: Callable[[Run], Iterable[NewEvent]],
        expected_seq: int | None = None,
    ) -> Run: ...


class ModelGatewayPort(Protocol):
    def generate(self, request: ModelRequest, trace_id: str) -> ModelResponse: ...


class ArtifactStorePort(Protocol):
    def put(self, content: bytes) -> str: ...
    def read(self, sha256: str, max_bytes: int = 64 * 1024 * 1024) -> bytes: ...


class CampaignBudgetPort(Protocol):
    def initialize(
        self,
        budget: CampaignBudget,
        *,
        provider_id: str,
        model_id: str,
    ) -> CampaignSummary: ...

    def reserve(
        self,
        budget: CampaignBudget,
        attempt_id: str,
        request_hash: str,
        amount: Decimal,
    ) -> CampaignSummary: ...

    def settle(
        self,
        campaign_id: str,
        attempt_id: str,
        actual_cost: Decimal,
        provider_trace_id: str | None,
    ) -> CampaignSummary: ...

    def mark_unknown(
        self,
        campaign_id: str,
        attempt_id: str,
        error_type: str,
    ) -> CampaignSummary: ...

    def attempt(self, campaign_id: str, attempt_id: str) -> CampaignAttempt: ...

    def attempts(self, campaign_id: str) -> tuple[CampaignAttempt, ...]: ...

    def summary(self, campaign_id: str) -> CampaignSummary: ...


class AcceptanceExecutorPort(Protocol):
    def execute(self, workspace: Path, check: AcceptanceCheck) -> AcceptanceResult: ...


class ToolGatewayPort(Protocol):
    @property
    def definitions(self) -> tuple[ToolDefinition, ...]: ...

    def current_revision(self) -> str: ...
    def activate_work_item(self, work_item: WorkItem) -> None: ...
    def dispatch_safe(
        self,
        name: str,
        arguments: dict[str, Any],
        attempt_id: str | None = None,
    ) -> ToolOutcome: ...
    def dispatch_protected_check(
        self,
        check_id: str,
        attempt_id: str | None = None,
    ) -> ToolOutcome: ...
    def cleanup_check_attempt(self, attempt_id: str) -> bool: ...
    def checkpoint(self) -> tuple[str, str]: ...
    def verify_manifest(self, manifest_hash: str) -> Any: ...
    def validation_evidence(self, results: tuple[AcceptanceResult, ...]) -> str: ...


class WriteRecoveryPort(Protocol):
    def assess_replace_recovery(
        self,
        arguments: dict[str, Any],
        pre_manifest_ref: str,
    ) -> ReplaceRecoveryAssessment: ...

    def rollback_replace_recovery(
        self,
        arguments: dict[str, Any],
        pre_manifest_ref: str,
        expected_current_revision: str,
    ) -> tuple[str, str]: ...

    def assess_patch_recovery(
        self,
        arguments: dict[str, Any],
        pre_manifest_ref: str,
    ) -> PatchRecoveryAssessment: ...

    def rollback_patch_recovery(
        self,
        arguments: dict[str, Any],
        pre_manifest_ref: str,
        expected_current_revision: str,
    ) -> tuple[str, str]: ...

    def assess_create_recovery(
        self,
        arguments: dict[str, Any],
        pre_manifest_ref: str,
    ) -> CreateRecoveryAssessment: ...

    def rollback_create_recovery(
        self,
        arguments: dict[str, Any],
        pre_manifest_ref: str,
        expected_current_revision: str,
    ) -> tuple[str, str]: ...


class CodeRetrievalPort(Protocol):
    def retrieve(
        self,
        *,
        source_manifest_ref: str,
        workspace_revision: str,
        allowed_paths: tuple[str, ...],
        denied_paths: tuple[str, ...],
        query: str,
        max_chunks: int,
    ) -> EvidencePack: ...
