from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from horizon.adapters.model.scripted import ScriptedModelGateway
from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.adapters.persistence.campaign_budget import CampaignBudgetLedger
from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.adapters.workspace.snapshot import SnapshotManager
from horizon.application.agent_loop import AgentLoopConfig, CodingAgentRunner
from horizon.application.human import ContextCapacityResumeService
from horizon.application.services import HarnessService, LeaseToken
from horizon.application.terminal_evidence import (
    TerminalEvidenceResult,
    export_terminal_evidence_pack,
    verify_terminal_evidence_pack,
)
from horizon.domain.agent import AgentSession
from horizon.domain.common import canonical_json, digest
from horizon.domain.context_capacity_demo import (
    ContextCapacityDemoEvidence,
    ContextCapacityEvidenceFile,
    ContextCapacityEvidencePack,
)
from horizon.domain.errors import ContextCapacityStop, ContextStopReason
from horizon.domain.events import Event
from horizon.domain.human import HumanContextDecision, HumanContextRequest
from horizon.domain.model import CampaignBudget, PriceCard
from horizon.domain.plan import Plan, WorkItem
from horizon.domain.run import projection_hash
from horizon.domain.run_evaluation import ScriptedModelAction
from horizon.domain.states import RunStatus
from horizon.domain.task import AcceptanceCheck, BudgetSpec, Constraints, Repository, TaskSpec
from horizon.domain.tools import AcceptanceResult
from horizon.tools.gateway import WorkspaceToolGateway

_FIXTURE_CONTENT = "def parse(value):\n    return [value]\n"
_FIXED_CONTENT = "def parse(value):\n    return [] if value == '' else [value]\n"
_OFFLINE_PRICE = PriceCard(
    currency="CNY",
    input_per_million="3.00",
    cached_input_per_million="0.30",
    output_per_million="9.00",
    version="context-capacity-demo-simulated-v1",
    source_url="https://offline.invalid/horizon-context-capacity-demo",
)


class _ParserAcceptanceExecutor:
    """Inspect the fixture text without importing or executing repository code."""

    def execute(self, workspace: Path, check: AcceptanceCheck) -> AcceptanceResult:
        if check.id != "empty-input":
            raise ValueError(f"Unknown context-capacity demo check: {check.id}")
        passed = (workspace / "src" / "parser.py").read_text(encoding="utf-8") == _FIXED_CONTENT
        output = "empty input returns []" if passed else "empty input still returns ['']"
        return AcceptanceResult(
            check_id=check.id,
            passed=passed,
            exit_code=0 if passed else 1,
            timed_out=False,
            output=output,
            output_hash=_sha256(output.encode("utf-8")),
        )


@dataclass(frozen=True)
class ContextCapacityDemoResult:
    output_dir: Path
    evidence_pack_path: Path
    context_stop_path: Path
    blocked_session_path: Path
    resumed_session_path: Path
    terminal_evidence_pack_path: Path
    trace_path: Path
    summary_path: Path
    evidence_pack: ContextCapacityEvidencePack


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_new(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)


def _file_record(root: Path, role: str, path: Path) -> ContextCapacityEvidenceFile:
    content = path.read_bytes()
    return ContextCapacityEvidenceFile(
        role=role,
        path=path.relative_to(root).as_posix(),
        sha256=_sha256(content),
        bytes=len(content),
    )


def _only_event(events: tuple[Event, ...], event_type: str, predicate) -> Event:
    matches = tuple(
        event for event in events if event.event_type == event_type and predicate(event)
    )
    if len(matches) != 1:
        raise ValueError(f"Context-capacity Trace requires exactly one matching {event_type} event")
    return matches[0]


def _event_at(events: tuple[Event, ...], sequence: int) -> Event:
    if not 1 <= sequence <= len(events):
        raise ValueError("Context-capacity event sequence is outside the Trace")
    event = events[sequence - 1]
    if event.seq != sequence:
        raise ValueError("Context-capacity Trace sequence is not contiguous")
    return event


def _session_matches_record(
    session: AgentSession,
    artifact_ref: str,
    record: dict,
) -> bool:
    return record == {
        "artifact_ref": artifact_ref,
        "task_spec_hash": session.task_spec_hash,
        "plan_version": session.plan_version,
        "work_item_id": session.work_item_id,
        "next_iteration": session.next_iteration,
        "covered_event_seq": session.covered_event_seq,
        "workspace_revision": session.workspace_revision,
        "message_count": len(session.messages),
    }


