from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Annotated
from uuid import uuid4

from pydantic import Field, ValidationError

from horizon.application.checkpoints import commit_checkpoint
from horizon.application.context import ContextProjector, build_mandatory_fact_ledger
from horizon.application.memory import RunMemoryProjector
from horizon.application.model_probe import (
    conservative_input_estimate,
    conservative_input_sizing,
    legacy_input_estimate,
)
from horizon.application.model_recovery import (
    LEASE_EVENTS,
    events_after_agent_session,
    load_recorded_model_response,
    persist_model_response,
    quarantine_unsettled_model_call,
    recoverable_model_turn,
    recoverable_readonly_tool_turn,
)
from horizon.application.services import HarnessService, LeaseToken
from horizon.domain.agent import AgentSession, AgentSessionRecord
from horizon.domain.budget import Usage
from horizon.domain.common import Contract, canonical_json, digest
from horizon.domain.context import ContextProjection, MandatoryFactLedger
from horizon.domain.errors import BudgetExceeded, Conflict, PolicyDenied, ProviderError
from horizon.domain.human import NoProgressPattern, classify_no_progress
from horizon.domain.memory import RunMemorySnapshot
from horizon.domain.model import (
    CONSERVATIVE_INPUT_TOKEN_ESTIMATOR,
    CampaignBudget,
    ModelCallRecord,
    ModelCallReservation,
    ModelMessage,
    ModelPolicyBinding,
    ModelRequest,
    ModelResponse,
    PriceCard,
    ToolDefinition,
)
from horizon.domain.plan import (
    MAX_EXECUTION_REPLAN_ITEMS,
    MAX_EXECUTION_REPLANS,
    ExecutionReplanProposal,
    WorkItem,
    check_execution_replan,
    permitted_plan_tools,
)
from horizon.domain.ports import (
    ArtifactStorePort,
    CampaignBudgetPort,
    ModelGatewayPort,
    ToolGatewayPort,
)
from horizon.domain.run import Run
from horizon.domain.states import RunStatus
from horizon.domain.tools import AcceptanceResult, ToolCallRecord, ToolCallReservation, ToolOutcome

EXECUTION_REPLAN_TOOL_NAME = "revise_plan"
MIN_EXECUTION_REPLAN_SESSION_MESSAGES = 5


class AgentLoopConfig(Contract):
    max_model_iterations: Annotated[int, Field(ge=1, le=50)] = 8
    max_output_tokens: Annotated[int, Field(ge=1, le=8192)] = 512
    max_run_cost: Annotated[Decimal, Field(gt=0)] = Decimal("1.00")
    enable_thinking: bool = False
    max_context_chars: Annotated[int, Field(ge=2_000, le=1_000_000)] = 60_000
    max_input_tokens: Annotated[int, Field(ge=2_000, le=2_000_000)] = 120_000
    preserve_recent_context_units: Annotated[int, Field(ge=1, le=50)] = 6
    max_run_memory_entries: Annotated[int, Field(ge=1, le=50)] = 12
    run_memory_excerpt_chars: Annotated[int, Field(ge=0, le=1_000)] = 240
    max_identical_no_progress_actions: Annotated[int, Field(ge=1, le=10)] = 2


