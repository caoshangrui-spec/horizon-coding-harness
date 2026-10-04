from __future__ import annotations

from horizon.domain.agent import AgentSessionRecord
from horizon.domain.common import digest
from horizon.domain.errors import Conflict, IntegrityError
from horizon.domain.events import Event
from horizon.domain.memory import RunMemoryEntry, RunMemorySnapshot
from horizon.domain.plan import Plan
from horizon.domain.ports import ArtifactStorePort
from horizon.domain.run import Run
from horizon.domain.tools import ToolCallRecord

SUPPORTED_MEMORY_TOOLS = frozenset(
    {
        "search_repo",
        "read_file",
        "retrieve_code",
        "replace_text",
        "apply_patch",
        "create_file",
        "run_check",
    }
)
MAX_MEMORY_EVIDENCE_BYTES = 512 * 1024


def _kind(tool_name: str) -> str:
    if tool_name in {"replace_text", "apply_patch", "create_file"}:
        return "workspace_change"
    if tool_name == "run_check":
        return "validation"
    return "observation"


def _statement(record: ToolCallRecord) -> str:
    labels = {
        "success": "succeeded",
        "error": "failed",
        "cancelled": "was cancelled",
    }
    return (
        f"Controller observed tool {record.name} {labels[record.status]}; "
        f"the exact output remains in evidence artifact {record.artifact_ref}."
    )