def _derive_evidence(
    terminal_pack_path: Path,
    stop_content: bytes,
    blocked_session_content: bytes,
    resumed_session_content: bytes,
) -> ContextCapacityDemoEvidence:
    terminal_pack = verify_terminal_evidence_pack(terminal_pack_path)
    terminal_root = terminal_pack_path.parent
    trace_record = next(item for item in terminal_pack.files if item.role == "trace")
    trace_path = (terminal_root / trace_record.path).resolve(strict=True)
    trace_text = trace_path.read_text(encoding="utf-8")
    events = tuple(Event.model_validate_json(line) for line in trace_text.splitlines() if line)
    replayed = SQLiteEventStore.replay_jsonl(trace_text)
    if replayed.status != RunStatus.SUCCEEDED:
        raise ValueError("Context-capacity demo Trace must end in SUCCEEDED")

    request_event = _only_event(
        events,
        "HUMAN_REQUEST_CREATED",
        lambda event: event.payload.get("request", {}).get("kind") == "context_capacity_required",
    )
    request = HumanContextRequest.model_validate(request_event.payload["request"])
    decision_event = _only_event(
        events,
        "HUMAN_DECISION_RECORDED",
        lambda event: (
            event.payload.get("decision", {}).get("kind") == "context_capacity_retry_authorized"
        ),
    )
    decision = HumanContextDecision.model_validate(decision_event.payload["decision"])
    resumed_event = _only_event(
        events,
        "AGENT_SESSION_CONTEXT_RESUMED",
        lambda event: (
            event.payload.get("session", {}).get("artifact_ref")
            == decision.resumed_session_artifact_ref
        ),
    )
    blocked_session_event = _only_event(
        events,
        "AGENT_SESSION_SAVED",
        lambda event: (
            event.payload.get("session", {}).get("artifact_ref")
            == request.agent_session_artifact_ref
        ),
    )

    waiting_event = _event_at(events, request_event.seq + 1)
    released_event = _event_at(events, request_event.seq + 2)
    if (
        waiting_event.event_type != "STATE_CHANGED"
        or waiting_event.payload != {"from": "RUNNING", "to": "WAITING_FOR_USER"}
        or released_event.event_type != "LEASE_RELEASED"
    ):
        raise ValueError("Context-capacity stop does not have the durable wait/release sequence")

    recovery_lease = _only_event(
        events,
        "LEASE_ACQUIRED",
        lambda event: event.seq > released_event.seq,
    )
    if decision_event.seq != recovery_lease.seq + 1:
        raise ValueError("Context-capacity decision must immediately follow its recovery lease")
    running_event = _event_at(events, decision_event.seq + 1)
    if (
        running_event.event_type != "STATE_CHANGED"
        or running_event.payload != {"from": "WAITING_FOR_USER", "to": "RUNNING"}
        or resumed_event.seq != running_event.seq + 1
    ):
        raise ValueError(
            "Context-capacity decision does not publish the resumed session atomically"
        )

    model_reservations = tuple(
        event for event in events if event.event_type == "MODEL_CALL_RESERVED"
    )
    if not model_reservations:
        raise ValueError("Context-capacity demo never resumed model execution")
    first_model_reservation = model_reservations[0]
    if first_model_reservation.seq <= resumed_event.seq:
        raise ValueError("Context-capacity demo dispatched a model call before session resume")
    if any(
        event.event_type in {"MODEL_CALL_RESERVED", "MODEL_CALL_SETTLED", "MODEL_CALL_UNKNOWN"}
        and event.seq < resumed_event.seq
        for event in events
    ):
        raise ValueError("Blocked context request reached model dispatch before resume")
    if any(
        event.event_type in {"TOOL_CALL_RESERVED", "TOOL_CALL_SETTLED", "TOOL_CALL_UNKNOWN"}
        and event.seq < resumed_event.seq
        for event in events
    ):
        raise ValueError("Blocked context request reached tool dispatch before resume")

    stop = ContextCapacityStop.model_validate_json(stop_content)
    blocked_session = AgentSession.model_validate_json(blocked_session_content)
    resumed_session = AgentSession.model_validate_json(resumed_session_content)
    if stop != request.stop or stop.reason_code != ContextStopReason.PROTECTED_PREFIX_LIMIT:
        raise ValueError("Context stop artifact does not match the protected-prefix request")
    if (
        _sha256(stop_content) != request.stop_artifact_ref
        or request.stop_artifact_ref != stop.sha256
    ):
        raise ValueError("Context stop artifact is not bound to its request hash")
    if (
        _sha256(blocked_session_content) != request.agent_session_artifact_ref
        or not _session_matches_record(
            blocked_session,
            request.agent_session_artifact_ref,
            blocked_session_event.payload["session"],
        )
        or blocked_session.covered_event_seq != blocked_session_event.seq - 1
        or blocked_session.run_id != replayed.run_id
        or blocked_session.task_spec_hash != request.task_spec_hash
        or blocked_session.plan_version != request.plan_version
        or blocked_session.work_item_id != request.work_item_id
        or blocked_session.workspace_revision != request.workspace_revision
        or blocked_session.next_iteration != request.next_iteration
    ):
        raise ValueError("Blocked Agent session does not match the context request")
    if (
        _sha256(resumed_session_content) != decision.resumed_session_artifact_ref
        or not _session_matches_record(
            resumed_session,
            decision.resumed_session_artifact_ref,
            resumed_event.payload["session"],
        )
        or resumed_session.run_id != blocked_session.run_id
        or resumed_session.task_spec_hash != blocked_session.task_spec_hash
        or resumed_session.plan_version != blocked_session.plan_version
        or resumed_session.work_item_id != blocked_session.work_item_id
        or resumed_session.workspace_revision != blocked_session.workspace_revision
        or resumed_session.next_iteration != blocked_session.next_iteration
    ):
        raise ValueError("Resumed Agent session does not match its decision and Trace record")
    if resumed_session.messages != blocked_session.messages:
        raise ValueError("Context-capacity resume changed the canonical Agent transcript")
    source_digest = digest(
        [message.model_dump(mode="json") for message in blocked_session.messages]
    )
    if source_digest != stop.source_digest or stop.source_message_count != len(
        blocked_session.messages
    ):
        raise ValueError("Context stop source digest does not match the blocked transcript")
    if resumed_session.covered_event_seq != resumed_event.seq - 1:
        raise ValueError("Resumed Agent session does not bind the decision event boundary")

    blocked_epoch = released_event.payload.get("epoch")
    recovery_epoch = recovery_lease.payload.get("epoch")
    if not isinstance(blocked_epoch, int) or not isinstance(recovery_epoch, int):
        raise ValueError("Context-capacity lease events omitted their epochs")
    if recovery_epoch != blocked_epoch + 1:
        raise ValueError("Context-capacity recovery did not use the next lease epoch")
    if decision.request_id != request.request_id:
        raise ValueError("Context-capacity decision does not consume the pending request")
    if (
        decision.max_context_chars < stop.required_context_chars
        or decision.max_input_tokens < stop.required_input_tokens
    ):
        raise ValueError("Context-capacity decision did not authorize sufficient limits")

    required_ids = {item.id for item in replayed.task.acceptance if item.required}
    required_validation_passed = replayed.validation is not None and required_ids <= set(
        replayed.validation["passed_check_ids"]
    )
    if not required_validation_passed:
        raise ValueError("Context-capacity demo did not finish protected validation")
    if len(replayed.model_calls) != 2 or len(replayed.tool_calls) != 3:
        raise ValueError("Context-capacity demo changed its frozen two-call execution story")
    if (
        terminal_pack.evidence.unknown_effects_present
        or terminal_pack.evidence.open_effects_present
    ):
        raise ValueError("Context-capacity demo ended with unknown or open effects")

    return ContextCapacityDemoEvidence(
        run_id=replayed.run_id,
        task_id=replayed.task.task_id,
        request_id=request.request_id,
        stop_artifact_ref=request.stop_artifact_ref,
        blocked_session_artifact_ref=request.agent_session_artifact_ref,
        resumed_session_artifact_ref=decision.resumed_session_artifact_ref,
        source_digest=source_digest,
        configured_context_chars=stop.max_context_chars,
        required_context_chars=stop.required_context_chars,
        configured_input_tokens=stop.max_input_tokens,
        required_input_tokens=stop.required_input_tokens,
        blocked_message_count=len(blocked_session.messages),
        resumed_message_count=len(resumed_session.messages),
        blocked_session_event_seq=blocked_session_event.seq,
        resumed_session_covered_event_seq=resumed_session.covered_event_seq,
        blocked_lease_epoch=blocked_epoch,
        recovery_lease_epoch=recovery_epoch,
        request_event_seq=request_event.seq,
        waiting_state_event_seq=waiting_event.seq,
        blocked_lease_release_event_seq=released_event.seq,
        recovery_lease_event_seq=recovery_lease.seq,
        decision_event_seq=decision_event.seq,
        running_state_event_seq=running_event.seq,
        session_resumed_event_seq=resumed_event.seq,
        first_model_reservation_event_seq=first_model_reservation.seq,
        event_count=replayed.seq,
        event_hash=replayed.event_hash,
        projection_hash=projection_hash(replayed),
        final_model_call_count=len(replayed.model_calls),
        final_tool_call_count=len(replayed.tool_calls),
    )