def _execution_replan_tool(run: Run) -> ToolDefinition:
    acceptance_ids = [check.id for check in run.task.acceptance]
    permitted_tools = list(permitted_plan_tools(run.task))
    max_items = MAX_EXECUTION_REPLAN_ITEMS
    return ToolDefinition(
        name=EXECUTION_REPLAN_TOOL_NAME,
        description=(
            "Revise the remaining Plan once when accumulated execution evidence proves the "
            "current structure is inadequate. Preserve completed WorkItems verbatim and do not "
            "expand task authority, acceptance checks, tools, or budgets."
        ),
        parameters={
            "type": "object",
            "properties": {
                "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": max_items,
                    "items": {
                        "type": "object",
                        "properties": {
                            "work_item_id": {"type": "string"},
                            "title": {"type": "string"},
                            "objective": {"type": "string"},
                            "dependencies": {
                                "type": "array",
                                "maxItems": max_items,
                                "items": {"type": "string"},
                            },
                            "expected_artifacts": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 16,
                                "items": {"type": "string"},
                            },
                            "acceptance_ids": {
                                "type": "array",
                                "minItems": 1,
                                "items": {"type": "string", "enum": acceptance_ids},
                            },
                            "allowed_tools": {
                                "type": "array",
                                "minItems": 1,
                                "items": {"type": "string", "enum": permitted_tools},
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
            "required": ["reason", "items"],
            "additionalProperties": False,
        },
    )


def _active_work_item(run: Run) -> WorkItem:
    if run.plan is None:
        raise Conflict("Agent loop requires an active plan")
    if run.agent_session is not None:
        item = next(
            (
                candidate
                for candidate in run.plan.items
                if candidate.work_item_id == run.agent_session.work_item_id
            ),
            None,
        )
        if (
            item is None
            or item.work_item_id in run.passed_items
            or not set(item.dependencies) <= run.passed_items
        ):
            raise Conflict("Persisted Agent session does not identify a ready work item")
        return item
    ready = run.plan.ready_items(run.passed_items)
    if not ready:
        raise Conflict("Run has no dependency-ready incomplete work item")
    return ready[0]


def _execution_replan_available(run: Run) -> bool:
    """Expose the large replan schema only after revision-bound execution evidence exists.

    A fresh work-item session contains the two canonical task-prefix messages. One ordinary
    tool turn raises that count to four, which is still too little evidence to justify replacing
    the Plan. Five messages means either one response produced multiple tool receipts or at
    least two execution turns have completed. A previously validated WorkItem is independently
    sufficient evidence for revising the remaining Plan.

    AgentSession metadata is used instead of the live tool-call count so the decision remains
    stable while recovering a model response or a read-only tool receipt after a crash.
    """

    if run.plan is None or len(run.execution_replans) >= MAX_EXECUTION_REPLANS:
        return False
    if run.passed_items:
        return True
    return (
        run.agent_session is not None
        and run.agent_session.message_count >= MIN_EXECUTION_REPLAN_SESSION_MESSAGES
    )


def _initial_messages(
    run: Run,
    item: WorkItem,
    *,
    completed_work_item_ids: tuple[str, ...] | None = None,
) -> list[ModelMessage]:
    assert run.plan is not None
    completed = completed_work_item_ids or tuple(
        candidate.work_item_id
        for candidate in run.plan.items
        if candidate.work_item_id in run.passed_items
    )
    contract = {
        "objective": run.task.objective,
        "work_item": item.model_dump(mode="json"),
        "completed_work_items": completed,
        "allowed_paths": run.task.constraints.allowed_paths,
        "denied_paths": run.task.constraints.denied_paths,
        "acceptance": [
            {"id": check.id, "required": check.required} for check in run.task.acceptance
        ],
    }
    return [
        ModelMessage(
            role="system",
            content=(
                "You are the decision component of a bounded coding agent. Use only the supplied "
                "tools. Inspect evidence before editing. Never invent paths or commands. The "
                "retrieve_code tool is preferred for locating symbols or concepts in a large "
                "repository; search_repo is a bounded exact-substring scan and may return only "
                "early call sites. After retrieve_code, use its path and line range with "
                "read_file: copy both start_line and end_line, and never send only one bound; "
                "unbounded reads of large files are rejected. Do not repeat a "
                "saturated search with equivalent queries. Plan objectives and expected "
                "artifacts are work guidance, not repository evidence: when current, "
                "revision-bound retrieval contradicts a guessed path, follow the evidence "
                "instead of searching to satisfy the guess. The "
                "controller owns permissions and final validation. Call submit only after the "
                "requested change is ready for protected validation. Use revise_plan only when "
                "run evidence proves the remaining Plan structure is inadequate; completed work "
                "and the immutable task contract must be preserved."
            ),
        ),
        ModelMessage(
            role="user",
            content=f"Complete this immutable task contract:\n{canonical_json(contract)}",
        ),
    ]


class CodingAgentRunner:
    """A single-work-item bounded loop with durable model/tool intents and receipts."""

    def __init__(
        self,
        service: HarnessService,
        model: ModelGatewayPort,
        campaign_ledger: CampaignBudgetPort,
        tools: ToolGatewayPort,
        session_store: ArtifactStorePort,
        *,
        provider_id: str,
        model_id: str,
        pricing: PriceCard,
        campaign: CampaignBudget,
        config: AgentLoopConfig | None = None,
    ):
        self.service = service
        self.model = model
        self.campaign_ledger = campaign_ledger
        self.tools = tools
        self.session_store = session_store
        self.provider_id = provider_id
        self.model_id = model_id
        self.pricing = pricing
        self.campaign = campaign
        self.config = config or AgentLoopConfig()

    def _model_tools(self, run: Run) -> tuple[ToolDefinition, ...]:
        definitions = self.tools.definitions
        if _execution_replan_available(run):
            return (*definitions, _execution_replan_tool(run))
        return definitions

    def _save_session(
        self,
        run: Run,
        token: LeaseToken,
        messages: list[ModelMessage],
        next_iteration: int,
    ) -> Run:
        run = self.service.store.get(run.run_id)
        item = _active_work_item(run)
        record = self._session_record(
            run,
            item,
            messages,
            next_iteration,
            covered_event_seq=run.seq,
        )
        return self.service.save_agent_session(
            run.run_id,
            record,
            token,
            f"session_{uuid4().hex}",
        )

    def _session_record(
        self,
        run: Run,
        item: WorkItem,
        messages: list[ModelMessage],
        next_iteration: int,
        *,
        covered_event_seq: int,
    ) -> AgentSessionRecord:
        if run.plan is None:
            raise Conflict("Agent session requires an active plan")
        session = AgentSession(
            run_id=run.run_id,
            task_spec_hash=run.task.sha256,
            plan_version=run.plan.version,
            work_item_id=item.work_item_id,
            next_iteration=next_iteration,
            covered_event_seq=covered_event_seq,
            workspace_revision=self.tools.current_revision(),
            messages=tuple(messages),
        )
        payload = canonical_json(session.model_dump(mode="json")).encode("utf-8")
        artifact_ref = self.session_store.put(payload)
        if self.session_store.read(artifact_ref) != payload:
            raise Conflict("Agent session artifact verification failed")
        return AgentSessionRecord(
            artifact_ref=artifact_ref,
            task_spec_hash=session.task_spec_hash,
            plan_version=session.plan_version,
            work_item_id=session.work_item_id,
            next_iteration=session.next_iteration,
            covered_event_seq=session.covered_event_seq,
            workspace_revision=session.workspace_revision,
            message_count=len(session.messages),
        )

    def _mandatory_facts(
        self,
        run: Run,
        workspace_revision: str | None = None,
    ) -> MandatoryFactLedger:
        item = _active_work_item(run)
        return build_mandatory_fact_ledger(
            run,
            item.work_item_id,
            workspace_revision or self.tools.current_revision(),
            self._model_tools(run),
        )

    def _store_mandatory_facts(self, facts: MandatoryFactLedger) -> str:
        payload = canonical_json(facts.model_dump(mode="json")).encode("utf-8")
        artifact_ref = self.session_store.put(payload)
        if artifact_ref != facts.sha256 or self.session_store.read(artifact_ref) != payload:
            raise Conflict("Mandatory fact ledger artifact verification failed")
        return artifact_ref

    def _run_memory(
        self,
        run: Run,
        *,
        workspace_revision: str,
    ) -> RunMemorySnapshot:
        return RunMemoryProjector(
            self.session_store,
            max_entries=self.config.max_run_memory_entries,
            excerpt_chars=self.config.run_memory_excerpt_chars,
        ).project(
            run,
            self.service.store.events(run.run_id, at=run.seq),
            current_revision=workspace_revision,
            active_work_item_id=_active_work_item(run).work_item_id,
        )

    def _store_run_memory(self, memory: RunMemorySnapshot) -> str:
        payload = canonical_json(memory.model_dump(mode="json")).encode("utf-8")
        artifact_ref = self.session_store.put(payload)
        if artifact_ref != memory.sha256 or self.session_store.read(artifact_ref) != payload:
            raise Conflict("Run memory artifact verification failed")
        return artifact_ref

    def _project_context(
        self,
        run: Run,
        messages: list[ModelMessage],
        facts: MandatoryFactLedger,
        facts_ref: str,
        memory: RunMemorySnapshot,
        memory_ref: str,
        *,
        estimator: str | None = None,
        max_input_tokens: int | None = None,
    ) -> ContextProjection:
        if tuple(messages[:2]) != tuple(_initial_messages(run, _active_work_item(run))):
            raise Conflict("Canonical Agent task prefix does not match the current Run")
        estimate_input = (
            legacy_input_estimate
            if estimator == CONSERVATIVE_INPUT_TOKEN_ESTIMATOR
            else conservative_input_estimate
        )
        return ContextProjector(
            max_chars=self.config.max_context_chars,
            preserve_recent_units=self.config.preserve_recent_context_units,
            max_input_tokens=(
                self.config.max_input_tokens if max_input_tokens is None else max_input_tokens
            ),
            estimate_input_tokens=lambda projected_messages: estimate_input(
                self._request_for_messages(projected_messages, run)
            ),
        ).project(
            tuple(messages),
            mandatory_facts=facts,
            mandatory_facts_ref=facts_ref,
            run_memory=memory,
            run_memory_ref=memory_ref,
        )

    def _affordable_input_token_limit(self, run: Run) -> int:
        """Return a representable input ceiling that fits every monetary scope.

        A limit below the domain minimum cannot be persisted as an InputTokenBudget. In that
        case the normal configured ceiling is retained so the existing reservation gates make
        the final cost decision instead of turning a possible monetary stop into a projection
        error.
        """
        if run.model_policy is None or self.pricing.input_per_million == 0:
            return self.config.max_input_tokens
        campaign = self.campaign_ledger.summary(self.campaign.campaign_id)
        run_remaining = max(
            Decimal("0"),
            run.model_policy.max_run_cost - run.model_occupied_cost,
        )
        available = min(
            run_remaining,
            campaign.remaining_cost,
            self.campaign.max_cost_per_call,
        )
        output_cost = self.pricing.reserve_cost(0, self.config.max_output_tokens)
        if available <= output_cost:
            return self.config.max_input_tokens
        affordable = int(
            (available - output_cost) * Decimal(1_000_000) / self.pricing.input_per_million
        )
        if affordable < 2_000:
            return self.config.max_input_tokens
        return min(self.config.max_input_tokens, affordable)

    def _request_for_messages(
        self,
        messages: tuple[ModelMessage, ...],
        run: Run,
    ) -> ModelRequest:
        return ModelRequest(
            model=self.model_id,
            messages=messages,
            tools=self._model_tools(run),
            tool_choice="auto",
            max_output_tokens=self.config.max_output_tokens,
            temperature=Decimal("0"),
            enable_thinking=self.config.enable_thinking,
        )

    def _request_from_projection(self, projection: ContextProjection, run: Run) -> ModelRequest:
        return self._request_for_messages(projection.messages, run)

    def _store_context_projection(self, projection: ContextProjection) -> str:
        payload = canonical_json(projection.model_dump(mode="json")).encode("utf-8")
        artifact_ref = self.session_store.put(payload)
        if self.session_store.read(artifact_ref) != payload:
            raise Conflict("Context projection artifact verification failed")
        return artifact_ref

    def _load_session(self, run: Run) -> tuple[list[ModelMessage], int, ModelResponse | None]:
        record = run.agent_session
        if record is None:
            return _initial_messages(run, _active_work_item(run)), 1, None
        session = AgentSession.model_validate_json(self.session_store.read(record.artifact_ref))
        if (
            session.run_id != run.run_id
            or session.task_spec_hash != run.task.sha256
            or run.plan is None
            or session.plan_version != run.plan.version
            or session.work_item_id != _active_work_item(run).work_item_id
            or session.next_iteration != record.next_iteration
            or session.covered_event_seq != record.covered_event_seq
            or session.workspace_revision != record.workspace_revision
            or len(session.messages) != record.message_count
        ):
            raise Conflict("Persisted Agent session does not match the current Run")
        events = self.service.store.events(run.run_id)
        if session.workspace_revision != self.tools.current_revision():
            raise Conflict("Workspace changed after the last persisted Agent session")
        if session.next_iteration > self.config.max_model_iterations + 1:
            raise Conflict("Persisted Agent session exceeds the configured iteration budget")
        tail = events_after_agent_session(run, events)
        if tail is None:
            raise Conflict("Run has no valid persisted Agent session boundary")
        if all(event.event_type in LEASE_EVENTS for event in tail):
            return list(session.messages), session.next_iteration, None
        pending = recoverable_model_turn(run, events)
        if pending is None:
            retry = recoverable_readonly_tool_turn(run, events, self.session_store)
            pending = retry.model if retry is not None else None
        if pending is None:
            raise Conflict("Run advanced beyond the last safe Agent session boundary")
        if run.model_policy is None:
            raise Conflict("Recoverable model response has no bound policy")
        attempt = self.campaign_ledger.attempt(
            run.model_policy.campaign_id,
            pending.record.call_id,
        )
        if (
            attempt.status != "settled"
            or attempt.request_hash != pending.reservation.request_hash
            or attempt.reserved_cost != pending.reservation.reserved_cost
            or attempt.actual_cost != pending.record.estimated_cost
            or attempt.provider_trace_id != pending.record.provider_trace_id
        ):
            raise Conflict("Recoverable model response requires reconciled campaign settlement")
        messages = list(session.messages)
        reservation = pending.reservation
        facts = self._mandatory_facts(run)
        if (
            reservation.mandatory_facts_ref is None
            or reservation.mandatory_facts_hash != facts.sha256
            or reservation.mandatory_facts_ref != facts.sha256
        ):
            raise Conflict("Recovered model request has a different mandatory fact ledger")
        recorded_facts = MandatoryFactLedger.model_validate_json(
            self.session_store.read(reservation.mandatory_facts_ref)
        )
        if recorded_facts != facts:
            raise Conflict("Recovered mandatory fact ledger does not match the current Run")
        if (
            reservation.run_memory_ref is None
            or reservation.run_memory_hash is None
            or reservation.run_memory_covered_event_seq < 1
        ):
            raise Conflict("Recovered model request has no bound Run memory")
        recorded_memory = RunMemorySnapshot.model_validate_json(
            self.session_store.read(reservation.run_memory_ref)
        )
        if (
            recorded_memory.sha256 != reservation.run_memory_hash
            or recorded_memory.sha256 != reservation.run_memory_ref
            or recorded_memory.covered_event_seq != reservation.run_memory_covered_event_seq
            or recorded_memory.included_entry_count != reservation.run_memory_entry_count
            or recorded_memory.workspace_revision != session.workspace_revision
        ):
            raise Conflict("Recovered Run memory metadata does not match its reservation")
        historical_run = self.service.store.get(
            run.run_id,
            at=reservation.run_memory_covered_event_seq,
        )
        rebuilt_memory = self._run_memory(
            historical_run,
            workspace_revision=recorded_memory.workspace_revision,
        )
        if rebuilt_memory != recorded_memory:
            raise Conflict("Recovered Run memory does not match authoritative event evidence")
        projection = self._project_context(
            run,
            messages,
            facts,
            reservation.mandatory_facts_ref,
            recorded_memory,
            reservation.run_memory_ref,
            estimator=(
                reservation.input_token_budget.estimate.estimator
                if reservation.input_token_budget is not None
                else None
            ),
            max_input_tokens=(
                reservation.input_token_budget.max_input_tokens
                if reservation.input_token_budget is not None
                else None
            ),
        )
        recovered_request = self._request_from_projection(projection, run)
        if recovered_request.sha256 != pending.record.request_hash:
            raise Conflict("Recovered model request does not match the persisted Agent session")
        if (
            reservation.request_payload is not None
            and recovered_request.openai_compatible_payload_evidence()
            != reservation.request_payload
        ):
            raise Conflict("Recovered model request payload does not match its reservation")
        if reservation.context_projection_ref is not None:
            recorded_projection = ContextProjection.model_validate_json(
                self.session_store.read(reservation.context_projection_ref)
            )
            if (
                recorded_projection != projection
                or recorded_projection.mandatory_facts_ref != reservation.mandatory_facts_ref
                or recorded_projection.mandatory_facts_hash != reservation.mandatory_facts_hash
                or recorded_projection.run_memory_ref != reservation.run_memory_ref
                or recorded_projection.run_memory_hash != reservation.run_memory_hash
                or recorded_projection.run_memory_entry_count != reservation.run_memory_entry_count
                or reservation.source_message_count != projection.source_message_count
                or reservation.projected_message_count != projection.projected_message_count
                or reservation.input_token_budget != projection.input_token_budget
            ):
                raise Conflict(
                    "Recovered context projection does not match the persisted Agent session"
                )
        response = load_recorded_model_response(pending, self.session_store)
        return messages, session.next_iteration, response

    def _model_call(
        self,
        run_id: str,
        token: LeaseToken,
        messages: list[ModelMessage],
        iteration: int,
    ):
        run = self.service.store.get(run_id)
        workspace_revision = self.tools.current_revision()
        facts = self._mandatory_facts(run, workspace_revision)
        mandatory_facts_ref = self._store_mandatory_facts(facts)
        memory = self._run_memory(run, workspace_revision=workspace_revision)
        run_memory_ref = self._store_run_memory(memory)
        effective_input_limit = self._affordable_input_token_limit(run)
        try:
            projection = self._project_context(
                run,
                messages,
                facts,
                mandatory_facts_ref,
                memory,
                run_memory_ref,
                max_input_tokens=effective_input_limit,
            )
        except Conflict:
            if effective_input_limit == self.config.max_input_tokens:
                raise
            projection = self._project_context(
                run,
                messages,
                facts,
                mandatory_facts_ref,
                memory,
                run_memory_ref,
            )
        request = self._request_from_projection(projection, run)
        request_estimate, request_payload = conservative_input_sizing(request)
        if request_estimate != projection.input_token_budget.estimate:
            raise Conflict("Context projection input-token estimate does not match its request")
        input_ceiling = request_estimate.token_ceiling
        context_projection_ref = self._store_context_projection(projection)
        reserved_cost = self.pricing.reserve_cost(
            input_ceiling,
            request.max_output_tokens,
        )
        call_id = f"model_{run_id}_{iteration}_{uuid4().hex}"
        trace_id = f"horizon-{uuid4().hex}"
        reservation = ModelCallReservation(
            call_id=call_id,
            client_trace_id=trace_id,
            request_hash=request.sha256,
            provider_id=self.provider_id,
            model=self.model_id,
            currency=self.pricing.currency,
            reserved_cost=reserved_cost,
            context_projection_ref=context_projection_ref,
            mandatory_facts_ref=mandatory_facts_ref,
            mandatory_facts_hash=facts.sha256,
            run_memory_ref=run_memory_ref,
            run_memory_hash=memory.sha256,
            run_memory_covered_event_seq=memory.covered_event_seq,
            run_memory_entry_count=memory.included_entry_count,
            source_message_count=projection.source_message_count,
            projected_message_count=projection.projected_message_count,
            input_token_budget=projection.input_token_budget,
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
                # No request was dispatched. Release only the Campaign reservation with a zero
                # receipt before preserving the original failure.
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
            raise BudgetExceeded(
                str(exc),
                stop=exc.stop,
                model_request_budget=request_budget,
            ) from exc
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
            response_artifact_ref = persist_model_response(response, self.session_store)
            record = ModelCallRecord(
                call_id=call_id,
                request_hash=request.sha256,
                provider_id=self.provider_id,
                model=response.model,
                currency=self.pricing.currency,
                estimated_cost=estimated_cost,
                response_id=response.response_id,
                response_artifact_ref=response_artifact_ref,
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
        return run, response

    def _tool_call(
        self,
        run_id: str,
        token: LeaseToken,
        name: str,
        arguments: dict,
        *,
        protected_check: bool = False,
        enforce_no_progress: bool = False,
    ) -> tuple[ToolOutcome, NoProgressPattern | None]:
        local_id = f"tool_{uuid4().hex}"
        arguments_hash = digest(arguments)
        workspace_revision, workspace_manifest_ref = self.tools.checkpoint()
        decision = None
        if enforce_no_progress:
            current = self.service.store.get(run_id)
            if current.no_progress_reset_tool_count > len(current.tool_calls):
                raise Conflict("No-progress reset boundary exceeds the tool history")
            decision = classify_no_progress(
                current.tool_calls,
                current.no_progress_reset_tool_count,
                name=name,
                arguments_hash=arguments_hash,
                workspace_revision=workspace_revision,
                max_identical_actions=self.config.max_identical_no_progress_actions,
            )
        no_progress_pattern = decision.pattern if decision is not None else None
        hard_no_progress_pattern = (
            no_progress_pattern if decision is not None and decision.hard_stop else None
        )
        reservation = ToolCallReservation(
            call_id=local_id,
            name=name,
            arguments_hash=arguments_hash,
            workspace_revision=workspace_revision,
            workspace_manifest_ref=workspace_manifest_ref,
        )
        self.service.reserve_tool_call(
            run_id,
            reservation,
            token,
            f"reserve_{local_id}",
        )
        if no_progress_pattern is not None:
            if no_progress_pattern == "identical_action":
                content = (
                    "NoProgressPolicy: identical tool name and arguments were already settled "
                    f"{decision.identical_prior_count} consecutive times at workspace revision "
                    f"{workspace_revision}. Choose different evidence or a different action."
                )
            else:
                first_name = decision.alternating_window[0][0]
                second_name = decision.alternating_window[1][0]
                content = (
                    "NoProgressPolicy: an exact alternating two-action cycle reached "
                    f"{len(decision.alternating_window)} consecutive unchanged-revision receipts "
                    f"({first_name} <-> {second_name}) at workspace revision "
                    f"{workspace_revision}. Choose evidence or an action outside this cycle."
                )
            payload = content.encode("utf-8")
            artifact_ref = self.session_store.put(payload)
            if self.session_store.read(artifact_ref) != payload:
                raise Conflict("No-progress policy artifact verification failed")
            outcome = ToolOutcome(
                status="error",
                content=content,
                output_hash=artifact_ref,
                workspace_revision_before=workspace_revision,
                workspace_revision_after=workspace_revision,
                artifact_ref=artifact_ref,
                workspace_manifest_ref=workspace_manifest_ref,
            )
        elif protected_check:
            if name != "run_check" or set(arguments) != {"check_id"}:
                raise Conflict("Protected dispatch only accepts one registered check ID")
            outcome = self.tools.dispatch_protected_check(
                arguments["check_id"],
                attempt_id=local_id,
            )
        else:
            outcome = self.tools.dispatch_safe(
                name,
                arguments,
                # Every adapter receives the durable local call ID for crash diagnostics. The
                # production gateway creates an external durable attempt only for run_check.
                attempt_id=local_id,
            )
        self.service.settle_tool_call(
            run_id,
            ToolCallRecord(
                call_id=local_id,
                name=name,
                arguments_hash=arguments_hash,
                status=outcome.status,
                output_hash=outcome.output_hash,
                workspace_revision_before=outcome.workspace_revision_before,
                workspace_revision_after=outcome.workspace_revision_after,
                artifact_ref=outcome.artifact_ref,
                workspace_manifest_ref=outcome.workspace_manifest_ref,
            ),
            token,
            f"settle_{local_id}",
        )
        if name == "run_check" and no_progress_pattern is None:
            # A durable Docker attempt is intentionally retained until the authoritative tool
            # receipt exists. Cleanup is best-effort after settlement; a failure leaves the
            # labeled stopped attempt available for deterministic operator cleanup.
            self.tools.cleanup_check_attempt(local_id)
        return outcome, hard_no_progress_pattern

    def _record_controller_error(
        self,
        run_id: str,
        token: LeaseToken,
        name: str,
        arguments: dict,
        detail: str,
    ) -> ToolOutcome:
        call_id = f"tool_{uuid4().hex}"
        arguments_hash = digest(arguments)
        revision, manifest_ref = self.tools.checkpoint()
        reservation = ToolCallReservation(
            call_id=call_id,
            name=name,
            arguments_hash=arguments_hash,
            workspace_revision=revision,
            workspace_manifest_ref=manifest_ref,
        )
        self.service.reserve_tool_call(
            run_id,
            reservation,
            token,
            f"reserve_{call_id}",
        )
        content = f"ExecutionReplanPolicy: {detail}"
        payload = content.encode("utf-8")
        artifact_ref = self.session_store.put(payload)
        if self.session_store.read(artifact_ref) != payload:
            raise Conflict("Controller tool error artifact verification failed")
        outcome = ToolOutcome(
            status="error",
            content=content,
            output_hash=artifact_ref,
            workspace_revision_before=revision,
            workspace_revision_after=revision,
            artifact_ref=artifact_ref,
            workspace_manifest_ref=manifest_ref,
        )
        self.service.settle_tool_call(
            run_id,
            ToolCallRecord(
                call_id=call_id,
                name=name,
                arguments_hash=arguments_hash,
                status=outcome.status,
                output_hash=outcome.output_hash,
                workspace_revision_before=revision,
                workspace_revision_after=revision,
                artifact_ref=artifact_ref,
                workspace_manifest_ref=manifest_ref,
            ),
            token,
            f"settle_{call_id}",
        )
        return outcome

    def _apply_execution_replan(
        self,
        run_id: str,
        token: LeaseToken,
        source_model_call_id: str,
        arguments: dict,
        next_iteration: int,
    ) -> tuple[Run | None, ToolOutcome]:
        run = self.service.store.get(run_id)
        if run.plan is None:
            raise Conflict("Execution replan requires an active Plan")
        try:
            proposal = ExecutionReplanProposal.model_validate(arguments)
            plan = proposal.plan(run.plan.version + 1)
            check_execution_replan(run.plan, plan, run.passed_items, run.task)
        except (PolicyDenied, ValidationError, ValueError) as exc:
            return None, self._record_controller_error(
                run_id,
                token,
                EXECUTION_REPLAN_TOOL_NAME,
                arguments,
                str(exc),
            )
        if run.agent_session is None:
            raise Conflict("Execution replan has no persisted Agent session")
        revision, manifest_ref = self.tools.checkpoint()
        if revision != run.agent_session.workspace_revision:
            raise Conflict("Workspace changed after the active Agent session")
        call_id = f"tool_{uuid4().hex}"
        content = (
            f"ExecutionReplanAccepted: Plan v{run.plan.version} -> v{plan.version}; "
            f"reason={proposal.reason}"
        )
        payload = content.encode("utf-8")
        artifact_ref = self.session_store.put(payload)
        if self.session_store.read(artifact_ref) != payload:
            raise Conflict("Execution replan artifact verification failed")
        outcome = ToolOutcome(
            status="success",
            content=content,
            output_hash=artifact_ref,
            workspace_revision_before=revision,
            workspace_revision_after=revision,
            artifact_ref=artifact_ref,
            workspace_manifest_ref=manifest_ref,
        )
        reservation = ToolCallReservation(
            call_id=call_id,
            name=EXECUTION_REPLAN_TOOL_NAME,
            arguments_hash=proposal.sha256,
            workspace_revision=revision,
            workspace_manifest_ref=manifest_ref,
        )
        record = ToolCallRecord(
            call_id=call_id,
            name=EXECUTION_REPLAN_TOOL_NAME,
            arguments_hash=proposal.sha256,
            status="success",
            output_hash=artifact_ref,
            workspace_revision_before=revision,
            workspace_revision_after=revision,
            artifact_ref=artifact_ref,
            workspace_manifest_ref=manifest_ref,
        )
        planned_run = replace(
            run,
            plan=plan,
            plan_source_model_call_id=source_model_call_id,
            agent_session=None,
            validation=None,
        )
        next_item = plan.ready_items(run.passed_items)[0]
        next_messages = _initial_messages(planned_run, next_item)
        next_session = self._session_record(
            planned_run,
            next_item,
            next_messages,
            next_iteration,
            covered_event_seq=run.seq + 7,
        )
        replanned = self.service.apply_execution_replan(
            run_id,
            proposal,
            source_model_call_id,
            reservation,
            record,
            next_session,
            token,
            f"replan_{uuid4().hex}",
        )
        self.tools.activate_work_item(next_item)
        return replanned, outcome

    def _protected_validation(
        self,
        run_id: str,
        token: LeaseToken,
        item: WorkItem,
        *,
        final_item: bool,
    ) -> tuple[Run, tuple[AcceptanceResult, ...]]:
        run = self.service.store.get(run_id)
        results: list[AcceptanceResult] = []
        selected_ids = set(item.acceptance_ids)
        if final_item:
            selected_ids.update(check.id for check in run.task.acceptance if check.required)
        for check in run.task.acceptance:
            if check.id not in selected_ids:
                continue
            outcome, _ = self._tool_call(
                run_id,
                token,
                "run_check",
                {"check_id": check.id},
                protected_check=True,
            )
            result = getattr(self.tools, "last_check_results", {}).get(check.id)
            if result is None:
                raise Conflict("Protected validation did not return a structured result")
            results.append(result)
            if outcome.status == "unknown":
                raise Conflict("Protected validation effect is unknown")
        revision, manifest = self.tools.checkpoint()
        run = self.service.store.get(run_id)
        run = commit_checkpoint(
            self.service,
            run_id,
            token,
            f"checkpoint_{uuid4().hex}",
            manifest,
            revision,
            run.seq,
            self.tools.verify_manifest,
        )
        run = self.service.transition(
            run_id,
            RunStatus.VALIDATING,
            token,
            f"validating_{uuid4().hex}",
        )
        passed_ids = tuple(result.check_id for result in results if result.passed)
        evidence_ref = self.tools.validation_evidence(tuple(results))
        run = self.service.record_validation(
            run_id,
            passed_ids,
            evidence_ref,
            token,
            f"validation_{uuid4().hex}",
        )
        return run, tuple(results)

    def _repair_or_fail(
        self,
        run: Run,
        token: LeaseToken,
        messages: list[ModelMessage],
        results: tuple[AcceptanceResult, ...],
    ) -> Run | None:
        if run.usage.repair_cycles >= run.task.budgets.max_repair_cycles:
            return self.service.fail(
                run.run_id,
                "repair_budget_exhausted",
                token,
                f"fail_{uuid4().hex}",
            )
        run = self.service.transition(
            run.run_id,
            RunStatus.REPAIRING,
            token,
            f"repairing_{uuid4().hex}",
        )
        repair_id = f"repair_{uuid4().hex}"
        self.service.reserve(
            run.run_id,
            repair_id,
            Usage(repair_cycles=1),
            token,
            f"reserve_{repair_id}",
        )
        self.service.settle(
            run.run_id,
            repair_id,
            Usage(repair_cycles=1),
            token,
            f"settle_{repair_id}",
        )
        run = self.service.transition(
            run.run_id,
            RunStatus.RUNNING,
            token,
            f"resume_{uuid4().hex}",
        )
        feedback = [
            {
                "check_id": result.check_id,
                "passed": result.passed,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "output": result.output[:8000],
            }
            for result in results
        ]
        messages.append(
            ModelMessage(
                role="user",
                content=(
                    "Protected validation failed. Inspect this evidence, repair within the same "
                    "contract, rerun relevant checks, then submit again:\n"
                    f"{canonical_json(feedback)}"
                ),
            )
        )
        return None

    def _yield_at_safe_boundary(self, run_id: str) -> Run:
        return self.service.expire_if_safe(
            run_id,
            f"agent_yield_deadline_{uuid4().hex}",
        )

    def run(
        self,
        run_id: str,
        token: LeaseToken,
        *,
        max_iterations_this_invocation: int | None = None,
    ) -> Run:
        if max_iterations_this_invocation is not None and max_iterations_this_invocation < 1:
            raise ValueError("Invocation iteration limit must be positive")
        run = self.service.store.get(run_id)
        if run.status != RunStatus.RUNNING or run.plan is None:
            raise Conflict("Agent loop requires a leased RUNNING run with a validated plan")
        active_item = _active_work_item(run)
        self.tools.activate_work_item(active_item)
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
        run = self.service.bind_model_policy(run_id, policy, token, f"bind_{uuid4().hex}")
        if run.agent_session is None:
            active_item = _active_work_item(run)
            self.tools.activate_work_item(active_item)
            run = self._save_session(run, token, _initial_messages(run, active_item), 1)
        else:
            self.tools.activate_work_item(_active_work_item(run))
        messages, start_iteration, pending_response = self._load_session(run)
        iterations_this_invocation = 0

        for iteration in range(start_iteration, self.config.max_model_iterations + 1):
            if pending_response is not None:
                run = self.service.store.get(run_id)
                response = pending_response
                pending_response = None
            else:
                try:
                    run, response = self._model_call(run_id, token, messages, iteration)
                except BudgetExceeded as exc:
                    if exc.stop is None or self.service.store.get(run_id).reservations:
                        raise
                    return self.service.fail_budget_stop(
                        run_id,
                        exc.stop,
                        token,
                        f"budget_stop_{uuid4().hex}",
                        model_request_budget=exc.model_request_budget,
                    )
                except ProviderError as exc:
                    return self.service.fail(
                        run_id,
                        f"provider_{type(exc).__name__}",
                        token,
                        f"fail_{uuid4().hex}",
                    )
            if run.terminal:
                return run
            messages.append(response.message)
            if not response.message.tool_calls:
                messages.append(
                    ModelMessage(
                        role="user",
                        content=(
                            "A bounded tool call is required; natural-language completion is not "
                            "accepted."
                        ),
                    )
                )
                run = self._save_session(run, token, messages, iteration + 1)
                iterations_this_invocation += 1
                if (
                    max_iterations_this_invocation is not None
                    and iterations_this_invocation >= max_iterations_this_invocation
                    and iteration < self.config.max_model_iterations
                ):
                    return self._yield_at_safe_boundary(run_id)
                continue
            replan_calls = [
                call
                for call in response.message.tool_calls
                if call.function.name == EXECUTION_REPLAN_TOOL_NAME
            ]
            replan_available = any(
                tool.name == EXECUTION_REPLAN_TOOL_NAME for tool in self._model_tools(run)
            )
            if replan_calls and replan_available:
                if len(response.message.tool_calls) != 1:
                    for call in response.message.tool_calls:
                        outcome = self._record_controller_error(
                            run_id,
                            token,
                            call.function.name,
                            call.function.arguments,
                            "revise_plan must be the only tool call in its model response",
                        )
                        messages.append(
                            ModelMessage(
                                role="tool",
                                content=outcome.content,
                                tool_call_id=call.id,
                            )
                        )
                    run = self._save_session(
                        self.service.store.get(run_id),
                        token,
                        messages,
                        iteration + 1,
                    )
                    iterations_this_invocation += 1
                    if (
                        max_iterations_this_invocation is not None
                        and iterations_this_invocation >= max_iterations_this_invocation
                        and iteration < self.config.max_model_iterations
                    ):
                        return self._yield_at_safe_boundary(run_id)
                    continue
                source_model_call_id = run.model_calls[-1].call_id
                replanned, outcome = self._apply_execution_replan(
                    run_id,
                    token,
                    source_model_call_id,
                    replan_calls[0].function.arguments,
                    iteration + 1,
                )
                if replanned is None:
                    messages.append(
                        ModelMessage(
                            role="tool",
                            content=outcome.content,
                            tool_call_id=replan_calls[0].id,
                        )
                    )
                    run = self._save_session(
                        self.service.store.get(run_id),
                        token,
                        messages,
                        iteration + 1,
                    )
                    iterations_this_invocation += 1
                    if (
                        max_iterations_this_invocation is not None
                        and iterations_this_invocation >= max_iterations_this_invocation
                        and iteration < self.config.max_model_iterations
                    ):
                        return self._yield_at_safe_boundary(run_id)
                    continue
                iterations_this_invocation += 1
                if iteration >= self.config.max_model_iterations:
                    return self.service.fail(
                        run_id,
                        "model_iteration_limit",
                        token,
                        f"fail_{uuid4().hex}",
                    )
                if (
                    max_iterations_this_invocation is not None
                    and iterations_this_invocation >= max_iterations_this_invocation
                ):
                    return self._yield_at_safe_boundary(run_id)
                remaining_iterations = (
                    None
                    if max_iterations_this_invocation is None
                    else max_iterations_this_invocation - iterations_this_invocation
                )
                return self.run(
                    run_id,
                    token,
                    max_iterations_this_invocation=remaining_iterations,
                )
            submitted = False
            for call in response.message.tool_calls:
                outcome, hard_no_progress_pattern = self._tool_call(
                    run_id,
                    token,
                    call.function.name,
                    call.function.arguments,
                    enforce_no_progress=len(response.message.tool_calls) == 1,
                )
                messages.append(
                    ModelMessage(
                        role="tool",
                        content=outcome.content,
                        tool_call_id=call.id,
                    )
                )
                submitted = submitted or (
                    call.function.name == "submit" and outcome.status == "success"
                )
                if hard_no_progress_pattern is not None:
                    run = self._save_session(
                        self.service.store.get(run_id),
                        token,
                        messages,
                        iteration + 1,
                    )
                    source = run.tool_calls[-1]
                    return self.service.request_operator_guidance(
                        run_id,
                        source.call_id,
                        outcome.content,
                        hard_no_progress_pattern,
                        token,
                        f"guidance_{uuid4().hex}",
                    )
            if not submitted:
                run = self._save_session(run, token, messages, iteration + 1)
                iterations_this_invocation += 1
                if (
                    max_iterations_this_invocation is not None
                    and iterations_this_invocation >= max_iterations_this_invocation
                    and iteration < self.config.max_model_iterations
                ):
                    return self._yield_at_safe_boundary(run_id)
                continue

            run = self.service.store.get(run_id)
            active_item = _active_work_item(run)
            is_final_item = len(run.passed_items) + 1 == len(run.plan.items)
            run, results = self._protected_validation(
                run_id,
                token,
                active_item,
                final_item=is_final_item,
            )
            if all(result.passed for result in results):
                if is_final_item:
                    return self.service.pass_work_item_and_succeed(
                        run_id,
                        active_item.work_item_id,
                        token,
                        f"complete_{uuid4().hex}",
                    )
                prospective_passed = set(run.passed_items) | {active_item.work_item_id}
                next_items = run.plan.ready_items(prospective_passed)
                if not next_items:
                    raise Conflict("Validated work item did not unlock a successor")
                next_item = next_items[0]
                completed = tuple(
                    item.work_item_id
                    for item in run.plan.items
                    if item.work_item_id in prospective_passed
                )
                next_messages = _initial_messages(
                    run,
                    next_item,
                    completed_work_item_ids=completed,
                )
                next_session = self._session_record(
                    run,
                    next_item,
                    next_messages,
                    iteration + 1,
                    covered_event_seq=run.seq + 2,
                )
                run = self.service.advance_work_item_and_save_session(
                    run_id,
                    active_item.work_item_id,
                    next_session,
                    token,
                    f"advance_{uuid4().hex}",
                )
                self.tools.activate_work_item(next_item)
                messages = next_messages
                iterations_this_invocation += 1
                if (
                    max_iterations_this_invocation is not None
                    and iterations_this_invocation >= max_iterations_this_invocation
                    and iteration < self.config.max_model_iterations
                ):
                    return self._yield_at_safe_boundary(run_id)
                continue
            terminal = self._repair_or_fail(run, token, messages, results)
            if terminal is not None:
                return terminal
            run = self._save_session(
                self.service.store.get(run_id),
                token,
                messages,
                iteration + 1,
            )
            iterations_this_invocation += 1
            if (
                max_iterations_this_invocation is not None
                and iterations_this_invocation >= max_iterations_this_invocation
                and iteration < self.config.max_model_iterations
            ):
                return self._yield_at_safe_boundary(run_id)

        return self.service.fail(
            run_id,
            "model_iteration_limit",
            token,
            f"fail_{uuid4().hex}",
        )
