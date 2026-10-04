from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from horizon.domain.agent import AgentSessionRecord
from horizon.domain.budget import Usage
from horizon.domain.common import digest
from horizon.domain.errors import BudgetStop, IntegrityError, InvalidTransition, PolicyDenied
from horizon.domain.events import Event
from horizon.domain.human import (
    HumanDecision,
    HumanGuidanceDecision,
    HumanGuidanceRequest,
    HumanPlanDecision,
    HumanPlanRequest,
    HumanRequest,
    matches_no_progress_evidence,
    parse_human_decision,
    parse_human_request,
)
from horizon.domain.model import (
    ModelCallRecord,
    ModelCallReservation,
    ModelPolicyBinding,
    ModelRequestBudgetEvidence,
)
from horizon.domain.plan import (
    MAX_EXECUTION_REPLANS,
    ExecutionReplanProposal,
    ExecutionReplanRecord,
    Plan,
    check_execution_replan,
)
from horizon.domain.promotion import PromotionIntent, PromotionReceipt, WorkspaceOrigin
from horizon.domain.states import TERMINAL, RunStatus, check_transition
from horizon.domain.task import TaskSpec
from horizon.domain.tools import ToolCallRecord, ToolCallReservation


@dataclass
class Run:
    run_id: str
    task: TaskSpec
    created_at: str
    deadline_at: str
    status: RunStatus = RunStatus.CREATED
    seq: int = 0
    event_hash: str = ""
    resume_state: RunStatus | None = None
    plan: Plan | None = None
    plan_source_model_call_id: str | None = None
    execution_replans: list[ExecutionReplanRecord] = field(default_factory=list)
    pending_human_request: HumanRequest | None = None
    pending_human_plan_hash: str | None = None
    pending_human_session_ref: str | None = None
    human_decisions: list[HumanDecision] = field(default_factory=list)
    no_progress_reset_tool_count: int = 0
    passed_items: set[str] = field(default_factory=set)
    validation: dict[str, Any] | None = None
    workspace_revision: str | None = None
    worker_id: str | None = None
    lease_id: str | None = None
    lease_epoch: int = 0
    lease_expires_at: str | None = None
    usage: Usage = field(default_factory=Usage)
    reservations: dict[str, Usage] = field(default_factory=dict)
    unknown_reservations: set[str] = field(default_factory=set)
    settled_ids: set[str] = field(default_factory=set)
    model_policy: ModelPolicyBinding | None = None
    model_reservations: dict[str, ModelCallReservation] = field(default_factory=dict)
    unknown_model_calls: set[str] = field(default_factory=set)
    model_calls: list[ModelCallRecord] = field(default_factory=list)
    tool_reservations: dict[str, ToolCallReservation] = field(default_factory=dict)
    unknown_tool_calls: set[str] = field(default_factory=set)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    agent_session: AgentSessionRecord | None = None
    last_checkpoint: dict[str, Any] | None = None
    workspace_origin: WorkspaceOrigin | None = None
    promotion_intent: PromotionIntent | None = None
    promotion_receipt: PromotionReceipt | None = None
    failure_reason: str | None = None
    budget_stop: BudgetStop | None = None
    model_request_budget: ModelRequestBudgetEvidence | None = None

    @property
    def occupied(self) -> Usage:
        total = self.usage
        for reservation in self.reservations.values():
            total = total.plus(reservation)
        return total

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def model_occupied_cost(self) -> Decimal:
        settled = sum((item.estimated_cost for item in self.model_calls), Decimal("0"))
        reserved = sum(
            (item.reserved_cost for item in self.model_reservations.values()),
            Decimal("0"),
        )
        return settled + reserved

    def as_dict(self) -> dict[str, Any]:
        result = {
            "run_id": self.run_id,
            "task": self.task.model_dump(mode="json"),
            "created_at": self.created_at,
            "deadline_at": self.deadline_at,
            "status": self.status.value,
            "last_event_seq": self.seq,
            "event_hash": self.event_hash,
            "resume_state": self.resume_state.value if self.resume_state else None,
            "plan": self.plan.model_dump(mode="json") if self.plan else None,
            "plan_source_model_call_id": self.plan_source_model_call_id,
            "execution_replans": [
                record.model_dump(mode="json") for record in self.execution_replans
            ],
            "pending_human_request": (
                self.pending_human_request.model_dump(mode="json")
                if self.pending_human_request
                else None
            ),
            "pending_human_plan_hash": self.pending_human_plan_hash,
            "pending_human_session_ref": self.pending_human_session_ref,
            "human_decisions": [
                decision.model_dump(mode="json") for decision in self.human_decisions
            ],
            "no_progress_reset_tool_count": self.no_progress_reset_tool_count,
            "passed_items": sorted(self.passed_items),
            "validation": self.validation,
            "workspace_revision": self.workspace_revision,
            "worker_id": self.worker_id,
            "lease_id": self.lease_id,
            "lease_epoch": self.lease_epoch,
            "lease_expires_at": self.lease_expires_at,
            "usage": self.usage.model_dump(mode="json"),
            "reserved": {
                key: value.model_dump(mode="json") for key, value in self.reservations.items()
            },
            "occupied": self.occupied.model_dump(mode="json"),
            "unknown_reservations": sorted(self.unknown_reservations),
            "settled_ids": sorted(self.settled_ids),
            "model_policy": (
                self.model_policy.model_dump(mode="json") if self.model_policy else None
            ),
            "model_reserved": {
                key: value.as_dict() for key, value in self.model_reservations.items()
            },
            "unknown_model_calls": sorted(self.unknown_model_calls),
            "model_calls": [item.model_dump(mode="json") for item in self.model_calls],
            "model_occupied_cost": str(self.model_occupied_cost),
            "tool_reserved": {
                key: value.model_dump(mode="json") for key, value in self.tool_reservations.items()
            },
            "unknown_tool_calls": sorted(self.unknown_tool_calls),
            "tool_calls": [item.model_dump(mode="json") for item in self.tool_calls],
            "agent_session": (
                self.agent_session.model_dump(mode="json") if self.agent_session else None
            ),
            "last_checkpoint": self.last_checkpoint,
            "workspace_origin": (
                self.workspace_origin.model_dump(mode="json") if self.workspace_origin else None
            ),
            "promotion_intent": (
                self.promotion_intent.model_dump(mode="json") if self.promotion_intent else None
            ),
            "promotion_receipt": (
                self.promotion_receipt.model_dump(mode="json") if self.promotion_receipt else None
            ),
            "failure_reason": self.failure_reason,
        }
        if self.budget_stop is not None:
            result["budget_stop"] = self.budget_stop.model_dump(mode="json")
        if self.model_request_budget is not None:
            result["model_request_budget"] = self.model_request_budget.as_dict()
        return result


