from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

from pydantic import Field, ValidationError

from horizon.application.model_probe import conservative_input_sizing
from horizon.application.model_recovery import (
    RecoverableModelTurn,
    load_recorded_model_response,
    persist_model_response,
    quarantine_unsettled_model_call,
)
from horizon.application.services import HarnessService, LeaseToken
from horizon.domain.budget import Usage
from horizon.domain.common import Contract, canonical_json, digest
from horizon.domain.errors import (
    BudgetExceeded,
    Conflict,
    IntegrityError,
    PlanProposalError,
    PolicyDenied,
    ProviderError,
)
from horizon.domain.model import (
    CampaignBudget,
    InputTokenBudget,
    ModelCallRecord,
    ModelCallReservation,
    ModelMessage,
    ModelPolicyBinding,
    ModelRequest,
    ModelResponse,
    PriceCard,
    ToolDefinition,
)
from horizon.domain.plan import Plan, permitted_plan_tools
from horizon.domain.planning import PlanningAcceptance, PlanningContext
from horizon.domain.ports import ArtifactStorePort, CampaignBudgetPort, ModelGatewayPort
from horizon.domain.run import Run
from horizon.domain.states import RunStatus
from horizon.domain.task import TaskSpec, relative_pattern

PLAN_TOOL_NAME = "propose_plan"
DEFAULT_MAX_INVENTORY_PATHS = 50
REPOSITORY_PATH_MENTION = re.compile(
    r"(?<![A-Za-z0-9_.-])"
    r"((?:[A-Za-z0-9_.-]+[\\/])+[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,12})"
    r"(?![A-Za-z0-9_.-])"
)


class PlanGeneratorConfig(Contract):
    max_work_items: int = Field(default=8, ge=1, le=20)
    max_inventory_paths: int = Field(default=DEFAULT_MAX_INVENTORY_PATHS, ge=1, le=2_000)
    max_output_tokens: int = Field(default=1_024, ge=128, le=8_192)
    max_input_tokens: int = Field(default=120_000, ge=2_000, le=2_000_000)
    max_run_cost: Decimal = Field(default=Decimal("1.00"), gt=0)
    enable_thinking: bool = False


@dataclass(frozen=True)
class PlanGenerationResult:
    run: Run
    plan: Plan
    model_call_id: str
    response_artifact_ref: str
    planning_context_ref: str
    reused_response: bool


def build_planning_context(
    task: TaskSpec,
    *,
    workspace_revision: str,
    source_manifest_ref: str,
    repository_paths: Iterable[str],
    max_work_items: int = 8,
    max_inventory_paths: int = DEFAULT_MAX_INVENTORY_PATHS,
) -> PlanningContext:
    if max_work_items < 1 or max_inventory_paths < 1:
        raise ValueError("Planning limits must be positive")
    visible: set[str] = set()
    for path in repository_paths:
        relative_pattern(path)
        allowed = any(
            fnmatch.fnmatchcase(path, pattern) for pattern in task.constraints.allowed_paths
        )
        denied = any(
            fnmatch.fnmatchcase(path, pattern) for pattern in task.constraints.denied_paths
        )
        if allowed and not denied:
            visible.add(path)
    ordered = tuple(sorted(visible))
    return PlanningContext(
        task_spec_hash=task.sha256,
        task_title=task.title,
        objective=task.objective,
        task_kind=task.task_kind,
        execution_mode=task.execution_mode,
        allowed_paths=task.constraints.allowed_paths,
        denied_paths=task.constraints.denied_paths,
        requirements=task.constraints.requirements,
        acceptance=tuple(
            PlanningAcceptance(id=check.id, required=check.required) for check in task.acceptance
        ),
        workspace_revision=workspace_revision,
        source_manifest_ref=source_manifest_ref,
        repository_paths=ordered[:max_inventory_paths],
        repository_path_count=len(ordered),
        repository_paths_truncated=len(ordered) > max_inventory_paths,
        max_work_items=max_work_items,
        permitted_tools=permitted_plan_tools(task),
    )