def _summary(evidence: ContextCapacityDemoEvidence) -> str:
    boundaries = "\n".join(f"- `{item}`" for item in evidence.boundaries)
    return (
        "# Horizon Context-Capacity Recovery Evidence\n\n"
        f"- Run: `{evidence.run_id}`\n"
        f"- Terminal status: `{evidence.terminal_status}`\n"
        f"- Stop reason: `{evidence.stop_reason}`\n"
        f"- Character limit: `{evidence.configured_context_chars}` -> required "
        f"`{evidence.required_context_chars}`\n"
        f"- Input-token limit: `{evidence.configured_input_tokens}` -> required "
        f"`{evidence.required_input_tokens}`\n"
        f"- Lease epoch: `{evidence.blocked_lease_epoch}` -> "
        f"`{evidence.recovery_lease_epoch}`\n"
        f"- Transcript preserved: `{str(evidence.transcript_preserved).lower()}`\n"
        f"- Provider dispatch before resume: "
        f"`{str(evidence.provider_dispatch_before_resume).lower()}`\n"
        f"- Final model/tool calls: `{evidence.final_model_call_count}` / "
        f"`{evidence.final_tool_call_count}`\n"
        f"- Event count: `{evidence.event_count}`\n"
        f"- Event hash: `{evidence.event_hash}`\n"
        f"- Projection hash: `{evidence.projection_hash}`\n"
        "- External model cost: `CNY 0`\n\n"
        "The verifier recursively validates the terminal EvidencePack, replays the full Trace, "
        "checks the durable wait/release/decision/resume order, and compares the content-addressed "
        "stop plus both Agent sessions.\n\n"
        "## Claim boundaries\n\n"
        f"{boundaries}\n"
    )