def _transition(run: Run, target: RunStatus) -> None:
    previous = run.status
    check_transition(run.status, target, run.resume_state)
    if target == RunStatus.READY and run.plan is None:
        raise InvalidTransition("READY requires a validated plan")
    if target == RunStatus.SUCCEEDED:
        report = run.validation
        required = {check.id for check in run.task.acceptance if check.required}
        if (
            report is None
            or report["task_spec_hash"] != run.task.sha256
            or report["workspace_revision"] != run.workspace_revision
            or not required <= set(report["passed_check_ids"])
            or run.plan is None
            or {item.work_item_id for item in run.plan.items} != run.passed_items
            or run.reservations
        ):
            raise InvalidTransition("Success needs current verification and all work items passed")
    if target in {RunStatus.RECOVERING, RunStatus.WAITING_FOR_USER}:
        if run.status not in {RunStatus.RECOVERING, RunStatus.WAITING_FOR_USER}:
            run.resume_state = run.status
    else:
        run.resume_state = None
    run.status = target
    if previous == RunStatus.VALIDATING and target == RunStatus.RUNNING:
        run.validation = None
    if target in TERMINAL:
        run.lease_id = run.worker_id = run.lease_expires_at = None
        run.pending_human_request = None
        run.pending_human_plan_hash = None
        run.pending_human_session_ref = None


