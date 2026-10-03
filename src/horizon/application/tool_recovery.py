from __future__ import annotations

import json
from typing import Literal
from uuid import uuid4

from horizon.application.model_recovery import (
    RecoverableToolTurn,
    load_recorded_model_response,
    pending_unknown_readonly_tool_turn,
    pending_unknown_tool_turn,
)
from horizon.application.services import HarnessService, LeaseToken
from horizon.domain.agent import AgentSession, AgentSessionRecord
from horizon.domain.common import canonical_json
from horizon.domain.errors import Conflict
from horizon.domain.model import ModelMessage
from horizon.domain.ports import ArtifactStorePort, WriteRecoveryPort
from horizon.domain.run import Run
from horizon.domain.tools import ToolCallRecord, ToolCallReservation


class ToolRecoveryService:
    """Apply explicit, auditable dispositions to uncertain tool intents."""

    def __init__(
        self,
        service: HarnessService,
        artifact_store: ArtifactStorePort,
        write_recovery: WriteRecoveryPort | None = None,
    ):
        self.service = service
        self.artifact_store = artifact_store
        self.write_recovery = write_recovery

    def _verify_manifest_revision(self, manifest_ref: str, revision: str) -> None:
        try:
            manifest = json.loads(self.artifact_store.read(manifest_ref))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Conflict("Current workspace manifest is not valid JSON") from exc
        if not isinstance(manifest, dict) or manifest.get("workspace_revision") != revision:
            raise Conflict("Current workspace manifest does not match its revision")

    def _resolution_artifact(
        self,
        *,
        decision: str,
        call_id: str,
        tool: str,
        workspace_revision: str,
        effect: str,
    ) -> tuple[str, str]:
        content = canonical_json(
            {
                "decision": decision,
                "original_call_id": call_id,
                "tool": tool,
                "workspace_revision": workspace_revision,
                "effect": effect,
            }
        )
        return content, self.artifact_store.put(content.encode("utf-8"))

    def _completed_turn_session(
        self,
        run: Run,
        pending: RecoverableToolTurn,
        tool_content: str,
        final_workspace_revision: str,
    ) -> AgentSessionRecord:
        record = run.agent_session
        if record is None or run.plan is None:
            raise Conflict("Recovered tool turn has no persisted Agent session")
        session = AgentSession.model_validate_json(self.artifact_store.read(record.artifact_ref))
        active_item = next(
            (item for item in run.plan.items if item.work_item_id == record.work_item_id),
            None,
        )
        if (
            session.run_id != run.run_id
            or session.task_spec_hash != run.task.sha256
            or session.plan_version != run.plan.version
            or active_item is None
            or active_item.work_item_id in run.passed_items
            or not set(active_item.dependencies) <= run.passed_items
            or session.work_item_id != active_item.work_item_id
            or session.next_iteration != record.next_iteration
            or session.covered_event_seq != record.covered_event_seq
            or session.workspace_revision != record.workspace_revision
            or len(session.messages) != record.message_count
        ):
            raise Conflict("Persisted Agent session does not match the recovered tool turn")
        response = load_recorded_model_response(pending.model, self.artifact_store)
        if len(response.message.tool_calls) != 1:
            raise Conflict("Tool recovery requires exactly one model tool call")
        messages = (
            *session.messages,
            response.message,
            ModelMessage(
                role="tool",
                content=tool_content,
                tool_call_id=response.message.tool_calls[0].id,
            ),
        )
        recovered = AgentSession(
            run_id=run.run_id,
            task_spec_hash=session.task_spec_hash,
            plan_version=session.plan_version,
            work_item_id=session.work_item_id,
            next_iteration=session.next_iteration + 1,
            # The atomic command appends budget settlement and tool receipt first.
            covered_event_seq=run.seq + 2,
            workspace_revision=final_workspace_revision,
            messages=messages,
        )
        payload = canonical_json(recovered.model_dump(mode="json")).encode("utf-8")
        artifact_ref = self.artifact_store.put(payload)
        if self.artifact_store.read(artifact_ref) != payload:
            raise Conflict("Recovered Agent session artifact verification failed")
        return AgentSessionRecord(
            artifact_ref=artifact_ref,
            task_spec_hash=recovered.task_spec_hash,
            plan_version=recovered.plan_version,
            work_item_id=recovered.work_item_id,
            next_iteration=recovered.next_iteration,
            covered_event_seq=recovered.covered_event_seq,
            workspace_revision=recovered.workspace_revision,
            message_count=len(recovered.messages),
        )

    def authorize_readonly_retry(
        self,
        run_id: str,
        call_id: str,
        *,
        current_workspace_revision: str,
        current_workspace_manifest_ref: str,
        token: LeaseToken,
    ) -> Run:
        run = self.service.store.get(run_id)
        self.service.check_worker(run, token)
        pending = pending_unknown_readonly_tool_turn(
            run,
            self.service.store.events(run_id),
            self.artifact_store,
        )
        if pending is None or pending.reservation.call_id != call_id:
            raise Conflict(
                "Only the single uncertain read-only tool at the current Agent turn can be retried"
            )
        reservation = pending.reservation
        if reservation.workspace_revision != current_workspace_revision:
            raise Conflict("Workspace changed after the uncertain read-only tool dispatch")
        self._verify_manifest_revision(
            current_workspace_manifest_ref,
            current_workspace_revision,
        )
        content, artifact_ref = self._resolution_artifact(
            decision="retry_readonly",
            call_id=call_id,
            tool=reservation.name,
            workspace_revision=current_workspace_revision,
            effect=(
                "The uncertain attempt is conservatively charged once and cancelled; "
                "the persisted model response may dispatch a new tool call."
            ),
        )
        return self.service.settle_tool_call(
            run_id,
            ToolCallRecord(
                call_id=call_id,
                name=reservation.name,
                arguments_hash=reservation.arguments_hash,
                status="cancelled",
                output_hash=artifact_ref,
                workspace_revision_before=current_workspace_revision,
                workspace_revision_after=current_workspace_revision,
                artifact_ref=artifact_ref,
                workspace_manifest_ref=current_workspace_manifest_ref,
                recovery_disposition="retry_readonly",
            ),
            token,
            f"resolve_readonly_retry_{call_id}_{uuid4().hex}",
        )

    def _discard_check_context(
        self,
        run_id: str,
        call_id: str,
        *,
        current_workspace_revision: str,
        current_workspace_manifest_ref: str,
        token: LeaseToken,
    ) -> tuple[Run, RecoverableToolTurn, ToolCallReservation]:
        run = self.service.store.get(run_id)
        self.service.check_worker(run, token)
        pending = pending_unknown_tool_turn(
            run,
            self.service.store.events(run_id),
            self.artifact_store,
        )
        active_item = None
        if run.plan is not None and run.agent_session is not None:
            active_item = next(
                (
                    item
                    for item in run.plan.items
                    if item.work_item_id == run.agent_session.work_item_id
                ),
                None,
            )
        if (
            pending is None
            or pending.reservation.call_id != call_id
            or pending.reservation.name != "run_check"
            or active_item is None
            or "run_check" not in active_item.allowed_tools
        ):
            raise Conflict(
                "Only the single uncertain run_check at the current Agent turn can be discarded"
            )
        response = load_recorded_model_response(pending.model, self.artifact_store)
        if len(response.message.tool_calls) != 1:
            raise Conflict("Check recovery requires exactly one model tool call")
        arguments = response.message.tool_calls[0].function.arguments
        if (
            set(arguments) != {"check_id"}
            or arguments["check_id"] not in active_item.acceptance_ids
        ):
            raise Conflict("Uncertain check is outside the active WorkItem acceptance scope")
        reservation = pending.reservation
        if reservation.workspace_revision != current_workspace_revision:
            raise Conflict("Workspace changed after the uncertain check dispatch")
        self._verify_manifest_revision(
            current_workspace_manifest_ref,
            current_workspace_revision,
        )
        return run, pending, reservation

    def validate_check_discard(
        self,
        run_id: str,
        call_id: str,
        *,
        current_workspace_revision: str,
        current_workspace_manifest_ref: str,
        token: LeaseToken,
    ) -> ToolCallReservation:
        _, _, reservation = self._discard_check_context(
            run_id,
            call_id,
            current_workspace_revision=current_workspace_revision,
            current_workspace_manifest_ref=current_workspace_manifest_ref,
            token=token,
        )
        return reservation

    def discard_check_result(
        self,
        run_id: str,
        call_id: str,
        *,
        current_workspace_revision: str,
        current_workspace_manifest_ref: str,
        sandbox_stopped: bool,
        token: LeaseToken,
        sandbox_stop_evidence: Literal[
            "operator_confirmation", "controller_verified"
        ] = "operator_confirmation",
    ) -> Run:
        if not sandbox_stopped:
            raise Conflict(
                "Discarding an uncertain check requires confirmation that its sandbox stopped"
            )
        run, pending, reservation = self._discard_check_context(
            run_id,
            call_id,
            current_workspace_revision=current_workspace_revision,
            current_workspace_manifest_ref=current_workspace_manifest_ref,
            token=token,
        )
        if sandbox_stop_evidence == "controller_verified":
            stop_statement = (
                "The controller verified and removed the labeled Docker attempt after it stopped."
            )
        else:
            stop_statement = "The operator confirmed the old sandbox stopped."
        content, artifact_ref = self._resolution_artifact(
            decision="discard_check",
            call_id=call_id,
            tool=reservation.name,
            workspace_revision=current_workspace_revision,
            effect=(
                f"{stop_statement} The uncertain check result was discarded without inferring "
                "pass or failure and will not be replayed."
            ),
        )
        record = ToolCallRecord(
            call_id=call_id,
            name=reservation.name,
            arguments_hash=reservation.arguments_hash,
            status="cancelled",
            output_hash=artifact_ref,
            workspace_revision_before=current_workspace_revision,
            workspace_revision_after=current_workspace_revision,
            artifact_ref=artifact_ref,
            workspace_manifest_ref=current_workspace_manifest_ref,
            recovery_disposition="discard_check",
        )
        session = self._completed_turn_session(
            run,
            pending,
            content,
            current_workspace_revision,
        )
        return self.service.settle_tool_call_and_save_session(
            run_id,
            record,
            session,
            token,
            f"resolve_discard_check_{call_id}_{uuid4().hex}",
        )

    def resolve_replace(
        self,
        run_id: str,
        call_id: str,
        *,
        decision: Literal["accept", "rollback"],
        token: LeaseToken,
    ) -> Run:
        return self._resolve_write(
            run_id,
            call_id,
            decision=decision,
            token=token,
            expected_tool="replace_text",
        )

    def resolve_write(
        self,
        run_id: str,
        call_id: str,
        *,
        decision: Literal["accept", "rollback"],
        token: LeaseToken,
    ) -> Run:
        return self._resolve_write(
            run_id,
            call_id,
            decision=decision,
            token=token,
            expected_tool=None,
        )

    def _resolve_write(
        self,
        run_id: str,
        call_id: str,
        *,
        decision: Literal["accept", "rollback"],
        token: LeaseToken,
        expected_tool: str | None,
    ) -> Run:
        if self.write_recovery is None:
            raise Conflict("Write recovery adapter is unavailable")
        run = self.service.store.get(run_id)
        self.service.check_worker(run, token)
        pending = pending_unknown_tool_turn(
            run,
            self.service.store.events(run_id),
            self.artifact_store,
        )
        active_item = None
        if run.plan is not None and run.agent_session is not None:
            active_item = next(
                (
                    item
                    for item in run.plan.items
                    if item.work_item_id == run.agent_session.work_item_id
                ),
                None,
            )
        if (
            pending is None
            or pending.reservation.call_id != call_id
            or pending.reservation.name not in {"replace_text", "apply_patch"}
            or (expected_tool is not None and pending.reservation.name != expected_tool)
            or active_item is None
            or pending.reservation.name not in active_item.allowed_tools
            or pending.reservation.workspace_revision is None
            or pending.reservation.workspace_manifest_ref is None
        ):
            raise Conflict(
                "Only one uncertain supported write call with a pre-dispatch manifest can be "
                "resolved"
            )
        response = load_recorded_model_response(pending.model, self.artifact_store)
        if len(response.message.tool_calls) != 1:
            raise Conflict("Write recovery requires exactly one model tool call")
        arguments = response.message.tool_calls[0].function.arguments
        reservation = pending.reservation
        if reservation.name == "replace_text":
            assessment = self.write_recovery.assess_replace_recovery(
                arguments,
                reservation.workspace_manifest_ref,
            )
            accept_disposition = "accept_replace"
            rollback_disposition = "rollback_replace"
            noun = "Replace"
        else:
            assessment = self.write_recovery.assess_patch_recovery(
                arguments,
                reservation.workspace_manifest_ref,
            )
            accept_disposition = "accept_patch"
            rollback_disposition = "rollback_patch"
            noun = "Patch"
        if assessment.pre_revision != reservation.workspace_revision:
            raise Conflict("Pre-dispatch manifest does not match the tool reservation")

        if decision == "accept":
            if assessment.state != "expected_effect":
                raise Conflict(f"{noun} can be accepted only when its exact expected effect exists")
            status = "success"
            disposition = accept_disposition
            final_revision = assessment.current_revision
            final_manifest = assessment.current_manifest_ref
            effect = f"The exact deterministic {noun.lower()} effect was found and accepted."
        else:
            if assessment.state not in {"pre_effect", "expected_effect"}:
                raise Conflict(
                    f"Diverged {noun.lower()} effect cannot be rolled back automatically"
                )
            if reservation.name == "replace_text":
                final_revision, final_manifest = self.write_recovery.rollback_replace_recovery(
                    arguments,
                    reservation.workspace_manifest_ref,
                    assessment.current_revision,
                )
            else:
                final_revision, final_manifest = self.write_recovery.rollback_patch_recovery(
                    arguments,
                    reservation.workspace_manifest_ref,
                    assessment.current_revision,
                )
            if final_revision != assessment.pre_revision:
                raise Conflict(f"{noun} rollback did not restore the pre-dispatch revision")
            status = "cancelled"
            disposition = rollback_disposition
            effect = "The workspace was restored to the pre-dispatch manifest revision."

        self._verify_manifest_revision(final_manifest, final_revision)
        content, artifact_ref = self._resolution_artifact(
            decision=disposition,
            call_id=call_id,
            tool=reservation.name,
            workspace_revision=final_revision,
            effect=effect,
        )
        tool_record = ToolCallRecord(
            call_id=call_id,
            name=reservation.name,
            arguments_hash=reservation.arguments_hash,
            status=status,
            output_hash=artifact_ref,
            workspace_revision_before=reservation.workspace_revision,
            workspace_revision_after=final_revision,
            artifact_ref=artifact_ref,
            workspace_manifest_ref=final_manifest,
            recovery_disposition=disposition,
        )
        session = self._completed_turn_session(
            run,
            pending,
            content,
            final_revision,
        )
        return self.service.settle_tool_call_and_save_session(
            run_id,
            tool_record,
            session,
            token,
            f"resolve_{disposition}_{call_id}_{uuid4().hex}",
        )