class ContextCapacityDemoRunner:
    def run(self, output_dir: Path) -> ContextCapacityDemoResult:
        destination = output_dir.resolve()
        if destination.exists():
            raise FileExistsError(
                f"Context-capacity evidence directory already exists: {destination}"
            )

        with tempfile.TemporaryDirectory(prefix="horizon-context-capacity-") as temporary:
            runtime = Path(temporary)
            workspace = runtime / "workspace"
            (workspace / "src").mkdir(parents=True)
            (workspace / "src" / "parser.py").write_text(
                _FIXTURE_CONTENT,
                encoding="utf-8",
                newline="\n",
            )

            artifacts_path = runtime / "artifacts"
            store_path = runtime / "control.sqlite3"
            campaign_path = runtime / "campaign.sqlite3"
            artifacts = ArtifactStore(artifacts_path)
            snapshots = SnapshotManager(artifacts)
            initial_snapshot, _ = snapshots.capture(
                workspace,
                allowed_paths=("src/**",),
                denied_paths=(".git/**", ".env", ".env.*", "secrets/**"),
            )
            demo_id = f"context-capacity-{uuid4().hex[-12:]}"
            check = AcceptanceCheck(
                id="empty-input",
                command="context-capacity-demo:inspect-empty-input",
                timeout_seconds=30,
                required=True,
            )
            task = TaskSpec(
                task_id=demo_id,
                title="Resume an oversized protected context without dropping its transcript",
                objective=(
                    "Preserve this exact long-horizon task contract, pause before provider "
                    "dispatch when it cannot fit, then resume after a sufficient local capacity "
                    "decision and repair empty-input parsing. Evidence payload: " + "x" * 5_000
                ),
                repository=Repository(
                    source="local",
                    path=str(workspace),
                    base_commit=initial_snapshot.workspace_revision,
                ),
                constraints=Constraints(
                    allowed_paths=("src/**",),
                    denied_paths=(".git/**", ".env", ".env.*", "secrets/**"),
                    allowed_tools=("replace_text", "run_check"),
                    network="deny",
                    requirements=(
                        "Do not drop or rewrite the canonical Agent transcript.",
                        "Do not dispatch a model request while the context stop is unresolved.",
                    ),
                ),
                acceptance=(check,),
                budgets=BudgetSpec(
                    max_steps=5,
                    max_model_calls=4,
                    max_tool_calls=5,
                    max_wall_time_seconds=120,
                    max_cost_usd="1.00",
                    max_input_tokens=50_000,
                    max_output_tokens=1_024,
                    max_repair_cycles=1,
                ),
                task_kind="bugfix",
                execution_mode="workspace_write",
                authority_scope="workspace_write",
                model_policy_id="offline-scripted-context-capacity-demo",
                memory_scope=f"context-capacity-{demo_id[-12:]}",
            )
            plan = Plan(
                items=(
                    WorkItem(
                        work_item_id="repair-parser",
                        title="Repair empty-input parsing",
                        objective="Return [] for an empty input without changing the public API.",
                        expected_artifacts=("src/parser.py", "protected validation evidence"),
                        acceptance_ids=(check.id,),
                        allowed_tools=("replace_text", "run_check"),
                    ),
                )
            )
            plan.check_task(task)
            actions = (
                ScriptedModelAction(
                    tool="replace_text",
                    arguments={
                        "path": "src/parser.py",
                        "old": "return [value]",
                        "new": "return [] if value == '' else [value]",
                    },
                ),
                ScriptedModelAction(
                    tool="submit",
                    arguments={"summary": "Repaired and verified after context-capacity resume."},
                ),
            )
            campaign = CampaignBudget(
                campaign_id=f"context-capacity-{demo_id[-12:]}",
                currency="CNY",
                max_cost="10.00",
                max_cost_per_call="1.00",
            )
            checker = _ParserAcceptanceExecutor()

            store = SQLiteEventStore(store_path)
            service = HarnessService(store)
            run = store.create(task, "create")
            run = service.set_plan(run.run_id, plan, "plan")
            run = service.acquire_lease(
                run.run_id,
                "context-worker-1",
                "lease-1",
                ttl_seconds=120,
            )
            token = LeaseToken.from_run(run)
            service.transition(run.run_id, RunStatus.RUNNING, token, "start")
            initial_model = ScriptedModelGateway("context-capacity-blocked", actions)
            initial_tools = WorkspaceToolGateway(
                workspace,
                task,
                plan.items[0],
                snapshots,
                checker,
            )
            initial_ledger = CampaignBudgetLedger(campaign_path)
            initial_runner = CodingAgentRunner(
                service,
                initial_model,
                initial_ledger,
                initial_tools,
                artifacts,
                provider_id="offline-scripted",
                model_id="scripted-context-capacity-v1",
                pricing=_OFFLINE_PRICE,
                campaign=campaign,
                config=AgentLoopConfig(
                    max_model_iterations=4,
                    max_output_tokens=256,
                    max_context_chars=2_000,
                    max_input_tokens=1_000_000,
                    max_run_cost=Decimal("10.00"),
                ),
            )
            waiting = initial_runner.run(run.run_id, token)
            if (
                waiting.status != RunStatus.WAITING_FOR_USER
                or not isinstance(waiting.pending_human_request, HumanContextRequest)
                or initial_model.requests
                or waiting.model_calls
                or waiting.tool_calls
                or initial_ledger.attempts(campaign.campaign_id)
            ):
                raise ValueError("Context-capacity demo did not stop before model/tool dispatch")
            request = waiting.pending_human_request
            stop_content = artifacts.read(request.stop_artifact_ref)
            blocked_session_content = artifacts.read(request.agent_session_artifact_ref)

            # Reopen every persistent adapter before applying the trusted local decision. The
            # Trace proves the new lease epoch; the output pack no longer depends on these files.
            store = SQLiteEventStore(store_path)
            service = HarnessService(store)
            artifacts = ArtifactStore(artifacts_path)
            snapshots = SnapshotManager(artifacts)
            resumed_run = service.acquire_lease(
                run.run_id,
                "context-worker-2",
                "lease-2-after-capacity-stop",
                ttl_seconds=120,
            )
            resumed_token = LeaseToken.from_run(resumed_run)
            max_context_chars = max(60_000, request.stop.required_context_chars)
            max_input_tokens = max(120_000, request.stop.required_input_tokens)
            resumed = ContextCapacityResumeService(service, artifacts).apply(
                run.run_id,
                resumed_token,
                "authorize-context-capacity-retry",
                max_context_chars=max_context_chars,
                max_input_tokens=max_input_tokens,
            )
            resumed_session_content = artifacts.read(resumed.session_artifact_ref)
            final_model = ScriptedModelGateway("context-capacity-resumed", actions)
            final_tools = WorkspaceToolGateway(
                workspace,
                task,
                plan.items[0],
                snapshots,
                checker,
            )
            final_runner = CodingAgentRunner(
                service,
                final_model,
                CampaignBudgetLedger(campaign_path),
                final_tools,
                artifacts,
                provider_id="offline-scripted",
                model_id="scripted-context-capacity-v1",
                pricing=_OFFLINE_PRICE,
                campaign=campaign,
                config=AgentLoopConfig(
                    max_model_iterations=4,
                    max_output_tokens=256,
                    max_context_chars=max_context_chars,
                    max_input_tokens=max_input_tokens,
                    max_run_cost=Decimal("10.00"),
                ),
            )
            result = final_runner.run(run.run_id, resumed_token)
            if result.status != RunStatus.SUCCEEDED or not final_model.consumed:
                raise ValueError("Context-capacity demo did not finish its resumed execution")

            destination.mkdir(parents=True, exist_ok=False)
            context_stop_path = destination / "context-stop.json"
            blocked_session_path = destination / "blocked-agent-session.json"
            resumed_session_path = destination / "resumed-agent-session.json"
            summary_path = destination / "SUMMARY.md"
            evidence_pack_path = destination / "evidence-pack.json"
            _write_new(context_stop_path, stop_content)
            _write_new(blocked_session_path, blocked_session_content)
            _write_new(resumed_session_path, resumed_session_content)
            terminal_result: TerminalEvidenceResult = export_terminal_evidence_pack(
                store,
                run.run_id,
                destination / "terminal",
            )
            evidence = _derive_evidence(
                terminal_result.evidence_pack_path,
                stop_content,
                blocked_session_content,
                resumed_session_content,
            )
            _write_new(summary_path, _summary(evidence).encode("utf-8"))
            pack = ContextCapacityEvidencePack(
                evidence=evidence,
                files=(
                    _file_record(destination, "context_stop", context_stop_path),
                    _file_record(destination, "blocked_session", blocked_session_path),
                    _file_record(destination, "resumed_session", resumed_session_path),
                    _file_record(
                        destination,
                        "terminal_pack",
                        terminal_result.evidence_pack_path,
                    ),
                    _file_record(destination, "summary", summary_path),
                ),
            )
            _write_new(evidence_pack_path, (canonical_json(pack) + "\n").encode("utf-8"))
            verified = verify_context_capacity_evidence_pack(evidence_pack_path)
            return ContextCapacityDemoResult(
                output_dir=destination,
                evidence_pack_path=evidence_pack_path,
                context_stop_path=context_stop_path,
                blocked_session_path=blocked_session_path,
                resumed_session_path=resumed_session_path,
                terminal_evidence_pack_path=terminal_result.evidence_pack_path,
                trace_path=terminal_result.trace_path,
                summary_path=summary_path,
                evidence_pack=verified,
            )


def verify_context_capacity_evidence_pack(path: Path) -> ContextCapacityEvidencePack:
    pack_path = path.resolve(strict=True)
    root = pack_path.parent
    pack = ContextCapacityEvidencePack.model_validate_json(pack_path.read_text(encoding="utf-8"))
    contents: dict[str, bytes] = {}
    paths: dict[str, Path] = {}
    for item in pack.files:
        candidate = (root / item.path).resolve(strict=True)
        if not candidate.is_relative_to(root):
            raise ValueError("Context-capacity evidence file escapes its pack directory")
        content = candidate.read_bytes()
        if len(content) != item.bytes or _sha256(content) != item.sha256:
            raise ValueError(
                f"Context-capacity evidence file failed integrity verification: {item.path}"
            )
        contents[item.role] = content
        paths[item.role] = candidate

    evidence = _derive_evidence(
        paths["terminal_pack"],
        contents["context_stop"],
        contents["blocked_session"],
        contents["resumed_session"],
    )
    if pack.evidence != evidence:
        raise ValueError("Context-capacity EvidencePack metadata does not match its Trace")
    if contents["summary"] != _summary(evidence).encode("utf-8"):
        raise ValueError("Context-capacity summary does not match replayed evidence")
    return pack