def apply(run: Run | None, event: Event) -> Run:
    p = event.payload
    if event.event_type == "RUN_CREATED":
        if run is not None or event.seq != 1:
            raise IntegrityError("RUN_CREATED must be the first event")
        run = Run(
            run_id=event.run_id,
            task=TaskSpec.model_validate(p["task"]),
            created_at=event.created_at,
            deadline_at=p["deadline_at"],
        )
    elif run is None:
        raise IntegrityError("Event stream has no RUN_CREATED")
    elif event.event_type == "STATE_CHANGED":
        if p["from"] != run.status:
            raise InvalidTransition("Event does not match previous state")
        if p["to"] == RunStatus.RUNNING and (
            not run.lease_id
            or not run.lease_expires_at
            or datetime.fromisoformat(event.created_at)
            >= datetime.fromisoformat(run.lease_expires_at)
        ):
            raise InvalidTransition("Entering RUNNING requires a live worker lease")
        _transition(run, RunStatus(p["to"]))
    elif event.event_type in {"PLAN_CREATED", "PLAN_REVISED"}:
        if run.status not in {RunStatus.PLANNING, RunStatus.READY, RunStatus.REPAIRING}:
            raise InvalidTransition("Plan change is not allowed in this phase")
        plan = Plan.model_validate(p["plan"])
        if plan.version != (run.plan.version + 1 if run.plan else 1):
            raise IntegrityError("Plan versions must be consecutive")
        # Event projection must remain able to replay plans accepted under an older
        # contract. New commands enforce current admission rules before appending.
        plan.check_task(run.task, enforce_unique_acceptance_ownership=False)
        source_model_call_id = p.get("source_model_call_id")
        execution_replan_data = p.get("execution_replan")
        if run.pending_human_plan_hash is not None:
            if plan.sha256 != run.pending_human_plan_hash or source_model_call_id is not None:
                raise IntegrityError("Replacement Plan does not match its human decision")
            run.pending_human_plan_hash = None
        if execution_replan_data is not None:
            if event.event_type != "PLAN_REVISED" or run.plan is None:
                raise IntegrityError("Execution replan requires an existing Plan revision")
            replan = ExecutionReplanRecord.model_validate(execution_replan_data)
            proposal = ExecutionReplanProposal(reason=replan.reason, items=plan.items)
            model_record = run.model_calls[-1] if run.model_calls else None
            tool_record = run.tool_calls[-1] if run.tool_calls else None
            session = run.agent_session
            preserved = tuple(
                item.work_item_id
                for item in run.plan.items
                if item.work_item_id in run.passed_items
            )
            if (
                p.get("operation") != "execution_replan"
                or run.status != RunStatus.REPAIRING
                or len(run.execution_replans) >= MAX_EXECUTION_REPLANS
                or source_model_call_id != replan.source_model_call_id
                or model_record is None
                or model_record.call_id != replan.source_model_call_id
                or model_record.purpose != "execution"
                or tool_record is None
                or tool_record.call_id != replan.source_tool_call_id
                or tool_record.name != "revise_plan"
                or tool_record.status != "success"
                or tool_record.arguments_hash != replan.proposal_hash
                or tool_record.workspace_revision_before != replan.workspace_revision
                or tool_record.workspace_revision_after != replan.workspace_revision
                or session is None
                or session.workspace_revision != replan.workspace_revision
                or replan.proposal_hash != proposal.sha256
                or replan.old_plan_version != run.plan.version
                or replan.old_plan_hash != run.plan.sha256
                or replan.new_plan_version != plan.version
                or replan.new_plan_hash != plan.sha256
                or replan.preserved_work_item_ids != preserved
            ):
                raise IntegrityError("Execution replan is not bound to its run evidence")
            try:
                check_execution_replan(
                    run.plan,
                    plan,
                    run.passed_items,
                    run.task,
                    enforce_unique_acceptance_ownership=False,
                )
            except PolicyDenied as exc:
                raise IntegrityError("Execution replan violates the bounded policy") from exc
            run.execution_replans.append(replan)
        elif source_model_call_id is not None and not any(
            record.call_id == source_model_call_id and record.purpose == "planning"
            for record in run.model_calls
        ):
            raise IntegrityError("Generated plan has no settled planning model receipt")
        run.plan = plan
        run.plan_source_model_call_id = source_model_call_id
        run.no_progress_reset_tool_count = len(run.tool_calls)
        if execution_replan_data is None:
            run.passed_items.clear()
        run.validation = None
        run.agent_session = None
    elif event.event_type == "HUMAN_REQUEST_CREATED":
        request = parse_human_request(p["request"])
        if (
            run.reservations
            or run.pending_human_request is not None
            or run.pending_human_plan_hash is not None
            or run.pending_human_session_ref is not None
        ):
            raise InvalidTransition(
                "Human request requires a quiescent Run without another request"
            )
        if isinstance(request, HumanPlanRequest):
            if run.status != RunStatus.PLANNING or run.plan is not None:
                raise InvalidTransition("Replacement Plan request requires PLANNING")
            record = next(
                (
                    item
                    for item in run.model_calls
                    if item.call_id == request.source_model_call_id and item.purpose == "planning"
                ),
                None,
            )
            if (
                record is None
                or record.response_artifact_ref != request.response_artifact_ref
                or request.task_spec_hash != run.task.sha256
                or request.requested_plan_version != (run.plan.version + 1 if run.plan else 1)
            ):
                raise IntegrityError("Human request is not bound to failed planning evidence")
        else:
            assert isinstance(request, HumanGuidanceRequest)
            session = run.agent_session
            if (
                run.status != RunStatus.RUNNING
                or run.plan is None
                or session is None
                or not matches_no_progress_evidence(
                    run.tool_calls,
                    run.no_progress_reset_tool_count,
                    request.pattern,
                    request.source_tool_call_id,
                    request.evidence_artifact_ref,
                    request.workspace_revision,
                    request.detail,
                )
                or request.task_spec_hash != run.task.sha256
                or request.plan_version != run.plan.version
                or request.plan_hash != run.plan.sha256
                or request.work_item_id != session.work_item_id
                or request.workspace_revision != session.workspace_revision
                or request.agent_session_artifact_ref != session.artifact_ref
                or request.next_iteration != session.next_iteration
            ):
                raise IntegrityError("Human request is not bound to no-progress evidence")
        run.pending_human_request = request
    elif event.event_type == "HUMAN_DECISION_RECORDED":
        decision = parse_human_decision(p["decision"])
        request = run.pending_human_request
        if run.status != RunStatus.WAITING_FOR_USER or request is None:
            raise IntegrityError("Human decision has no pending request")
        if isinstance(decision, HumanPlanDecision):
            if (
                not isinstance(request, HumanPlanRequest)
                or run.resume_state != RunStatus.PLANNING
                or request.request_id != decision.request_id
                or request.task_spec_hash != decision.task_spec_hash
                or decision.task_spec_hash != run.task.sha256
                or run.pending_human_plan_hash is not None
            ):
                raise IntegrityError("Human Plan decision does not match the pending request")
            run.pending_human_plan_hash = decision.plan_hash
        else:
            assert isinstance(decision, HumanGuidanceDecision)
            if (
                not isinstance(request, HumanGuidanceRequest)
                or run.resume_state != RunStatus.RUNNING
                or request.request_id != decision.request_id
                or request.task_spec_hash != decision.task_spec_hash
                or request.plan_version != decision.plan_version
                or request.workspace_revision != decision.workspace_revision
                or request.next_iteration != decision.next_iteration
                or decision.task_spec_hash != run.task.sha256
                or run.plan is None
                or decision.plan_version != run.plan.version
                or run.pending_human_session_ref is not None
            ):
                raise IntegrityError("Human guidance decision does not match the pending request")
            run.pending_human_session_ref = decision.guided_session_artifact_ref
            run.no_progress_reset_tool_count = len(run.tool_calls)
        run.human_decisions.append(decision)
        run.pending_human_request = None
    elif event.event_type == "CANCEL_REQUESTED":
        _transition(run, RunStatus.CANCELLED)
    elif event.event_type == "RUN_FAILED":
        stop = None
        request_budget = None
        if p.get("budget_stop") is not None:
            stop = BudgetStop.model_validate(p["budget_stop"])
            if p["reason"] != stop.reason_code.value:
                raise IntegrityError("Budget stop reason does not match the Run failure reason")
        if p.get("model_request_budget") is not None:
            if stop is None:
                raise IntegrityError("Model request budget evidence requires a budget stop")
            request_budget = ModelRequestBudgetEvidence.model_validate(p["model_request_budget"])
            expected_status = (
                RunStatus.PLANNING if request_budget.purpose == "planning" else RunStatus.RUNNING
            )
            if run.status != expected_status:
                raise IntegrityError("Model request budget evidence does not match the Run phase")
            if request_budget.call_id in run.model_reservations or any(
                record.call_id == request_budget.call_id for record in run.model_calls
            ):
                raise IntegrityError("Budget-stopped model request was already dispatched")
        _transition(run, RunStatus.FAILED)
        run.failure_reason = p["reason"]
        if stop is not None:
            run.budget_stop = stop
            run.model_request_budget = request_budget
    elif event.event_type == "LEASE_ACQUIRED":
        if run.terminal or p["epoch"] != run.lease_epoch + 1:
            raise IntegrityError("Invalid lease epoch or terminal run")
        run.lease_epoch = p["epoch"]
        run.lease_id, run.worker_id = p["lease_id"], p["worker_id"]
        run.lease_expires_at = p["expires_at"]
    elif event.event_type in {"LEASE_RENEWED", "LEASE_RELEASED"}:
        if (p["lease_id"], p["epoch"]) != (run.lease_id, run.lease_epoch):
            raise IntegrityError("Event has stale lease")
        if event.event_type == "LEASE_RELEASED":
            run.lease_id = run.worker_id = run.lease_expires_at = None
        else:
            run.lease_expires_at = p["expires_at"]
    elif event.event_type == "BUDGET_RESERVED":
        key = p["reservation_id"]
        if key in run.reservations or key in run.settled_ids or run.terminal:
            raise IntegrityError("Duplicate reservation or terminal run")
        amount = Usage.model_validate(p["amount"])
        run.occupied.plus(amount).check(run.task.budgets)
        run.reservations[key] = amount
    elif event.event_type == "BUDGET_SETTLED":
        key = p["reservation_id"]
        if key not in run.reservations:
            raise IntegrityError("Unknown budget reservation")
        actual = Usage.model_validate(p["actual"])
        reserved = run.reservations[key]
        for counter in ("model_calls", "tool_calls", "steps", "repair_cycles"):
            if getattr(actual, counter) < getattr(reserved, counter):
                raise IntegrityError("Attempt counters cannot be refunded")
        run.usage = run.usage.plus(actual)
        del run.reservations[key]
        run.unknown_reservations.discard(key)
        run.settled_ids.add(key)
    elif event.event_type == "BUDGET_USAGE_UNKNOWN":
        if p["reservation_id"] not in run.reservations:
            raise IntegrityError("Unknown reservation")
        run.unknown_reservations.add(p["reservation_id"])
    elif event.event_type == "MODEL_POLICY_BOUND":
        policy = ModelPolicyBinding.model_validate(p["policy"])
        if run.model_policy is not None and run.model_policy != policy:
            raise IntegrityError("A run cannot silently change its bound model policy")
        if run.task.model_policy_id != policy.policy_id:
            raise IntegrityError("Bound model policy does not match the TaskSpec")
        run.model_policy = policy
    elif event.event_type == "MODEL_CALL_RESERVED":
        reservation = ModelCallReservation.model_validate(p["reservation"])
        if run.model_policy is None:
            raise IntegrityError("Model calls require a bound policy")
        if reservation.call_id in run.model_reservations or any(
            item.call_id == reservation.call_id for item in run.model_calls
        ):
            raise IntegrityError("Duplicate model call ID")
        if (
            reservation.provider_id != run.model_policy.provider_id
            or reservation.model != run.model_policy.model
            or reservation.currency != run.model_policy.currency
        ):
            raise IntegrityError("Model reservation does not match the bound policy")
        if run.model_occupied_cost + reservation.reserved_cost > run.model_policy.max_run_cost:
            raise IntegrityError("Model reservation exceeds the bound per-run cost")
        if reservation.call_id not in run.reservations:
            raise IntegrityError("Model reservation requires a generic budget reservation")
        run.model_reservations[reservation.call_id] = reservation
    elif event.event_type == "MODEL_CALL_SETTLED":
        record = ModelCallRecord.model_validate(p["record"])
        reservation = run.model_reservations.get(record.call_id)
        if reservation is None or record.call_id not in run.settled_ids:
            raise IntegrityError("Model settlement has no settled budget reservation")
        if (
            record.purpose != reservation.purpose
            or record.request_hash != reservation.request_hash
            or record.provider_id != reservation.provider_id
            or record.model != reservation.model
            or record.currency != reservation.currency
        ):
            raise IntegrityError("Model settlement does not match its reservation")
        del run.model_reservations[record.call_id]
        run.unknown_model_calls.discard(record.call_id)
        run.model_calls.append(record)
    elif event.event_type == "MODEL_CALL_UNKNOWN":
        call_id = p["call_id"]
        if call_id not in run.model_reservations or call_id not in run.unknown_reservations:
            raise IntegrityError("Unknown model call has no unknown budget reservation")
        run.unknown_model_calls.add(call_id)
    elif event.event_type == "TOOL_CALL_RESERVED":
        reservation = ToolCallReservation.model_validate(p["reservation"])
        if reservation.call_id in run.tool_reservations or any(
            item.call_id == reservation.call_id for item in run.tool_calls
        ):
            raise IntegrityError("Duplicate tool call ID")
        if reservation.call_id not in run.reservations:
            raise IntegrityError("Tool reservation requires a generic budget reservation")
        run.tool_reservations[reservation.call_id] = reservation
    elif event.event_type == "TOOL_CALL_SETTLED":
        record = ToolCallRecord.model_validate(p["record"])
        reservation = run.tool_reservations.get(record.call_id)
        if reservation is None or record.call_id not in run.settled_ids:
            raise IntegrityError("Tool settlement has no settled budget reservation")
        if record.name != reservation.name or record.arguments_hash != reservation.arguments_hash:
            raise IntegrityError("Tool settlement does not match its reservation")
        del run.tool_reservations[record.call_id]
        run.unknown_tool_calls.discard(record.call_id)
        run.tool_calls.append(record)
    elif event.event_type == "TOOL_CALL_UNKNOWN":
        call_id = p["call_id"]
        if call_id not in run.tool_reservations or call_id not in run.unknown_reservations:
            raise IntegrityError("Unknown tool call has no unknown budget reservation")
        run.unknown_tool_calls.add(call_id)
    elif event.event_type in {"AGENT_SESSION_SAVED", "AGENT_SESSION_GUIDED"}:
        record = AgentSessionRecord.model_validate(p["session"])
        if run.status != RunStatus.RUNNING or run.plan is None or run.reservations:
            raise InvalidTransition("Agent session requires a quiescent running phase")
        if (
            record.task_spec_hash != run.task.sha256
            or record.plan_version != run.plan.version
            or record.work_item_id not in {item.work_item_id for item in run.plan.items}
        ):
            raise IntegrityError("Agent session does not match the current task and plan")
        if event.event_type == "AGENT_SESSION_GUIDED":
            current = run.agent_session
            if (
                current is None
                or run.pending_human_session_ref != record.artifact_ref
                or record.next_iteration != current.next_iteration
                or record.workspace_revision != current.workspace_revision
                or record.message_count <= current.message_count
            ):
                raise IntegrityError("Guided Agent session does not match its human decision")
            run.pending_human_session_ref = None
        elif run.agent_session is not None and (
            record.next_iteration <= run.agent_session.next_iteration
        ):
            raise IntegrityError("Agent session iteration must advance")
        if record.covered_event_seq != event.seq - 1:
            raise IntegrityError("Agent session must cover the immediately preceding event")
        if run.agent_session is not None and record.work_item_id != run.agent_session.work_item_id:
            run.no_progress_reset_tool_count = len(run.tool_calls)
        run.agent_session = record
    elif event.event_type == "WORKSPACE_ORIGIN_BOUND":
        origin = WorkspaceOrigin.model_validate(p["origin"])
        if run.workspace_origin is not None and run.workspace_origin != origin:
            raise IntegrityError("A Run cannot change its source workspace origin")
        if run.task.repository.base_commit != origin.source_revision:
            raise IntegrityError("Workspace origin does not match the TaskSpec base revision")
        run.workspace_origin = origin
    elif event.event_type == "PROMOTION_RESERVED":
        intent = PromotionIntent.model_validate(p["intent"])
        if run.status != RunStatus.SUCCEEDED or run.workspace_origin is None:
            raise IntegrityError("Promotion requires a successful Run with a bound origin")
        if run.promotion_intent is not None or run.promotion_receipt is not None:
            raise IntegrityError("A Run can have only one promotion intent")
        if (
            intent.plan.source_path_hash != run.workspace_origin.source_path_hash
            or intent.plan.source_revision_before != run.workspace_origin.source_revision
            or intent.plan.source_manifest_ref_before != run.workspace_origin.source_manifest_ref
            or intent.plan.git_head_before != run.workspace_origin.git_head
            or intent.plan.candidate_revision != run.workspace_revision
            or run.last_checkpoint is None
            or intent.plan.candidate_manifest_ref != run.last_checkpoint["manifest_sha256"]
        ):
            raise IntegrityError("Promotion intent does not match the successful Run evidence")
        run.promotion_intent = intent
    elif event.event_type == "PROMOTION_SETTLED":
        receipt = PromotionReceipt.model_validate(p["receipt"])
        if run.promotion_intent is None or run.promotion_receipt is not None:
            raise IntegrityError("Promotion receipt has no pending intent")
        if (
            receipt.promotion_id != run.promotion_intent.promotion_id
            or receipt.plan_hash != run.promotion_intent.plan.sha256
            or receipt.source_revision_after != run.promotion_intent.plan.candidate_revision
            or receipt.git_head_after != run.promotion_intent.plan.git_head_before
        ):
            raise IntegrityError("Promotion receipt does not match its intent")
        run.promotion_receipt = receipt
    elif event.event_type == "TASK_SPEC_AMENDED":
        task = TaskSpec.model_validate(p["task"])
        if run.terminal or task.task_id != run.task.task_id:
            raise IntegrityError("Cannot amend this task")
        if task.spec_version != run.task.spec_version + 1 or not p["user_source"]:
            raise IntegrityError("Amendment requires consecutive version and user source")
        if run.reservations:
            raise IntegrityError("Unsettled operations must be reconciled before amendment")
        if run.workspace_origin is not None and task.repository != run.task.repository:
            raise IntegrityError("A staged Run cannot change its bound repository identity")
        run.occupied.check(task.budgets)
        run.task = task
        run.deadline_at = p["deadline_at"]
        run.plan = None
        run.plan_source_model_call_id = None
        run.pending_human_request = None
        run.pending_human_plan_hash = None
        run.pending_human_session_ref = None
        run.no_progress_reset_tool_count = len(run.tool_calls)
        run.passed_items.clear()
        run.validation = None
        run.agent_session = None
        run.status, run.resume_state = RunStatus.PLANNING, None
        run.lease_id = run.worker_id = run.lease_expires_at = None
    elif event.event_type == "WORK_ITEM_PASSED":
        if run.plan is None or run.status != RunStatus.VALIDATING:
            raise InvalidTransition("Work item pass requires a plan and validation phase")
        ready = {item.work_item_id: item for item in run.plan.ready_items(run.passed_items)}
        item = ready.get(p["work_item_id"])
        report = run.validation
        if (
            item is None
            or report is None
            or not set(item.acceptance_ids) <= set(report["passed_check_ids"])
        ):
            raise InvalidTransition("Work item dependencies or checks have not passed")
        run.passed_items.add(item.work_item_id)
    elif event.event_type == "VALIDATION_RECORDED":
        if run.status != RunStatus.VALIDATING or p["task_spec_hash"] != run.task.sha256:
            raise InvalidTransition("Validation does not match current task and phase")
        if not set(p["passed_check_ids"]) <= {check.id for check in run.task.acceptance}:
            raise IntegrityError("Unknown acceptance check")
        if p["workspace_revision"] != run.workspace_revision or not p["evidence_ref"]:
            raise IntegrityError("Validation must reference current workspace and evidence")
        run.validation = p
    elif event.event_type == "CHECKPOINT_COMMITTED":
        if p["event_seq"] != run.seq or p["task_spec_hash"] != run.task.sha256:
            raise IntegrityError("Checkpoint must cover the immediately preceding state")
        if run.reservations:
            raise IntegrityError("In-flight operations prohibit a consistent checkpoint")
        run.last_checkpoint = p
        if run.workspace_revision != p["workspace_revision"]:
            run.validation = None
        run.workspace_revision = p["workspace_revision"]
    else:
        raise IntegrityError(f"Unsupported event type: {event.event_type}")
    run.seq = event.seq
    run.event_hash = event.event_hash
    return run