class RunMemoryProjector:
    """Derive bounded, evidence-backed Run memory from authoritative events."""

    def __init__(
        self,
        artifacts: ArtifactStorePort,
        *,
        max_entries: int = 12,
        excerpt_chars: int = 240,
    ):
        if not 1 <= max_entries <= 50:
            raise ValueError("Run memory accepts between 1 and 50 entries")
        if not 0 <= excerpt_chars <= 1_000:
            raise ValueError("Run memory excerpt limit must be between 0 and 1000 characters")
        self.artifacts = artifacts
        self.max_entries = max_entries
        self.excerpt_chars = excerpt_chars

    def _settled_entry(
        self,
        run: Run,
        event: Event,
        record: ToolCallRecord,
        current_revision: str,
        work_item_id: str,
    ) -> RunMemoryEntry:
        if record.status == "unknown":
            raise IntegrityError("Unknown tool effects cannot be stored as settled observations")
        if record.artifact_ref is None or record.artifact_ref != record.output_hash:
            raise IntegrityError("Settled tool memory requires content-addressed output evidence")
        raw = self.artifacts.read(record.artifact_ref, max_bytes=MAX_MEMORY_EVIDENCE_BYTES)
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise IntegrityError("Tool memory evidence must be UTF-8 text") from exc
        dependency_revision = record.workspace_revision_after or record.workspace_revision_before
        status = "active" if dependency_revision == current_revision else "stale"
        assert run.plan is not None
        return RunMemoryEntry.create(
            scope_id=run.run_id,
            source_run_id=run.run_id,
            task_spec_hash=run.task.sha256,
            work_item_id=work_item_id,
            source_event_id=event.event_id,
            source_event_seq=event.seq,
            source_event_hash=event.event_hash,
            source_call_id=record.call_id,
            kind=_kind(record.name),
            tool_name=record.name,
            outcome=record.status,
            status=status,
            dependency_revision=dependency_revision,
            evidence_ref=record.artifact_ref,
            evidence_hash=record.output_hash,
            statement=_statement(record),
            excerpt=content[: self.excerpt_chars],
        )

    @staticmethod
    def _unknown_entry(
        run: Run,
        event: Event,
        current_revision: str,
        work_item_id: str,
    ) -> RunMemoryEntry:
        call_id = event.payload["call_id"]
        reservation = run.tool_reservations.get(call_id)
        if reservation is None:
            raise IntegrityError("Unknown tool memory has no current reservation")
        assert run.plan is not None
        return RunMemoryEntry.create(
            scope_id=run.run_id,
            source_run_id=run.run_id,
            task_spec_hash=run.task.sha256,
            work_item_id=work_item_id,
            source_event_id=event.event_id,
            source_event_seq=event.seq,
            source_event_hash=event.event_hash,
            source_call_id=call_id,
            kind="unresolved_effect",
            tool_name=reservation.name,
            outcome="unknown",
            status="unresolved",
            dependency_revision=reservation.workspace_revision or current_revision,
            statement=(
                f"Tool {reservation.name} has an unknown effect; no success or failure fact "
                "may be inferred until explicit reconciliation."
            ),
        )

    def project(
        self,
        run: Run,
        events: list[Event],
        *,
        current_revision: str,
        active_work_item_id: str,
    ) -> RunMemorySnapshot:
        if run.plan is None:
            raise Conflict("Run memory requires an active plan")
        final_work_items = {item.work_item_id for item in run.plan.items}
        if active_work_item_id not in final_work_items:
            raise Conflict("Run memory active work item is not in the current plan")
        if not events or events[-1].seq != run.seq:
            raise IntegrityError("Run memory events do not cover the requested Run state")

        entries: list[RunMemoryEntry] = []
        relevant_events: list[Event] = []
        # Attribute evidence against the Plan version active at each event. Runtime replanning may
        # legitimately remove an unfinished WorkItem ID from the current Plan while its historical
        # sessions and observations remain auditable.
        current_plan: Plan | None = None
        current_work_item_id: str | None = None
        passed_items: set[str] = set()
        observed_unknown: set[str] = set()
        for event in events:
            if event.event_type == "TASK_SPEC_AMENDED":
                current_plan = None
                current_work_item_id = None
                passed_items.clear()
                entries.clear()
                relevant_events.clear()
                observed_unknown.clear()
                continue
            if event.event_type in {"PLAN_CREATED", "PLAN_REVISED"}:
                candidate = Plan.model_validate(event.payload["plan"])
                if current_plan is not None and candidate.version != current_plan.version + 1:
                    raise IntegrityError("Run memory observed a non-consecutive Plan revision")
                if current_plan is None and candidate.version != 1:
                    raise IntegrityError("Run memory did not observe Plan version one first")
                if event.payload.get("execution_replan") is None:
                    passed_items.clear()
                current_plan = candidate
                ready = current_plan.ready_items(passed_items)
                current_work_item_id = ready[0].work_item_id if ready else None
                continue
            if event.event_type == "WORK_ITEM_PASSED":
                if current_plan is None:
                    raise IntegrityError("Work item passed before a Plan existed")
                passed_items.add(event.payload["work_item_id"])
                ready = current_plan.ready_items(passed_items)
                current_work_item_id = ready[0].work_item_id if ready else None
                continue
            if event.event_type in {"AGENT_SESSION_SAVED", "AGENT_SESSION_GUIDED"}:
                session = AgentSessionRecord.model_validate(event.payload["session"])
                known_work_items = (
                    {item.work_item_id for item in current_plan.items}
                    if current_plan is not None
                    else set()
                )
                if (
                    current_plan is None
                    or session.plan_version != current_plan.version
                    or session.work_item_id not in known_work_items
                ):
                    raise IntegrityError("Agent session references an unknown work item")
                current_work_item_id = session.work_item_id
                continue
            if event.event_type == "TOOL_CALL_SETTLED":
                record = ToolCallRecord.model_validate(event.payload["record"])
                if record.name not in SUPPORTED_MEMORY_TOOLS:
                    continue
                if current_work_item_id is None:
                    raise IntegrityError("Tool memory has no active work item session")
                relevant_events.append(event)
                entries.append(
                    self._settled_entry(
                        run,
                        event,
                        record,
                        current_revision,
                        current_work_item_id,
                    )
                )
                continue
            if event.event_type == "TOOL_CALL_UNKNOWN":
                call_id = event.payload.get("call_id")
                if call_id not in run.unknown_tool_calls:
                    continue
                if current_work_item_id is None:
                    raise IntegrityError("Unknown tool memory has no active work item session")
                relevant_events.append(event)
                entries.append(
                    self._unknown_entry(
                        run,
                        event,
                        current_revision,
                        current_work_item_id,
                    )
                )
                observed_unknown.add(call_id)
        if current_plan != run.plan:
            raise IntegrityError("Run memory Plan history does not reach the requested Run state")
        if observed_unknown != run.unknown_tool_calls:
            raise IntegrityError("Current unknown tool has no source event")

        entries.sort(key=lambda entry: (entry.source_event_seq, entry.memory_id))
        relevant_events.sort(key=lambda event: event.seq)
        selected = tuple(entries[-self.max_entries :])
        omitted = len(entries) - len(selected)
        return RunMemorySnapshot(
            run_id=run.run_id,
            task_spec_hash=run.task.sha256,
            plan_version=run.plan.version,
            work_item_id=active_work_item_id,
            covered_event_seq=run.seq,
            workspace_revision=current_revision,
            relevant_event_digest=digest(
                [(event.event_id, event.event_hash) for event in relevant_events]
            ),
            max_entries=self.max_entries,
            excerpt_chars=self.excerpt_chars,
            total_entry_count=len(entries),
            included_entry_count=len(selected),
            omitted_entry_count=omitted,
            active_count=sum(entry.status == "active" for entry in selected),
            stale_count=sum(entry.status == "stale" for entry in selected),
            unresolved_count=sum(entry.status == "unresolved" for entry in selected),
            entries=selected,
        )
