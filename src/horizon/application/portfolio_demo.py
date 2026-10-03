from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from horizon.adapters.model.scripted import ScriptedModelGateway
from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.adapters.persistence.campaign_budget import CampaignBudgetLedger
from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.adapters.retrieval.sqlite_fts import SQLiteCodeRetriever
from horizon.adapters.workspace.promotion import workspace_path_hash
from horizon.adapters.workspace.snapshot import SnapshotManager
from horizon.application.agent_loop import AgentLoopConfig, CodingAgentRunner
from horizon.application.services import HarnessService, LeaseToken
from horizon.domain.common import canonical_json
from horizon.domain.model import CampaignBudget, PriceCard
from horizon.domain.plan import Plan, WorkItem
from horizon.domain.portfolio_demo import (
    PORTFOLIO_DEMO_EXCLUDED_CLAIMS,
    PortfolioDemoReport,
    PortfolioDemoVerification,
    PortfolioEvidenceFile,
    PortfolioEvidencePack,
)
from horizon.domain.promotion import WorkspaceOrigin
from horizon.domain.run import projection_hash
from horizon.domain.run_evaluation import ScriptedModelAction
from horizon.domain.states import RunStatus
from horizon.domain.task import (
    AcceptanceCheck,
    BudgetSpec,
    Constraints,
    Repository,
    TaskSpec,
)
from horizon.domain.tools import AcceptanceResult
from horizon.tools.gateway import WorkspaceToolGateway

_FIXTURE_CONTENT = "def parse(value):\n    return [value]\n"
_FIXED_CONTENT = "def parse(value):\n    return [] if value == '' else [value]\n"
_STRUCTURED_RANGE_ERROR = "start_line and end_line must be supplied together"
_OFFLINE_PRICE = PriceCard(
    currency="CNY",
    input_per_million="3.00",
    cached_input_per_million="0.30",
    output_per_million="9.00",
    version="portfolio-demo-simulated-v1",
    source_url="https://offline.invalid/horizon-portfolio-demo",
)


class _ParserAcceptanceExecutor:
    """Deterministic demo check; it inspects text and never executes repository code."""

    def execute(self, workspace: Path, check: AcceptanceCheck) -> AcceptanceResult:
        if check.id != "empty-input":
            raise ValueError(f"Unknown portfolio demo check: {check.id}")
        content = (workspace / "src" / "parser.py").read_text(encoding="utf-8")
        passed = content == _FIXED_CONTENT
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
class PortfolioDemoResult:
    output_dir: Path
    evidence_pack_path: Path
    report_path: Path
    trace_path: Path
    final_run_path: Path
    summary_path: Path
    workspace: Path
    report: PortfolioDemoReport
    evidence_pack: PortfolioEvidencePack


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_new(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)


def _file_record(root: Path, role: str, path: Path) -> PortfolioEvidenceFile:
    content = path.read_bytes()
    return PortfolioEvidenceFile(
        role=role,
        path=path.relative_to(root).as_posix(),
        sha256=_sha256(content),
        bytes=len(content),
    )


def verify_portfolio_evidence_pack(path: Path) -> PortfolioEvidencePack:
    """Verify exported files and replay the Trace without trusting the summary JSON."""

    path = path.resolve(strict=True)
    root = path.parent
    pack = PortfolioEvidencePack.model_validate_json(path.read_text(encoding="utf-8"))
    by_role = {item.role: item for item in pack.files}
    contents: dict[str, bytes] = {}
    for item in pack.files:
        candidate = (root / item.path).resolve(strict=True)
        if not candidate.is_relative_to(root):
            raise ValueError("Portfolio evidence file escapes its pack directory")
        content = candidate.read_bytes()
        if len(content) != item.bytes or _sha256(content) != item.sha256:
            raise ValueError(f"Portfolio evidence file failed integrity verification: {item.path}")
        contents[item.role] = content

    report = PortfolioDemoReport.model_validate_json(contents["report"])
    if (
        report.demo_id != pack.demo_id
        or report.run_id != pack.run_id
        or report.verification.all_checks_passed != pack.all_checks_passed
        or report.excluded_claims != pack.excluded_claims
    ):
        raise ValueError("Portfolio EvidencePack metadata does not match its report")
    if report.trace_sha256 != by_role["trace"].sha256:
        raise ValueError("Portfolio report Trace hash does not match its EvidencePack")
    if report.final_run_sha256 != by_role["final_state"].sha256:
        raise ValueError("Portfolio report final-state hash does not match its EvidencePack")

    replayed = SQLiteEventStore.replay_jsonl(contents["trace"].decode("utf-8"))
    if projection_hash(replayed) != report.projection_hash:
        raise ValueError("Portfolio Trace replay projection does not match its report")
    expected_final = (canonical_json(replayed.as_dict()) + "\n").encode("utf-8")
    if contents["final_state"] != expected_final:
        raise ValueError("Portfolio final-state export does not match Trace replay")
    return pack