def project(events: list[Event]) -> Run:
    run: Run | None = None
    previous_hash = ""
    previous_time: datetime | None = None
    previous_id: str | None = None
    event_ids: set[str] = set()
    for seq, event in enumerate(events, start=1):
        if event.seq != seq or event.previous_hash != previous_hash:
            raise IntegrityError("Non-contiguous or corrupted event chain")
        if event.event_hash != event.calculated_hash() or (run and event.run_id != run.run_id):
            raise IntegrityError("Event hash or run ID mismatch")
        if event.event_id in event_ids or event.causation_id != previous_id:
            raise IntegrityError("Duplicate event ID or broken causation chain")
        current_time = datetime.fromisoformat(event.created_at)
        if current_time.tzinfo is None or (previous_time and current_time < previous_time):
            raise IntegrityError("Event timestamps must be timezone-aware and monotonic")
        try:
            run = apply(run, event)
        except (KeyError, TypeError, ValueError) as exc:
            raise IntegrityError("Malformed event payload") from exc
        previous_hash, previous_time = event.event_hash, current_time
        previous_id = event.event_id
        event_ids.add(event.event_id)
    if run is None:
        raise IntegrityError("Empty event stream")
    return run


def projection_hash(run: Run) -> str:
    return digest(run.as_dict())
