from __future__ import annotations

from dataclasses import dataclass

from horizon.application.services import HarnessService, LeaseToken
from horizon.domain.agent import AgentSession, AgentSessionRecord
from horizon.domain.common import canonical_json
from horizon.domain.errors import Conflict
from horizon.domain.human import HumanGuidanceRequest
from horizon.domain.model import ModelMessage
from horizon.domain.ports import ArtifactStorePort
from horizon.domain.run import Run


@dataclass(frozen=True)
class GuidanceResult:
    run: Run
    session_artifact_ref: str


class OperatorGuidanceService:
    """Apply bounded local guidance without expanding the immutable execution contract."""

    def __init__(self, service: HarnessService, artifact_store: ArtifactStorePort):
        self.service = service
        self.artifact_store = artifact_store

    def apply(
        self,
        run_id: str,
        guidance: str,
        token: LeaseToken,
        key: str,
    ) -> GuidanceResult:
        guidance = guidance.strip()
        if not guidance or len(guidance) > 8000:
            raise ValueError("Operator guidance must contain 1 to 8000 characters")
        run = self.service.store.get(run_id)
        self.service.check_worker(run, token)
        request = run.pending_human_request
        record = run.agent_session
        if not isinstance(request, HumanGuidanceRequest) or record is None or run.plan is None:
            raise Conflict("Run has no pending operator-guidance request")
        session = AgentSession.model_validate_json(self.artifact_store.read(record.artifact_ref))
        if (
            session.run_id != run.run_id
            or session.task_spec_hash != request.task_spec_hash
            or session.plan_version != request.plan_version
            or session.work_item_id != request.work_item_id
            or session.workspace_revision != request.workspace_revision
            or session.next_iteration != request.next_iteration
            or session.covered_event_seq != record.covered_event_seq
            or len(session.messages) != record.message_count
            or record.artifact_ref != request.agent_session_artifact_ref
        ):
            raise Conflict("Pending guidance request does not match the persisted Agent session")
        messages = (
            *session.messages,
            ModelMessage(
                role="user",
                content=(
                    "Trusted operator guidance follows. It is advice within the immutable "
                    "TaskSpec and does not expand paths, tools, acceptance checks, or budgets:\n"
                    f"{guidance}"
                ),
            ),
        )
        guided = AgentSession(
            run_id=run.run_id,
            task_spec_hash=session.task_spec_hash,
            plan_version=session.plan_version,
            work_item_id=session.work_item_id,
            next_iteration=session.next_iteration,
            covered_event_seq=run.seq + 2,
            workspace_revision=session.workspace_revision,
            messages=messages,
        )
        payload = canonical_json(guided.model_dump(mode="json")).encode("utf-8")
        artifact_ref = self.artifact_store.put(payload)
        if self.artifact_store.read(artifact_ref) != payload:
            raise Conflict("Guided Agent session artifact verification failed")
        guided_record = AgentSessionRecord(
            artifact_ref=artifact_ref,
            task_spec_hash=guided.task_spec_hash,
            plan_version=guided.plan_version,
            work_item_id=guided.work_item_id,
            next_iteration=guided.next_iteration,
            covered_event_seq=guided.covered_event_seq,
            workspace_revision=guided.workspace_revision,
            message_count=len(guided.messages),
        )
        resolved = self.service.resolve_operator_guidance(
            run_id,
            guided_record,
            token,
            key,
        )
        return GuidanceResult(run=resolved, session_artifact_ref=artifact_ref)