class PortfolioDemoRunner:
    """Run one fixed, offline Harness story and export a self-verifying evidence directory."""

    def run(self, output_dir: Path) -> PortfolioDemoResult:
        output_dir = output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=False)
        demo_id = f"demo-{uuid4().hex}"
        source = output_dir / "source"
        workspace = output_dir / "workspace"
        (source / "src").mkdir(parents=True)
        _write_new(source / "src" / "parser.py", _FIXTURE_CONTENT.encode("utf-8"))

        artifacts = ArtifactStore(output_dir / "artifacts")
        snapshots = SnapshotManager(artifacts)
        source_snapshot, source_manifest_ref = snapshots.capture(
            source,
            allowed_paths=("src/**",),
            denied_paths=(".git/**", ".env", ".env.*", "secrets/**"),
        )
        snapshots.restore(source_manifest_ref, workspace)

        check = AcceptanceCheck(
            id="empty-input",
            command="portfolio-demo:inspect-empty-input",
            timeout_seconds=30,
            required=True,
        )
        checker = _ParserAcceptanceExecutor()
        initial_validation = (checker.execute(source, check),)
        task = TaskSpec(
            task_id=f"horizon-portfolio-{demo_id[-12:]}",
            title="Recover from a bounded file-read error and repair an empty-input bug",
            objective=(
                "Use repository evidence, recover from an invalid one-sided line range, "
                "and make parse('') return an empty list."
            ),
            repository=Repository(
                source="local",
                path=str(workspace),
                base_commit=source_snapshot.workspace_revision,
            ),
            constraints=Constraints(
                allowed_paths=("src/**",),
                denied_paths=(".git/**", ".env", ".env.*", "secrets/**"),
                network="deny",
                requirements=(
                    "Use repository evidence before editing.",
                    "Do not expand the write scope beyond src/**.",
                ),
            ),
            acceptance=(check,),
            budgets=BudgetSpec(
                max_steps=12,
                max_model_calls=8,
                max_tool_calls=12,
                max_wall_time_seconds=120,
                max_cost_usd="1.00",
                max_input_tokens=50_000,
                max_output_tokens=2_048,
                max_repair_cycles=1,
            ),
            task_kind="bugfix",
            execution_mode="workspace_write",
            authority_scope="workspace_write",
            model_policy_id="offline-scripted-portfolio-demo",
            memory_scope=f"portfolio-{demo_id[-12:]}",
        )
        plan = Plan(
            items=(
                WorkItem(
                    work_item_id="repair-parser",
                    title="Repair empty-input parsing",
                    objective="Return [] for an empty input without changing the public API.",
                    expected_artifacts=("src/parser.py", "protected validation evidence"),
                    acceptance_ids=("empty-input",),
                    allowed_tools=(
                        "search_repo",
                        "read_file",
                        "retrieve_code",
                        "replace_text",
                        "run_check",
                    ),
                ),
            )
        )
        plan.check_task(task)

        actions = (
            ScriptedModelAction(tool="retrieve_code", arguments={"query": "parse"}),
            ScriptedModelAction(
                tool="read_file",
                arguments={"path": "src/parser.py", "start_line": 1},
            ),
            ScriptedModelAction(
                tool="read_file",
                arguments={"path": "src/parser.py", "start_line": 1, "end_line": 2},
            ),
            ScriptedModelAction(
                tool="replace_text",
                arguments={
                    "path": "src/parser.py",
                    "old": "return [value]",
                    "new": "return [] if value == '' else [value]",
                },
            ),
            ScriptedModelAction(tool="run_check", arguments={"check_id": "empty-input"}),
            ScriptedModelAction(
                tool="submit",
                arguments={"summary": "Recovered from the range error and verified the repair."},
            ),
        )
        store_path = output_dir / "control.sqlite3"
        campaign_path = output_dir / "campaign.sqlite3"
        retrieval_path = output_dir / "retrieval.sqlite3"
        store = SQLiteEventStore(store_path)
        service = HarnessService(store)
        run = store.create(task, "create")
        run = service.bind_workspace_origin(
            run.run_id,
            WorkspaceOrigin(
                source_path_hash=workspace_path_hash(source),
                source_revision=source_snapshot.workspace_revision,
                source_manifest_ref=source_manifest_ref,
            ),
            "workspace-origin",
        )
        run = service.set_plan(run.run_id, plan, "plan")
        run = service.acquire_lease(run.run_id, "portfolio-worker-1", "lease-1", ttl_seconds=120)
        token = LeaseToken.from_run(run)
        service.transition(run.run_id, RunStatus.RUNNING, token, "start")

        campaign = CampaignBudget(
            campaign_id=f"portfolio-{demo_id[-12:]}",
            currency="CNY",
            max_cost="10.00",
            max_cost_per_call="1.00",
        )
        config = AgentLoopConfig(
            max_model_iterations=len(actions),
            max_output_tokens=256,
            max_run_cost=Decimal("10.00"),
        )

        def build_runner(
            active_service: HarnessService,
            model: ScriptedModelGateway,
            active_artifacts: ArtifactStore,
            active_snapshots: SnapshotManager,
        ) -> CodingAgentRunner:
            tools = WorkspaceToolGateway(
                workspace,
                task,
                plan.items[0],
                active_snapshots,
                checker,
                SQLiteCodeRetriever(retrieval_path, active_snapshots),
            )
            return CodingAgentRunner(
                active_service,
                model,
                CampaignBudgetLedger(campaign_path),
                tools,
                active_artifacts,
                provider_id="offline-scripted",
                model_id="scripted-portfolio-v1",
                pricing=_OFFLINE_PRICE,
                campaign=campaign,
                config=config,
            )

        first_model = ScriptedModelGateway("portfolio", actions[:2])
        partial = build_runner(service, first_model, artifacts, snapshots).run(
            run.run_id,
            token,
            max_iterations_this_invocation=2,
        )
        if partial.status != RunStatus.RUNNING or partial.agent_session is None:
            raise ValueError("Portfolio demo did not reach its durable worker handoff boundary")
        service.release_lease(run.run_id, token, "worker-1-handoff")

        # Reopen every worker-owned adapter before continuing from the durable session.
        store = SQLiteEventStore(store_path)
        service = HarnessService(store)
        artifacts = ArtifactStore(output_dir / "artifacts")
        snapshots = SnapshotManager(artifacts)
        leased = service.acquire_lease(
            run.run_id,
            "portfolio-worker-2",
            "lease-2",
            ttl_seconds=120,
        )
        token = LeaseToken.from_run(leased)
        second_model = ScriptedModelGateway("portfolio", actions[2:], start_index=2)
        result = build_runner(service, second_model, artifacts, snapshots).run(run.run_id, token)

        trace_content = store.export_jsonl(run.run_id).encode("utf-8")
        replayed = SQLiteEventStore.replay_jsonl(trace_content.decode("utf-8"))
        trace_replay_verified = replayed.as_dict() == result.as_dict() and projection_hash(
            replayed
        ) == projection_hash(result)
        source_after, _ = snapshots.capture(
            source,
            allowed_paths=task.constraints.allowed_paths,
            denied_paths=task.constraints.denied_paths,
        )
        workspace_after, workspace_manifest_ref = snapshots.capture(
            workspace,
            allowed_paths=task.constraints.allowed_paths,
            denied_paths=task.constraints.denied_paths,
        )
        final_validation = tuple(checker.execute(workspace, item) for item in task.acceptance)

        error_records = [
            record
            for record in result.tool_calls
            if record.name == "read_file" and record.status == "error"
        ]
        if len(error_records) != 1 or error_records[0].artifact_ref is None:
            raise ValueError("Portfolio demo requires one content-addressed read error")
        error_content = artifacts.read(error_records[0].artifact_ref).decode("utf-8")
        resumed_context_contains_error = bool(second_model.requests) and any(
            message.role == "tool" and _STRUCTURED_RANGE_ERROR in (message.content or "")
            for message in second_model.requests[0].messages
        )
        required_ids = {item.id for item in task.acceptance if item.required}
        run_validation_passed = result.validation is not None and required_ids <= set(
            result.validation["passed_check_ids"]
        )
        verification_values = {
            "initial_failure_confirmed": all(not item.passed for item in initial_validation),
            "structured_tool_error_observed": _STRUCTURED_RANGE_ERROR in error_content,
            "resumed_context_contains_error": resumed_context_contains_error,
            "final_validation_passed": (
                run_validation_passed and all(item.passed for item in final_validation)
            ),
            "trace_replay_verified": trace_replay_verified,
            "source_workspace_unchanged": (
                source_after.workspace_revision == source_snapshot.workspace_revision
            ),
            "staging_workspace_changed": (
                workspace_after.workspace_revision != source_snapshot.workspace_revision
            ),
            "scripted_actions_consumed": first_model.consumed and second_model.consumed,
            "no_unknown_calls": (not result.unknown_model_calls and not result.unknown_tool_calls),
            "no_open_reservations": (
                not result.reservations
                and not result.model_reservations
                and not result.tool_reservations
            ),
        }
        verification = PortfolioDemoVerification(
            **verification_values,
            all_checks_passed=all(verification_values.values()),
        )

        trace_path = output_dir / "trace.jsonl"
        final_run_path = output_dir / "final-run.json"
        report_path = output_dir / "report.json"
        summary_path = output_dir / "SUMMARY.md"
        evidence_pack_path = output_dir / "evidence-pack.json"
        _write_new(trace_path, trace_content)
        final_run_content = (canonical_json(result.as_dict()) + "\n").encode("utf-8")
        _write_new(final_run_path, final_run_content)
        report = PortfolioDemoReport(
            demo_id=demo_id,
            run_id=result.run_id,
            status=result.status,
            final_lease_epoch=result.lease_epoch,
            event_count=result.seq,
            tool_sequence=tuple(record.name for record in result.tool_calls),
            tool_statuses=tuple(record.status for record in result.tool_calls),
            initial_validation=initial_validation,
            final_validation=final_validation,
            usage=result.usage,
            simulated_model_cost=result.model_occupied_cost,
            model_currency=_OFFLINE_PRICE.currency,
            projection_hash=projection_hash(result),
            trace_sha256=_sha256(trace_content),
            final_run_sha256=_sha256(final_run_content),
            source_revision=source_snapshot.workspace_revision,
            source_manifest_ref=source_manifest_ref,
            workspace_revision=workspace_after.workspace_revision,
            workspace_manifest_ref=workspace_manifest_ref,
            structured_error_artifact_ref=error_records[0].artifact_ref,
            verification=verification,
        )
        _write_new(report_path, (canonical_json(report) + "\n").encode("utf-8"))
        summary = self._summary(report)
        _write_new(summary_path, summary.encode("utf-8"))
        pack = PortfolioEvidencePack(
            demo_id=demo_id,
            run_id=result.run_id,
            files=(
                _file_record(output_dir, "report", report_path),
                _file_record(output_dir, "trace", trace_path),
                _file_record(output_dir, "final_state", final_run_path),
                _file_record(output_dir, "summary", summary_path),
            ),
            all_checks_passed=verification.all_checks_passed,
        )
        _write_new(evidence_pack_path, (canonical_json(pack) + "\n").encode("utf-8"))
        verified_pack = verify_portfolio_evidence_pack(evidence_pack_path)
        return PortfolioDemoResult(
            output_dir=output_dir,
            evidence_pack_path=evidence_pack_path,
            report_path=report_path,
            trace_path=trace_path,
            final_run_path=final_run_path,
            summary_path=summary_path,
            workspace=workspace,
            report=report,
            evidence_pack=verified_pack,
        )

    @staticmethod
    def _summary(report: PortfolioDemoReport) -> str:
        checks = report.verification
        exclusions = "\n".join(f"- `{item}`" for item in PORTFOLIO_DEMO_EXCLUDED_CLAIMS)
        return (
            "# Horizon Portfolio Demo Evidence\n\n"
            f"- Outcome: `{report.status.value}`\n"
            f"- Run: `{report.run_id}`\n"
            f"- Durable worker handoffs: `{report.worker_handoffs}`\n"
            f"- Final lease epoch: `{report.final_lease_epoch}`\n"
            f"- Events: `{report.event_count}`\n"
            f"- Model/tool calls: `{report.usage.model_calls}` / "
            f"`{report.usage.tool_calls}`\n"
            f"- External model cost: `CNY {report.external_cost_cny}`\n"
            f"- Simulated accounting cost: `CNY {report.simulated_model_cost}`\n"
            f"- Trace projection: `{report.projection_hash}`\n\n"
            "## Verified path\n\n"
            f"- Initial failure confirmed: `{checks.initial_failure_confirmed}`\n"
            f"- Structured tool error observed: `{checks.structured_tool_error_observed}`\n"
            f"- Second worker received the persisted error: "
            f"`{checks.resumed_context_contains_error}`\n"
            f"- Protected final validation passed: `{checks.final_validation_passed}`\n"
            f"- Trace replay matched final state: `{checks.trace_replay_verified}`\n"
            f"- Original source stayed unchanged: `{checks.source_workspace_unchanged}`\n"
            f"- All evidence checks passed: `{checks.all_checks_passed}`\n\n"
            "## Explicitly excluded claims\n\n"
            f"{exclusions}\n"
        )