def _plan_tool(context: PlanningContext) -> ToolDefinition:
    acceptance_ids = [item.id for item in context.acceptance]
    tools = list(context.permitted_tools)
    return ToolDefinition(
        name=PLAN_TOOL_NAME,
        description=(
            "Propose one bounded dependency DAG. The controller validates all IDs, tools, "
            "unique acceptance ownership, coverage, and dependencies before accepting it. "
            "Every WorkItem is protected-validated when submitted."
        ),
        parameters={
            "type": "object",
            "properties": {
                "version": {"type": "integer", "const": 1},
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": context.max_work_items,
                    "items": {
                        "type": "object",
                        "properties": {
                            "work_item_id": {"type": "string"},
                            "title": {"type": "string"},
                            "objective": {"type": "string"},
                            "dependencies": {
                                "type": "array",
                                "maxItems": context.max_work_items,
                                "items": {"type": "string"},
                            },
                            "expected_artifacts": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 16,
                                "items": {
                                    "type": "string",
                                    "description": (
                                        "Unless the immutable task explicitly names an exact "
                                        "repository path, describe the evidence or behavior "
                                        "generically; inventory membership alone does not prove "
                                        "an implementation location."
                                    ),
                                },
                            },
                            "acceptance_ids": {
                                "type": "array",
                                "minItems": 1,
                                "items": {"type": "string", "enum": acceptance_ids},
                            },
                            "allowed_tools": {
                                "type": "array",
                                "minItems": 1,
                                "items": {"type": "string", "enum": tools},
                            },
                        },
                        "required": [
                            "work_item_id",
                            "title",
                            "objective",
                            "dependencies",
                            "expected_artifacts",
                            "acceptance_ids",
                            "allowed_tools",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["version", "items"],
            "additionalProperties": False,
        },
    )


def build_plan_request(
    model_id: str,
    context: PlanningContext,
    *,
    max_output_tokens: int,
    enable_thinking: bool,
) -> ModelRequest:
    return ModelRequest(
        model=model_id,
        messages=(
            ModelMessage(
                role="system",
                content=(
                    "You plan a bounded software-engineering run. Call propose_plan exactly "
                    "once. Use only supplied acceptance IDs and tools. Keep items independently "
                    "verifiable, order dependencies explicitly, and do not expand authority. "
                    "Assign each acceptance ID to exactly one WorkItem. The controller validates "
                    "every WorkItem on submit, so fold investigation and verification into the "
                    "WorkItem that can make its acceptance pass; never create standalone locate "
                    "or verify items that reuse the same final check. For a simple task with one "
                    "acceptance check, prefer one end-to-end WorkItem. Repository inventory "
                    "proves only that a path exists, not that it contains the implementation. "
                    "Unless the immutable task explicitly names a path, do not guess an exact "
                    "implementation path in objectives or expected_artifacts; inventory "
                    "membership alone is not evidence. Describe the evidence-discovered source "
                    "change instead. Existing-file writes remain exact and bounded. The "
                    "create_file tool may create one new file inside the supplied "
                    "path scope, but only in an already-existing parent directory; use it only "
                    "when the immutable task actually requires a new path."
                ),
            ),
            ModelMessage(
                role="user",
                content=(
                    "Propose a minimal plan for this immutable planning context:\n"
                    f"{canonical_json(context)}"
                ),
            ),
        ),
        tools=(_plan_tool(context),),
        tool_choice="required",
        max_output_tokens=max_output_tokens,
        temperature=Decimal("0"),
        enable_thinking=enable_thinking,
    )


class PlanGenerator:
    """One-shot, budgeted planner with durable response reuse and no implicit retry."""

    def __init__(
        self,
        service: HarnessService,
        model: ModelGatewayPort,
        campaign_ledger: CampaignBudgetPort,
        artifact_store: ArtifactStorePort,
        *,
        provider_id: str,
        model_id: str,
        pricing: PriceCard,
        campaign: CampaignBudget,
        config: PlanGeneratorConfig | None = None,
    ):
        self.service = service
        self.model = model
        self.campaign_ledger = campaign_ledger
        self.artifact_store = artifact_store
        self.provider_id = provider_id
        self.model_id = model_id
        self.pricing = pricing
        self.campaign = campaign
        self.config = config or PlanGeneratorConfig()

    def _store_context(self, context: PlanningContext) -> str:
        payload = canonical_json(context).encode("utf-8")
        artifact_ref = self.artifact_store.put(payload)
        if artifact_ref != context.sha256 or self.artifact_store.read(artifact_ref) != payload:
            raise Conflict("Planning context artifact verification failed")
        return artifact_ref

    def _historical_reservations(self, run: Run) -> dict[str, ModelCallReservation]:
        found: dict[str, ModelCallReservation] = {}
        for event in self.service.store.events(run.run_id):
            if event.event_type != "MODEL_CALL_RESERVED":
                continue
            reservation = ModelCallReservation.model_validate(event.payload["reservation"])
            if reservation.call_id in found:
                raise IntegrityError("Duplicate historical model reservation")
            found[reservation.call_id] = reservation
        return found

    def _reusable_response(
        self,
        run: Run,
        request: ModelRequest,
        context_ref: str,
    ) -> tuple[ModelCallRecord, ModelResponse] | None:
        planning_records = [record for record in run.model_calls if record.purpose == "planning"]
        if not planning_records:
            return None
        if len(planning_records) != 1:
            raise Conflict("Automatic planning is one-shot; multiple planning receipts exist")
        record = planning_records[0]
        if record.request_hash != request.sha256:
            raise Conflict("Persisted planning receipt belongs to a different request")
        reservation = self._historical_reservations(run).get(record.call_id)
        if (
            reservation is None
            or reservation.purpose != "planning"
            or reservation.planning_context_ref != context_ref
            or reservation.planning_context_hash != context_ref
        ):
            raise IntegrityError("Planning receipt has no matching context-bound intent")
        if (
            reservation.request_payload is not None
            and request.openai_compatible_payload_evidence() != reservation.request_payload
        ):
            raise IntegrityError("Planning receipt has mismatched request payload evidence")
        if run.model_policy is None:
            raise IntegrityError("Planning receipt has no model policy")
        attempt = self.campaign_ledger.attempt(run.model_policy.campaign_id, record.call_id)
        if (
            attempt.status != "settled"
            or attempt.request_hash != reservation.request_hash
            or attempt.reserved_cost != reservation.reserved_cost
            or attempt.actual_cost != record.estimated_cost
            or attempt.provider_trace_id != record.provider_trace_id
        ):
            raise Conflict("Planning response requires reconciled campaign settlement")
        response = load_recorded_model_response(
            RecoverableModelTurn(reservation=reservation, record=record),
            self.artifact_store,
        )
        return record, response

    def _parse_plan(
        self,
        response: ModelResponse,
        task: TaskSpec,
        context: PlanningContext,
    ) -> Plan:
        calls = response.message.tool_calls
        if len(calls) != 1 or calls[0].function.name != PLAN_TOOL_NAME:
            raise PlanProposalError("Planner must call propose_plan exactly once")
        try:
            plan = Plan.model_validate(calls[0].function.arguments)
            if plan.version != 1 or len(plan.items) > self.config.max_work_items:
                raise ValueError("Generated Plan exceeds its fixed version or item limit")
            permitted = set(permitted_plan_tools(task))
            if any(not set(item.allowed_tools) <= permitted for item in plan.items):
                raise ValueError("Generated Plan requested a tool outside controller policy")
            if not context.repository_paths_truncated:
                repository_paths = set(context.repository_paths)
                missing_paths: set[str] = set()
                for item in plan.items:
                    for value in (item.title, item.objective, *item.expected_artifacts):
                        for match in REPOSITORY_PATH_MENTION.finditer(value):
                            path = match.group(1)
                            if path in repository_paths:
                                continue
                            try:
                                relative_pattern(path)
                                valid_relative_path = True
                            except ValueError:
                                valid_relative_path = False
                            allowed_create = (
                                valid_relative_path
                                and "create_file" in item.allowed_tools
                                and any(
                                    fnmatch.fnmatchcase(path, pattern)
                                    for pattern in task.constraints.allowed_paths
                                )
                                and not any(
                                    fnmatch.fnmatchcase(path, pattern)
                                    for pattern in task.constraints.denied_paths
                                )
                            )
                            if not allowed_create:
                                missing_paths.add(path)
                missing_paths = sorted(missing_paths)
                if missing_paths:
                    raise ValueError(
                        "Generated Plan names path(s) absent from the complete repository "
                        f"inventory: {', '.join(missing_paths)}"
                    )
            plan.check_task(task)
        except (PolicyDenied, ValidationError, ValueError) as exc:
            raise PlanProposalError(f"Planner proposal failed validation: {exc}") from exc
        return plan

    def generate_and_set(
        self,
        run_id: str,
        token: LeaseToken,
        context: PlanningContext,
    ) -> PlanGenerationResult:
        run = self.service.store.get(run_id)
        self.service.check_worker(run, token)
        if run.status != RunStatus.PLANNING or run.plan is not None:
            raise Conflict("Automatic planning requires an unplanned PLANNING Run")
        if run.workspace_origin is None:
            raise Conflict("Automatic planning requires a bound workspace origin")
        expected_acceptance = tuple(
            PlanningAcceptance(id=check.id, required=check.required)
            for check in run.task.acceptance
        )
        if (
            context.task_spec_hash != run.task.sha256
            or context.task_title != run.task.title
            or context.objective != run.task.objective
            or context.task_kind != run.task.task_kind
            or context.execution_mode != run.task.execution_mode
            or context.workspace_revision != run.task.repository.base_commit
            or context.source_manifest_ref != run.workspace_origin.source_manifest_ref
            or context.allowed_paths != run.task.constraints.allowed_paths
            or context.denied_paths != run.task.constraints.denied_paths
            or context.requirements != run.task.constraints.requirements
            or context.acceptance != expected_acceptance
            or context.max_work_items != self.config.max_work_items
            or len(context.repository_paths) > self.config.max_inventory_paths
            or context.permitted_tools != permitted_plan_tools(run.task)
            or any(
                not any(
                    fnmatch.fnmatchcase(path, pattern)
                    for pattern in run.task.constraints.allowed_paths
                )
                or any(
                    fnmatch.fnmatchcase(path, pattern)
                    for pattern in run.task.constraints.denied_paths
                )
                for path in context.repository_paths
            )
        ):
            raise Conflict("Planning context does not match the current Run contract")

        policy = ModelPolicyBinding(
            policy_id=run.task.model_policy_id,
            provider_id=self.provider_id,
            model=self.model_id,
            campaign_id=self.campaign.campaign_id,
            currency=self.pricing.currency,
            max_run_cost=self.config.max_run_cost,
            price_card_hash=digest(self.pricing),
        )
        self.campaign_ledger.initialize(
            self.campaign,
            provider_id=self.provider_id,
            model_id=self.model_id,
        )
        run = self.service.bind_model_policy(
            run_id,
            policy,
            token,
            f"bind_planner_{uuid4().hex}",
        )
        context_ref = self._store_context(context)
        request = build_plan_request(
            self.model_id,
            context,
            max_output_tokens=self.config.max_output_tokens,
            enable_thinking=self.config.enable_thinking,
        )
        input_estimate, request_payload = conservative_input_sizing(request)
        if input_estimate.token_ceiling > self.config.max_input_tokens:
            raise PolicyDenied(
                "Planning request exceeds the configured conservative input-token budget"
            )
        input_token_budget = InputTokenBudget(
            max_input_tokens=self.config.max_input_tokens,
            estimate=input_estimate,
        )
        reusable = self._reusable_response(run, request, context_ref)
        if reusable is not None:
            record, response = reusable
            reused_response = True
        else:
            if run.model_reservations or run.reservations:
                raise Conflict("Unsettled planning operations require reconciliation")
            historical_ids = set(self._historical_reservations(run))
            prefix = f"model_{run.run_id}_"
            campaign_only = [
                attempt
                for attempt in self.campaign_ledger.attempts(self.campaign.campaign_id)
                if attempt.attempt_id.startswith(prefix)
                and attempt.attempt_id not in historical_ids
                and not (
                    attempt.status == "settled"
                    and attempt.actual_cost == 0
                    and attempt.provider_trace_id is None
                )
            ]
            if campaign_only:
                raise Conflict("Campaign-only planning attempt requires reconciliation")
            input_ceiling = input_estimate.token_ceiling
            reserved_cost = self.pricing.reserve_cost(
                input_ceiling,
                request.max_output_tokens,
            )
            call_id = f"model_{run.run_id}_plan_{uuid4().hex}"
            trace_id = f"horizon-{uuid4().hex}"
            reservation = ModelCallReservation(
                call_id=call_id,
                purpose="planning",
                client_trace_id=trace_id,
                request_hash=request.sha256,
                provider_id=self.provider_id,
                model=self.model_id,
                currency=self.pricing.currency,
                reserved_cost=reserved_cost,
                planning_context_ref=context_ref,
                planning_context_hash=context.sha256,
                input_token_budget=input_token_budget,
                request_payload=request_payload,
            )
            request_budget = reservation.budget_evidence(request.max_output_tokens)
            try:
                self.campaign_ledger.reserve(
                    self.campaign,
                    call_id,
                    request.sha256,
                    reserved_cost,
                )
                try:
                    self.service.reserve_model_call(
                        run_id,
                        reservation,
                        Usage(
                            model_calls=1,
                            input_tokens=input_ceiling,
                            output_tokens=request.max_output_tokens,
                        ),
                        token,
                        f"reserve_{call_id}",
                    )
                except BaseException:
                    self.campaign_ledger.settle(
                        self.campaign.campaign_id,
                        call_id,
                        Decimal("0"),
                        None,
                    )
                    raise
            except BudgetExceeded as exc:
                if exc.stop is None:
                    raise
                enriched = BudgetExceeded(
                    str(exc),
                    stop=exc.stop,
                    model_request_budget=request_budget,
                )
                if not self.service.store.get(run_id).reservations:
                    self.service.fail_budget_stop(
                        run_id,
                        exc.stop,
                        token,
                        f"planning_budget_stop_{uuid4().hex}",
                        model_request_budget=request_budget,
                    )
                raise enriched from exc
            try:
                response = self.model.generate(request, trace_id)
            except ProviderError as exc:
                self.service.mark_model_call_unknown(
                    run_id,
                    call_id,
                    token,
                    f"unknown_{call_id}",
                )
                self.campaign_ledger.mark_unknown(
                    self.campaign.campaign_id,
                    call_id,
                    type(exc).__name__,
                )
                raise
            try:
                estimated_cost = self.pricing.cost_for(response.usage)
                response_ref = persist_model_response(response, self.artifact_store)
                record = ModelCallRecord(
                    call_id=call_id,
                    purpose="planning",
                    request_hash=request.sha256,
                    provider_id=self.provider_id,
                    model=response.model,
                    currency=self.pricing.currency,
                    estimated_cost=estimated_cost,
                    response_id=response.response_id,
                    response_artifact_ref=response_ref,
                    provider_trace_id=response.provider_trace_id,
                    finish_reason=response.finish_reason,
                    usage=response.usage,
                )
                run = self.service.settle_model_call(
                    run_id,
                    record,
                    Usage(
                        model_calls=1,
                        input_tokens=response.usage.input_tokens,
                        output_tokens=response.usage.output_tokens,
                    ),
                    token,
                    f"settle_{call_id}",
                )
            except Exception:
                quarantine_unsettled_model_call(
                    self.service,
                    self.campaign_ledger,
                    campaign_id=self.campaign.campaign_id,
                    run_id=run_id,
                    call_id=call_id,
                    token=token,
                    error_type="PostResponseReceiptUnavailable",
                )
                raise
            self.campaign_ledger.settle(
                self.campaign.campaign_id,
                call_id,
                estimated_cost,
                response.provider_trace_id,
            )
            if run.terminal:
                raise Conflict("Planning usage exceeded the Run budget")
            reused_response = False

        plan = self._parse_plan(response, run.task, context)
        completed = self.service.set_plan(
            run_id,
            plan,
            f"generated_plan_{uuid4().hex}",
            token,
            source_model_call_id=record.call_id,
        )
        if record.response_artifact_ref is None:
            raise IntegrityError("Planning model receipt omitted its response artifact")
        return PlanGenerationResult(
            run=completed,
            plan=plan,
            model_call_id=record.call_id,
            response_artifact_ref=record.response_artifact_ref,
            planning_context_ref=context_ref,
            reused_response=reused_response,
        )
