from __future__ import annotations

import functools
import platform
import sqlite3
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import typer
import yaml
from pydantic import ValidationError

from horizon import __version__
from horizon.adapters.model.config import load_provider_config, resolve_credential
from horizon.adapters.model.openai_compatible import OpenAICompatibleModelGateway
from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.adapters.persistence.campaign_budget import CampaignBudgetLedger
from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.adapters.retrieval.sqlite_fts import SQLiteCodeRetriever
from horizon.adapters.sandbox.docker import DockerSandbox
from horizon.adapters.sandbox.validation import DockerAcceptanceExecutor
from horizon.adapters.vcs.git import read_git_head, verify_clean_git_checkout
from horizon.adapters.workspace.promotion import WorkspacePromoter, workspace_path_hash
from horizon.adapters.workspace.snapshot import SnapshotManager
from horizon.application.agent_loop import AgentLoopConfig, CodingAgentRunner
from horizon.application.human import OperatorGuidanceService
from horizon.application.model_probe import (
    ModelProbeService,
    build_probe_request,
    conservative_input_ceiling,
    conservative_input_sizing,
)
from horizon.application.model_sizing import analyze_model_request_sizing
from horizon.application.pilot import (
    PilotPreflightService,
    load_pilot_preflight_report,
    verify_pilot_launch_binding,
)
from horizon.application.planning import (
    PlanGenerator,
    PlanGeneratorConfig,
    build_planning_context,
)
from horizon.application.portfolio_demo import PortfolioDemoRunner
from horizon.application.promotion import PromotionService
from horizon.application.recovery import RecoveryService
from horizon.application.recovery_eval import RecoveryMatrixEvaluator
from horizon.application.reliability_eval import ReliabilityEvaluator
from horizon.application.reservation_analysis import analyze_reservation_traces
from horizon.application.retrieval_eval import RetrievalEvaluator
from horizon.application.run_ab_eval import (
    RunABEvaluator,
    build_run_ab_suite_report,
    store_run_ab_report,
    store_run_ab_suite_report,
)
from horizon.application.services import HarnessService, LeaseToken
from horizon.application.tool_recovery import ToolRecoveryService
from horizon.domain.common import canonical_json, digest
from horizon.domain.errors import (
    BudgetExceeded,
    Conflict,
    HorizonError,
    NotFound,
    PlanProposalError,
    PolicyDenied,
)
from horizon.domain.evaluation import RetrievalEvalManifest
from horizon.domain.human import HumanGuidanceRequest, HumanPlanRequest
from horizon.domain.model import ModelPolicyBinding
from horizon.domain.pilot import RealModelPilotManifest
from horizon.domain.plan import MAX_EXECUTION_REPLANS, Plan
from horizon.domain.promotion import WorkspaceOrigin
from horizon.domain.recovery_evaluation import RecoveryMatrixManifest
from horizon.domain.reliability import ReliabilityEvalManifest
from horizon.domain.run import projection_hash
from horizon.domain.run_evaluation import RunABEvalManifest, RunABSuiteManifest
from horizon.domain.states import RunStatus
from horizon.domain.task import TaskSpec
from horizon.tools.gateway import WorkspaceToolGateway

app = typer.Typer(no_args_is_help=True, help="Horizon durable control plane (development preview).")
tasks = typer.Typer(no_args_is_help=True)
plans = typer.Typer(no_args_is_help=True)
traces = typer.Typer(no_args_is_help=True)
models = typer.Typer(no_args_is_help=True)
agents = typer.Typer(no_args_is_help=True)
evaluations = typer.Typer(no_args_is_help=True)
demos = typer.Typer(no_args_is_help=True)
app.add_typer(tasks, name="task")
app.add_typer(plans, name="plan")
app.add_typer(traces, name="trace")
app.add_typer(models, name="model")
app.add_typer(agents, name="agent")
app.add_typer(evaluations, name="eval")
app.add_typer(demos, name="demo")


def guarded(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ValidationError as exc:
            errors = [{"loc": error["loc"], "message": error["msg"]} for error in exc.errors()]
            typer.echo(canonical_json({"error": "schema_validation", "details": errors}), err=True)
            raise typer.Exit(2) from None
        except BudgetExceeded as exc:
            payload = {"error": type(exc).__name__, "message": str(exc)}
            if exc.stop is not None:
                payload["budget_stop"] = exc.stop.model_dump(mode="json")
            if exc.model_request_budget is not None:
                payload["model_request_budget"] = exc.model_request_budget.as_dict()
            typer.echo(canonical_json(payload), err=True)
            raise typer.Exit(2) from None
        except (HorizonError, OSError, ValueError, yaml.YAMLError) as exc:
            message = "Invalid YAML" if isinstance(exc, yaml.YAMLError) else str(exc)
            typer.echo(canonical_json({"error": type(exc).__name__, "message": message}), err=True)
            raise typer.Exit(2) from None

    return wrapped


def read_yaml(path: Path):
    with path.open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def store_for(
    ctx: typer.Context, *, must_exist: bool = True, read_only: bool = False
) -> SQLiteEventStore:
    path = ctx.obj["db"]
    if must_exist and not path.is_file():
        raise NotFound("No control database yet; prepare a run first")
    return SQLiteEventStore(path, read_only=read_only)


def active_plan_item(run):
    if run.plan is None:
        raise ValueError("Run has no active plan")
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
            raise ValueError("Persisted Agent session has no active work item")
        return item
    ready = run.plan.ready_items(run.passed_items)
    if not ready:
        raise ValueError("Run has no dependency-ready work item")
    return ready[0]


def pending_human_next_action(run) -> str | None:
    request = run.pending_human_request
    if isinstance(request, HumanPlanRequest):
        return f"horizon plan set {run.run_id} <plan.yaml>"
    if isinstance(request, HumanGuidanceRequest):
        return f"horizon agent guide {run.run_id} <guidance.txt>"
    return None


def plan_source_label(run) -> str:
    if run.plan_source_model_call_id is None:
        return "file"
    record = next(
        (item for item in run.model_calls if item.call_id == run.plan_source_model_call_id),
        None,
    )
    if record is None:
        raise ValueError("Plan source model receipt is missing")
    return "execution_replan" if record.purpose == "execution" else "model"


def release_quiescent_lease(
    service: HarnessService,
    run_id: str,
    token: LeaseToken,
    key: str,
) -> None:
    """Release a live local lease after a handled pre-dispatch failure."""

    latest = service.store.get(run_id)
    if (
        latest.terminal
        or latest.lease_id != token.lease_id
        or latest.lease_epoch != token.epoch
        or set(latest.reservations) - latest.unknown_reservations
    ):
        return
    try:
        service.release_lease(run_id, token, key)
    except HorizonError:
        # Preserve the original planning failure; recovery will fence an unreleased lease.
        return


@app.callback()
def root(
    ctx: typer.Context,
    db: Annotated[
        Path, typer.Option(help="Controller database; never mount into an agent.")
    ] = Path(".horizon/control.sqlite3"),
):
    ctx.obj = {"db": db}


@tasks.command("validate")
@guarded
def task_validate(path: Path):
    task = TaskSpec.model_validate(read_yaml(path))
    typer.echo(canonical_json({"valid": True, "task_id": task.task_id, "sha256": task.sha256}))


@evaluations.command("retrieval")
@guarded
def evaluate_retrieval(
    path: Path,
    source: Annotated[
        Path, typer.Option(help="Workspace snapshot to evaluate without executing its code.")
    ] = Path("."),
    state_dir: Annotated[
        Path | None,
        typer.Option(help="Derived artifacts and FTS cache; defaults under source/.horizon."),
    ] = None,
):
    """Run a fixed, offline lexical-retrieval diagnostic."""
    manifest = RetrievalEvalManifest.model_validate(read_yaml(path))
    source = source.resolve(strict=True)
    if not source.is_dir():
        raise ValueError("Retrieval evaluation source must be a directory")
    state_root = (state_dir or source / ".horizon" / "retrieval-eval").resolve()
    if state_root.is_relative_to(source):
        relative = state_root.relative_to(source)
        if not relative.parts or relative.parts[0] != ".horizon":
            raise ValueError("Evaluation state inside source must live below source/.horizon")

    artifacts = ArtifactStore(state_root / "artifacts")
    snapshots = SnapshotManager(artifacts)
    snapshot, source_manifest_ref = snapshots.capture(
        source,
        allowed_paths=manifest.allowed_paths,
        denied_paths=manifest.denied_paths,
    )
    retriever = SQLiteCodeRetriever(state_root / "retrieval.sqlite3", snapshots)
    report = RetrievalEvaluator(retriever).evaluate(
        manifest,
        source_manifest_ref=source_manifest_ref,
        workspace_revision=snapshot.workspace_revision,
    )
    report_ref = artifacts.put(canonical_json(report).encode("utf-8"))
    typer.echo(
        canonical_json(
            {
                "report": report.model_dump(mode="json"),
                "report_ref": report_ref,
            }
        )
    )


@evaluations.command("reliability")
@guarded
def evaluate_reliability(
    path: Path,
    state_dir: Annotated[
        Path,
        typer.Option(help="Content-addressed evaluation reports; no provider state is loaded."),
    ] = Path(".horizon/reliability-eval"),
):
    """Run frozen controller-policy traces without a model, network, or repository code."""
    manifest = ReliabilityEvalManifest.model_validate(read_yaml(path))
    report = ReliabilityEvaluator().evaluate(manifest)
    artifacts = ArtifactStore(state_dir.resolve() / "artifacts")
    report_ref = artifacts.put(canonical_json(report).encode("utf-8"))
    typer.echo(
        canonical_json(
            {
                "report": report.model_dump(mode="json"),
                "report_ref": report_ref,
            }
        )
    )


@evaluations.command("recovery")
@guarded
def evaluate_recovery_matrix(
    path: Path,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            help="New self-contained evidence directory; defaults below .horizon/recovery-eval.",
        ),
    ] = None,
):
    """Run the bounded offline crash/write-state recovery matrix."""
    manifest = RecoveryMatrixManifest.model_validate(read_yaml(path))
    destination = output or Path(".horizon") / "recovery-eval" / f"matrix-{uuid4().hex[:12]}"
    result = RecoveryMatrixEvaluator().evaluate(manifest, destination)
    typer.echo(
        canonical_json(
            {
                "benchmark_id": result.report.benchmark_id,
                "manifest_digest": result.report.manifest_digest,
                "case_count": result.report.case_count,
                "passed_case_count": result.report.passed_case_count,
                "recovery_success_rate": result.report.recovery_success_rate,
                "safe_block_rate": result.report.safe_block_rate,
                "unrecoverable_count": result.report.unrecoverable_count,
                "incorrect_resume_count": result.report.incorrect_resume_count,
                "recovery_redispatch_count": result.report.recovery_redispatch_count,
                "duplicate_side_effect_count": result.report.duplicate_side_effect_count,
                "actual_process_crash_case_count": (result.report.actual_process_crash_case_count),
                "trace_replay_verified_count": (result.report.trace_replay_verified_count),
                "paid_model_called": result.report.paid_model_called,
                "network_called": result.report.network_called,
                "repository_code_executed": result.report.repository_code_executed,
                "external_cost_cny": str(result.report.external_cost_cny),
                "claim_scope": result.report.claim_scope,
                "excluded_claims": result.report.excluded_claims,
                "output_dir": str(result.output_dir),
                "report": str(result.report_path),
                "report_ref": result.report_ref,
            }
        )
    )
    if result.report.passed_case_count != result.report.case_count:
        raise typer.Exit(3)


@evaluations.command("pilot-preflight")
@guarded
def evaluate_pilot_preflight(
    path: Path,
    image: Annotated[
        str,
        typer.Option(
            "--image",
            help="Existing local Linux image for the initial-failure gate; never pulled.",
        ),
    ],
    config_path: Annotated[
        Path, typer.Option("--config", help="Strict pilot provider policy without secrets.")
    ] = Path("config/providers/siliconflow-pilot.yaml"),
    state_dir: Annotated[
        Path,
        typer.Option(help="Isolated immutable preflight evidence and prepared TaskSpec."),
    ] = Path(".horizon/real-model-pilot"),
):
    """Validate a solution-blind real-model task without loading credentials or calling a model."""
    manifest_path = path.resolve(strict=True)
    manifest = RealModelPilotManifest.model_validate(read_yaml(manifest_path))
    source = (manifest_path.parent / manifest.source_path).resolve(strict=True)
    if not source.is_relative_to(manifest_path.parent):
        raise ValueError("Pilot source escapes the manifest directory")
    provider = load_provider_config(config_path)
    ledger_path = Path(provider.ledger_path)
    ledger = CampaignBudgetLedger(ledger_path)
    # Creating an immutable zero-spend campaign definition is part of offline
    # preflight; no credential or provider request is involved.
    campaign = ledger.initialize(
        provider.campaign,
        provider_id=provider.provider_id,
        model_id=provider.model.id,
    )
    state_root = state_dir.resolve()
    staging_root = state_root / "staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    sandbox = DockerSandbox(staging_root, image)
    result = PilotPreflightService(
        DockerAcceptanceExecutor(sandbox),
        validation_backend_ref=sandbox.image_id,
    ).evaluate(
        manifest,
        source=source,
        state_dir=state_root,
        provider=provider,
        campaign=campaign,
    )
    typer.echo(
        canonical_json(
            {
                "report": result.report.model_dump(mode="json"),
                "report_ref": result.report_ref,
                "report_path": str(result.report_path),
                "prepared_task_path": str(result.prepared_task_path),
                "requires_paid_confirmation": True,
                "launch": {
                    "command": "horizon agent run",
                    "task_path": str(result.prepared_task_path),
                    "auto_plan": True,
                    "image": image,
                    "config_path": str(config_path.resolve()),
                    "pilot_preflight": str(result.report_path),
                },
            }
        )
    )
    if not result.report.ready:
        raise typer.Exit(3)


@evaluations.command("run-ab")
@guarded
def evaluate_run_ab(
    path: Path,
    image: Annotated[
        str,
        typer.Option(
            "--image",
            help="Existing local Linux image for protected checks; no image is pulled.",
        ),
    ],
    state_dir: Annotated[
        Path,
        typer.Option(help="Isolated Run databases, workspaces, traces, and reports."),
    ] = Path(".horizon/run-ab-eval"),
):
    """Compare two frozen full Agent Runs without calling a paid model or the network."""
    manifest_path = path.resolve(strict=True)
    manifest = RunABEvalManifest.model_validate(read_yaml(manifest_path))
    fixture_source = (manifest_path.parent / manifest.fixture_path).resolve(strict=True)
    state_root = state_dir.resolve()
    if state_root == fixture_source or state_root.is_relative_to(fixture_source):
        raise ValueError("Run A/B state directory cannot live inside the fixture repository")
    staging_root = state_root / "staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    sandbox = DockerSandbox(staging_root, image)
    report = RunABEvaluator(
        DockerAcceptanceExecutor(sandbox),
        validation_backend="docker",
        validation_backend_ref=sandbox.image_id,
        repository_code_executed=True,
    ).evaluate(
        manifest,
        fixture_source=fixture_source,
        state_dir=state_root,
    )
    artifacts = ArtifactStore(state_root / "artifacts")
    report_ref = store_run_ab_report(artifacts, report)
    typer.echo(
        canonical_json(
            {
                "report": report.model_dump(mode="json"),
                "report_ref": report_ref,
                "state_dir": str(state_root),
            }
        )
    )
    if not report.all_expectations_met:
        raise typer.Exit(3)


@evaluations.command("run-ab-suite")
@guarded
def evaluate_run_ab_suite(
    path: Path,
    image: Annotated[
        str,
        typer.Option(
            "--image",
            help="Existing local Linux image for every protected check; no image is pulled.",
        ),
    ],
    state_dir: Annotated[
        Path,
        typer.Option(help="Shared isolated state and content-addressed reports for the suite."),
    ] = Path(".horizon/run-ab-suite"),
):
    """Run a source-bound set of frozen full Agent A/B cases with one local image."""
    suite_path = path.resolve(strict=True)
    suite_root = suite_path.parent
    manifest = RunABSuiteManifest.model_validate(read_yaml(suite_path))
    state_root = state_dir.resolve()
    resolved_cases = []
    for case in manifest.cases:
        case_path = (suite_root / case.manifest_path).resolve(strict=True)
        if not case_path.is_relative_to(suite_root):
            raise ValueError("Run A/B suite case manifest escapes the suite directory")
        case_manifest = RunABEvalManifest.model_validate(read_yaml(case_path))
        case.check_manifest(case_manifest)
        fixture_source = (case_path.parent / case_manifest.fixture_path).resolve(strict=True)
        if case.source.reduction == "full_checkout":
            verify_clean_git_checkout(fixture_source, case.source.buggy_commit)
        if state_root == fixture_source or state_root.is_relative_to(fixture_source):
            raise ValueError(
                "Run A/B suite state directory cannot live inside a fixture repository"
            )
        resolved_cases.append((case, case_manifest, fixture_source))

    staging_root = state_root / "staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    sandbox = DockerSandbox(staging_root, image)
    evaluator = RunABEvaluator(
        DockerAcceptanceExecutor(sandbox),
        validation_backend="docker",
        validation_backend_ref=sandbox.image_id,
        repository_code_executed=True,
    )
    artifacts = ArtifactStore(state_root / "artifacts")
    reports = {}
    for case, case_manifest, fixture_source in resolved_cases:
        report = evaluator.evaluate(
            case_manifest,
            fixture_source=fixture_source,
            state_dir=state_root,
        )
        reports[case.case_id] = (report, store_run_ab_report(artifacts, report))

    suite_report = build_run_ab_suite_report(
        manifest,
        reports,
        validation_backend="docker",
        validation_backend_ref=sandbox.image_id,
        repository_code_executed=True,
    )
    report_ref = store_run_ab_suite_report(artifacts, suite_report)
    typer.echo(
        canonical_json(
            {
                "report": suite_report.model_dump(mode="json"),
                "report_ref": report_ref,
                "state_dir": str(state_root),
            }
        )
    )
    if not suite_report.all_expectations_met:
        raise typer.Exit(3)


@app.command("run")
@guarded
def prepare_run(
    ctx: typer.Context,
    path: Path,
    prepare_only: Annotated[
        bool, typer.Option(help="Validate and persist; do not execute.")
    ] = False,
    key: Annotated[str | None, typer.Option(help="Idempotency key for safe retries.")] = None,
):
    if not prepare_only:
        raise ValueError(
            "Execution backend is not connected yet. Use --prepare-only for the durable contract; "
            "this command does not call a model or execute repository code."
        )
    task = TaskSpec.model_validate(read_yaml(path))
    run = store_for(ctx, must_exist=False).create(task, key or f"create_{uuid4().hex}")
    typer.echo(canonical_json(run.as_dict()))


@plans.command("set")
@guarded
def plan_set(ctx: typer.Context, run_id: str, path: Path, key: str | None = None):
    plan = Plan.model_validate(read_yaml(path))
    service = HarnessService(store_for(ctx))
    current = service.store.get(run_id)
    if current.status == RunStatus.WAITING_FOR_USER and isinstance(
        current.pending_human_request,
        HumanPlanRequest,
    ):
        run = service.resolve_replacement_plan(
            run_id,
            plan,
            key or f"resolve_plan_{uuid4().hex}",
        )
    else:
        run = service.set_plan(run_id, plan, key or f"plan_{uuid4().hex}")
    typer.echo(canonical_json(run.as_dict()))


@app.command()
@guarded
def status(ctx: typer.Context, run_id: str | None = None):
    store = store_for(ctx, read_only=True)
    typer.echo(canonical_json(store.get(run_id).as_dict() if run_id else store.list_runs()))


@app.command()
@guarded
def cancel(ctx: typer.Context, run_id: str, key: str | None = None):
    run = HarnessService(store_for(ctx)).cancel(run_id, key or f"cancel_{uuid4().hex}")
    typer.echo(canonical_json(run.as_dict()))


@traces.command("export")
@guarded
def trace_export(ctx: typer.Context, run_id: str, output: Path | None = None):
    content = store_for(ctx, read_only=True).export_jsonl(run_id)
    if output:
        # Never silently replace a previous trace or evidence file.
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        typer.echo(canonical_json({"trace": str(output.resolve())}))
    else:
        typer.echo(content, nl=False)


@traces.command("replay")
@guarded
def trace_replay(path: Path):
    run = SQLiteEventStore.replay_jsonl(path.read_text(encoding="utf-8"))
    typer.echo(canonical_json({"state": run.as_dict(), "projection_hash": projection_hash(run)}))


@traces.command("reservation-report")
@guarded
def trace_reservation_report(
    paths: Annotated[
        list[Path],
        typer.Argument(help="One or more replayable Trace JSONL files."),
    ],
):
    """Compare conservative model reservations with settled provider-reported usage."""

    typer.echo(canonical_json(analyze_reservation_traces(paths)))


@demos.command("run")
@guarded
def portfolio_demo_run(
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            help="New evidence directory; defaults to a unique path below .horizon/demos.",
        ),
    ] = None,
):
    """Run the deterministic offline hard-crash recovery story and export its EvidencePack."""
    destination = output or Path(".horizon") / "demos" / f"portfolio-{uuid4().hex[:12]}"
    result = PortfolioDemoRunner().run(destination)
    typer.echo(
        canonical_json(
            {
                "demo_id": result.report.demo_id,
                "run_id": result.report.run_id,
                "status": result.report.status,
                "all_checks_passed": result.report.verification.all_checks_passed,
                "output_dir": str(result.output_dir),
                "evidence_pack": str(result.evidence_pack_path),
                "summary": str(result.summary_path),
                "report": str(result.report_path),
                "trace": str(result.trace_path),
                "final_state": str(result.final_run_path),
                "workspace": str(result.workspace),
                "paid_model_called": result.report.paid_model_called,
                "network_called": result.report.network_called,
                "repository_code_executed": result.report.repository_code_executed,
                "external_cost_cny": str(result.report.external_cost_cny),
                "simulated_model_cost_cny": str(result.report.simulated_model_cost),
                "claim_scope": result.report.claim_scope,
                "recovery_mode": result.report.recovery_mode,
                "worker_handoffs": result.report.worker_handoffs,
                "hard_crash_recovery_verified": (
                    result.report.verification.hard_crash_recovery_verified
                ),
                "crashed_worker_exit_code": (
                    result.report.crash_recovery.observed_exit_code
                    if result.report.crash_recovery is not None
                    else None
                ),
                "write_recovery_disposition": (
                    result.report.crash_recovery.recovery_disposition
                    if result.report.crash_recovery is not None
                    else None
                ),
                "excluded_claims": result.report.excluded_claims,
            }
        )
    )
    if not result.report.verification.all_checks_passed:
        raise typer.Exit(3)


@models.command("check")
@guarded
def model_check(
    config_path: Annotated[
        Path, typer.Option("--config", help="Provider configuration without secrets.")
    ] = Path("config/providers/siliconflow.yaml"),
    dotenv_path: Annotated[
        Path, typer.Option("--dotenv", help="Ignored local credential file.")
    ] = Path(".env"),
):
    config = load_provider_config(config_path)
    credential = resolve_credential(config, dotenv_path)
    probe_request = build_probe_request(
        config.model.id,
        max_output_tokens=config.request.probe_max_output_tokens,
        enable_thinking=config.request.enable_thinking,
    )
    probe_estimate, probe_payload = conservative_input_sizing(probe_request)
    if probe_estimate.token_ceiling > config.request.max_input_tokens:
        raise PolicyDenied("Probe request exceeds the configured input-token budget")
    probe_reservation = config.pricing.reserve_cost(
        probe_estimate.token_ceiling,
        probe_request.max_output_tokens,
    )
    typer.echo(
        canonical_json(
            {
                "valid": True,
                "provider_id": config.provider_id,
                "api_type": config.api_type,
                "base_url": config.base_url,
                "model": config.model.id,
                "credential_source": credential.source,
                "credential_present": True,
                "currency": config.campaign.currency,
                "campaign_max_cost": str(config.campaign.max_cost),
                "per_call_max_cost": str(config.campaign.max_cost_per_call),
                "per_run_max_cost": str(config.run_budget.max_cost),
                "probe_reserved_cost": str(probe_reservation),
                "probe_max_output_tokens": probe_request.max_output_tokens,
                "max_context_chars": config.request.max_context_chars,
                "max_input_tokens": config.request.max_input_tokens,
                "probe_input_token_estimate": probe_estimate.model_dump(mode="json"),
                "probe_request_payload": probe_payload.model_dump(mode="json"),
                "preserve_recent_context_units": (config.request.preserve_recent_context_units),
                "fallback_enabled": config.fallback_enabled,
                "network_called": False,
            }
        )
    )


@models.command("sizing-report")
@guarded
def model_sizing_report(
    config_path: Annotated[
        Path, typer.Option("--config", help="Provider configuration without secrets.")
    ] = Path("config/providers/siliconflow.yaml"),
):
    config = load_provider_config(config_path)
    typer.echo(
        canonical_json(
            analyze_model_request_sizing(
                config.model.id,
                max_output_tokens=min(512, config.request.max_output_tokens),
                enable_thinking=config.request.enable_thinking,
            )
        )
    )


@models.command("probe")
@guarded
def model_probe(
    config_path: Annotated[
        Path, typer.Option("--config", help="Provider configuration without secrets.")
    ] = Path("config/providers/siliconflow.yaml"),
    dotenv_path: Annotated[
        Path, typer.Option("--dotenv", help="Ignored local credential file.")
    ] = Path(".env"),
    confirm_paid: Annotated[
        bool,
        typer.Option(
            "--confirm-paid",
            help="Acknowledge that this single bounded request can incur provider charges.",
        ),
    ] = False,
):
    if not confirm_paid:
        raise ValueError("Paid provider probe requires --confirm-paid")
    config = load_provider_config(config_path)
    credential = resolve_credential(config, dotenv_path)
    ledger_path = Path(config.ledger_path)
    gateway = OpenAICompatibleModelGateway(config, credential)
    request = build_probe_request(
        config.model.id,
        max_output_tokens=config.request.probe_max_output_tokens,
        enable_thinking=config.request.enable_thinking,
    )
    if conservative_input_ceiling(request) > config.request.max_input_tokens:
        raise PolicyDenied("Probe request exceeds the configured input-token budget")
    result = ModelProbeService(gateway, CampaignBudgetLedger(ledger_path)).run(
        provider_id=config.provider_id,
        request=request,
        pricing=config.pricing,
        budget=config.campaign,
    )
    typer.echo(canonical_json(result))
    if not result.passed:
        raise typer.Exit(3)


@models.command("budget")
@guarded
def model_budget(
    config_path: Annotated[
        Path, typer.Option("--config", help="Provider configuration without secrets.")
    ] = Path("config/providers/siliconflow.yaml"),
):
    config = load_provider_config(config_path)
    ledger_path = Path(config.ledger_path)
    if not ledger_path.is_file():
        raise NotFound("No provider campaign ledger yet; run a confirmed probe first")
    summary = CampaignBudgetLedger(ledger_path).summary(config.campaign.campaign_id)
    typer.echo(canonical_json(summary))


@agents.command("run")
@guarded
def agent_run(
    ctx: typer.Context,
    task_path: Path,
    image: Annotated[
        str,
        typer.Option(
            "--image",
            help="Existing local Linux image; Horizon never pulls it automatically.",
        ),
    ],
    plan_path: Annotated[
        Path | None,
        typer.Argument(help="Validated Plan YAML; omit only with --auto-plan."),
    ] = None,
    auto_plan: Annotated[
        bool,
        typer.Option(
            "--auto-plan",
            help="Use one budgeted model call to propose and validate a bounded Plan.",
        ),
    ] = False,
    config_path: Annotated[
        Path, typer.Option("--config", help="Provider configuration without secrets.")
    ] = Path("config/providers/siliconflow.yaml"),
    dotenv_path: Annotated[
        Path, typer.Option("--dotenv", help="Ignored local credential file.")
    ] = Path(".env"),
    confirm_paid: Annotated[
        bool,
        typer.Option(
            "--confirm-paid",
            help="Acknowledge bounded paid model calls for this Run.",
        ),
    ] = False,
    pilot_preflight: Annotated[
        Path | None,
        typer.Option(
            "--pilot-preflight",
            help="Content-addressed offline pilot report that must still match this launch.",
        ),
    ] = None,
    slice_iterations: Annotated[
        int | None,
        typer.Option(
            "--slice-iterations",
            min=1,
            help="Yield at a safe persisted turn boundary after this many model iterations.",
        ),
    ] = None,
):
    if not confirm_paid:
        raise ValueError("Agent execution requires --confirm-paid")
    if auto_plan == (plan_path is not None):
        raise ValueError("Provide exactly one Plan source: PLAN_PATH or --auto-plan")
    if pilot_preflight is not None and not auto_plan:
        raise ValueError("A preflighted real-model pilot must use --auto-plan")
    provider = load_provider_config(config_path)
    credential = resolve_credential(provider, dotenv_path)
    template = TaskSpec.model_validate(read_yaml(task_path))
    pilot_report = None
    pilot_preflight_ref = None
    if pilot_preflight is not None:
        pilot_report, pilot_preflight_ref = load_pilot_preflight_report(pilot_preflight)
    if template.repository.source != "local" or template.repository.path is None:
        raise ValueError("The first executable loop requires a local repository source")
    if template.model_policy_id != provider.policy_id:
        raise ValueError("TaskSpec model_policy_id does not match the provider config")
    if auto_plan and template.budgets.max_model_calls < 2:
        raise ValueError("Automatic planning requires at least two TaskSpec model calls")
    source = Path(template.repository.path).resolve(strict=True)
    if not source.is_dir():
        raise ValueError("Local repository source must be a directory")

    control_root = Path(".horizon")
    artifacts = ArtifactStore(control_root / "artifacts")
    snapshots = SnapshotManager(artifacts)
    initial_snapshot, initial_manifest = snapshots.capture(
        source,
        denied_paths=template.constraints.denied_paths,
    )
    staging_root = control_root / "staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    sandbox = None
    if pilot_report is not None:
        sandbox = DockerSandbox(staging_root, image)
        verify_pilot_launch_binding(
            pilot_report,
            task=template,
            provider=provider,
            source_git_head=read_git_head(source),
            source_snapshot_revision=initial_snapshot.workspace_revision,
            validation_backend_ref=sandbox.image_id,
        )
        current_campaign = CampaignBudgetLedger(Path(provider.ledger_path)).summary(
            provider.campaign.campaign_id
        )
        if current_campaign.remaining_cost < pilot_report.run_cost_cap:
            raise BudgetExceeded(
                "Campaign no longer has headroom for the preflighted Pilot Run cap"
            )
    workspace = staging_root / f"agent-{uuid4().hex}"
    snapshots.restore(initial_manifest, workspace)

    task_data = template.model_dump(mode="json")
    task_data["task_id"] = f"{template.task_id}-{uuid4().hex[:8]}"
    task_data["repository"] = {
        "source": "local",
        "path": str(workspace.resolve()),
        "base_commit": initial_snapshot.workspace_revision,
    }
    task = TaskSpec.model_validate(task_data)
    manual_plan = Plan.model_validate(read_yaml(plan_path)) if plan_path is not None else None
    if manual_plan is not None:
        manual_plan.check_task(task)
    store = store_for(ctx, must_exist=False)
    service = HarnessService(store)
    run = store.create(task, f"create_{uuid4().hex}")
    run = service.bind_workspace_origin(
        run.run_id,
        WorkspaceOrigin(
            source_path_hash=workspace_path_hash(source),
            source_revision=initial_snapshot.workspace_revision,
            source_manifest_ref=initial_manifest,
            git_head=read_git_head(source),
        ),
        f"origin_{uuid4().hex}",
    )
    gateway = OpenAICompatibleModelGateway(provider, credential)
    ledger = CampaignBudgetLedger(Path(provider.ledger_path))
    planning_response_reused = False
    if auto_plan:
        run = service.acquire_lease(
            run.run_id,
            "local-agent",
            f"lease_{uuid4().hex}",
            ttl_seconds=600,
        )
        token = LeaseToken.from_run(run)
        run = service.transition(
            run.run_id,
            RunStatus.PLANNING,
            token,
            f"planning_{uuid4().hex}",
        )
        planning_context = build_planning_context(
            task,
            workspace_revision=initial_snapshot.workspace_revision,
            source_manifest_ref=initial_manifest,
            repository_paths=(entry.path for entry in initial_snapshot.files),
        )
        try:
            generated = PlanGenerator(
                service,
                gateway,
                ledger,
                artifacts,
                provider_id=provider.provider_id,
                model_id=provider.model.id,
                pricing=provider.pricing,
                campaign=provider.campaign,
                config=PlanGeneratorConfig(
                    max_output_tokens=min(1024, provider.request.max_output_tokens),
                    max_input_tokens=provider.request.max_input_tokens,
                    max_run_cost=provider.run_budget.max_cost,
                    enable_thinking=provider.request.enable_thinking,
                ),
            ).generate_and_set(run.run_id, token, planning_context)
        except PlanProposalError as exc:
            latest = service.store.get(run.run_id)
            planning_records = [
                record for record in latest.model_calls if record.purpose == "planning"
            ]
            if len(planning_records) != 1:
                raise
            waiting = service.request_replacement_plan(
                run.run_id,
                planning_records[0].call_id,
                str(exc),
                token,
                f"request_plan_{uuid4().hex}",
            )
            typer.echo(
                canonical_json(
                    {
                        "run_id": waiting.run_id,
                        "status": waiting.status,
                        "human_request": waiting.pending_human_request.model_dump(mode="json"),
                        "planning_error": str(exc),
                        "next_action": f"horizon plan set {waiting.run_id} <plan.yaml>",
                        "model_call_records": len(waiting.model_calls),
                        "campaign": ledger.summary(provider.campaign.campaign_id).model_dump(
                            mode="json"
                        ),
                    }
                )
            )
            raise typer.Exit(3) from None
        except BudgetExceeded:
            stopped = service.store.get(run.run_id)
            if stopped.budget_stop is None:
                release_quiescent_lease(
                    service,
                    run.run_id,
                    token,
                    f"planning_budget_error_release_{uuid4().hex}",
                )
                raise
            typer.echo(
                canonical_json(
                    {
                        "run_id": stopped.run_id,
                        "status": stopped.status,
                        "failure_reason": stopped.failure_reason,
                        "budget_stop": stopped.budget_stop.model_dump(mode="json"),
                        "model_request_budget": (
                            stopped.model_request_budget.as_dict()
                            if stopped.model_request_budget
                            else None
                        ),
                        "workspace": str(workspace.resolve()),
                        "campaign": ledger.summary(provider.campaign.campaign_id).model_dump(
                            mode="json"
                        ),
                    }
                )
            )
            raise typer.Exit(3) from None
        except HorizonError:
            release_quiescent_lease(
                service,
                run.run_id,
                token,
                f"planning_error_release_{uuid4().hex}",
            )
            raise
        run = generated.run
        plan = generated.plan
        planning_response_reused = generated.reused_response
    else:
        assert manual_plan is not None
        plan = manual_plan
        run = service.set_plan(run.run_id, plan, f"plan_{uuid4().hex}")
        run = service.acquire_lease(
            run.run_id,
            "local-agent",
            f"lease_{uuid4().hex}",
            ttl_seconds=600,
        )
        token = LeaseToken.from_run(run)

    run = service.transition(
        run.run_id,
        RunStatus.RUNNING,
        token,
        f"start_{uuid4().hex}",
    )

    initial_item = plan.ready_items(set())[0]
    if sandbox is None:
        sandbox = DockerSandbox(staging_root, image)
    checks = DockerAcceptanceExecutor(sandbox)
    tools = WorkspaceToolGateway(
        workspace,
        task,
        initial_item,
        snapshots,
        checks,
        SQLiteCodeRetriever(control_root / "retrieval.sqlite3", snapshots),
    )
    planning_calls = sum(record.purpose == "planning" for record in run.model_calls)
    try:
        result = CodingAgentRunner(
            service,
            gateway,
            ledger,
            tools,
            artifacts,
            provider_id=provider.provider_id,
            model_id=provider.model.id,
            pricing=provider.pricing,
            campaign=provider.campaign,
            config=AgentLoopConfig(
                max_model_iterations=min(8, task.budgets.max_model_calls - planning_calls),
                max_output_tokens=min(512, provider.request.max_output_tokens),
                max_run_cost=provider.run_budget.max_cost,
                enable_thinking=provider.request.enable_thinking,
                max_context_chars=provider.request.max_context_chars,
                max_input_tokens=provider.request.max_input_tokens,
                preserve_recent_context_units=provider.request.preserve_recent_context_units,
            ),
        ).run(
            run.run_id,
            token,
            max_iterations_this_invocation=slice_iterations,
        )
    except HorizonError:
        release_quiescent_lease(
            service,
            run.run_id,
            token,
            f"execution_error_release_{uuid4().hex}",
        )
        raise
    if result.status == RunStatus.RUNNING:
        result = service.release_lease(
            result.run_id,
            token,
            f"yield_{uuid4().hex}",
        )
    campaign = ledger.summary(provider.campaign.campaign_id)
    source_after, _ = snapshots.capture(
        source,
        denied_paths=template.constraints.denied_paths,
    )
    source_unchanged = source_after.workspace_revision == initial_snapshot.workspace_revision
    typer.echo(
        canonical_json(
            {
                "run_id": result.run_id,
                "status": result.status,
                "failure_reason": result.failure_reason,
                "budget_stop": (
                    result.budget_stop.model_dump(mode="json") if result.budget_stop else None
                ),
                "model_request_budget": (
                    result.model_request_budget.as_dict() if result.model_request_budget else None
                ),
                "workspace": str(workspace.resolve()),
                "source_workspace_unchanged": source_unchanged,
                "pilot_preflight_ref": pilot_preflight_ref,
                "continuation_required": result.status
                in {RunStatus.RUNNING, RunStatus.WAITING_FOR_USER},
                "human_request": (
                    result.pending_human_request.model_dump(mode="json")
                    if result.pending_human_request
                    else None
                ),
                "next_action": pending_human_next_action(result),
                "plan_source": plan_source_label(result),
                "plan_source_model_call_id": result.plan_source_model_call_id,
                "execution_replans": [
                    record.model_dump(mode="json") for record in result.execution_replans
                ],
                "planning_response_reused": planning_response_reused,
                "usage": result.usage.model_dump(mode="json"),
                "model_cost": str(result.model_occupied_cost),
                "model_currency": provider.pricing.currency,
                "model_call_records": len(result.model_calls),
                "tool_call_records": len(result.tool_calls),
                "check_cleanup_failures": tools.check_cleanup_failures,
                "validation": result.validation,
                "checkpoint": result.last_checkpoint,
                "agent_session": (
                    result.agent_session.model_dump(mode="json") if result.agent_session else None
                ),
                "campaign": campaign.model_dump(mode="json"),
            }
        )
    )
    if result.status not in {RunStatus.SUCCEEDED, RunStatus.RUNNING}:
        raise typer.Exit(3)


@agents.command("resume")
@guarded
def agent_resume(
    ctx: typer.Context,
    run_id: str,
    image: Annotated[
        str,
        typer.Option(
            "--image",
            help="Existing local Linux image; Horizon never pulls it automatically.",
        ),
    ],
    config_path: Annotated[
        Path, typer.Option("--config", help="Provider configuration without secrets.")
    ] = Path("config/providers/siliconflow.yaml"),
    dotenv_path: Annotated[
        Path, typer.Option("--dotenv", help="Ignored local credential file.")
    ] = Path(".env"),
    confirm_paid: Annotated[
        bool,
        typer.Option(
            "--confirm-paid",
            help="Acknowledge bounded paid model calls for this continuation.",
        ),
    ] = False,
    confirm_old_worker_stopped: Annotated[
        bool,
        typer.Option(
            "--confirm-old-worker-stopped",
            help=(
                "Allow takeover only after an old lease expired and its process is confirmed "
                "stopped."
            ),
        ),
    ] = False,
    slice_iterations: Annotated[
        int | None,
        typer.Option(
            "--slice-iterations",
            min=1,
            help="Yield again after this many additional model iterations.",
        ),
    ] = None,
):
    if not confirm_paid:
        raise ValueError("Agent continuation requires --confirm-paid")
    provider = load_provider_config(config_path)
    credential = resolve_credential(provider, dotenv_path)
    store = store_for(ctx)
    service = HarnessService(store)
    current = store.get(run_id)
    if current.status == RunStatus.WAITING_FOR_USER and isinstance(
        current.pending_human_request,
        HumanGuidanceRequest,
    ):
        raise ValueError(
            f"Run requires operator guidance first: horizon agent guide {run_id} <guidance.txt>"
        )
    if current.status not in {RunStatus.PLANNING, RunStatus.READY, RunStatus.RUNNING}:
        raise ValueError("Only a PLANNING, READY, or RUNNING Agent Run can continue")
    if current.status == RunStatus.RUNNING and current.agent_session is None:
        raise ValueError("Run has no persisted Agent session to continue")
    if current.status == RunStatus.PLANNING and current.plan is not None:
        raise ValueError("PLANNING Run cannot already have an active Plan")
    if current.status == RunStatus.READY and current.plan is None:
        raise ValueError("READY Run has no validated Plan")
    if current.reservations:
        raise ValueError("Run has unsettled operations that require reconciliation")
    if current.task.model_policy_id != provider.policy_id:
        raise ValueError("Run model_policy_id does not match the provider config")
    if current.task.repository.source != "local" or current.task.repository.path is None:
        raise ValueError("The first resumable loop requires a local staging workspace")
    if current.workspace_origin is None:
        raise ValueError("Resumable Agent Run has no bound workspace origin")

    control_root = Path(".horizon")
    staging_root = (control_root / "staging").resolve(strict=True)
    workspace = Path(current.task.repository.path).resolve(strict=True)
    if not workspace.is_dir() or not workspace.is_relative_to(staging_root):
        raise ValueError("Run workspace is not inside the controlled staging root")

    artifacts = ArtifactStore(control_root / "artifacts")
    snapshots = SnapshotManager(artifacts)
    leased = service.acquire_lease(
        run_id,
        "local-agent-resume",
        f"resume_lease_{uuid4().hex}",
        ttl_seconds=600,
        prior_worker_stopped=confirm_old_worker_stopped,
    )
    token = LeaseToken.from_run(leased)
    gateway = OpenAICompatibleModelGateway(provider, credential)
    ledger = CampaignBudgetLedger(Path(provider.ledger_path))
    current = leased
    planning_response_reused = False
    if current.status == RunStatus.PLANNING:
        initial_snapshot = snapshots.verify(current.workspace_origin.source_manifest_ref)
        staged_snapshot, _ = snapshots.capture(
            workspace,
            denied_paths=current.task.constraints.denied_paths,
        )
        if (
            initial_snapshot.workspace_revision != current.task.repository.base_commit
            or staged_snapshot.workspace_revision != initial_snapshot.workspace_revision
        ):
            release_quiescent_lease(
                service,
                run_id,
                token,
                f"resume_scope_error_release_{uuid4().hex}",
            )
            raise ValueError("Planning workspace changed after its immutable origin snapshot")
        planning_context = build_planning_context(
            current.task,
            workspace_revision=initial_snapshot.workspace_revision,
            source_manifest_ref=current.workspace_origin.source_manifest_ref,
            repository_paths=(entry.path for entry in initial_snapshot.files),
        )
        try:
            generated = PlanGenerator(
                service,
                gateway,
                ledger,
                artifacts,
                provider_id=provider.provider_id,
                model_id=provider.model.id,
                pricing=provider.pricing,
                campaign=provider.campaign,
                config=PlanGeneratorConfig(
                    max_output_tokens=min(1024, provider.request.max_output_tokens),
                    max_input_tokens=provider.request.max_input_tokens,
                    max_run_cost=provider.run_budget.max_cost,
                    enable_thinking=provider.request.enable_thinking,
                ),
            ).generate_and_set(run_id, token, planning_context)
        except PlanProposalError as exc:
            latest = service.store.get(run_id)
            planning_records = [
                record for record in latest.model_calls if record.purpose == "planning"
            ]
            if len(planning_records) != 1:
                raise
            waiting = service.request_replacement_plan(
                run_id,
                planning_records[0].call_id,
                str(exc),
                token,
                f"request_plan_{uuid4().hex}",
            )
            typer.echo(
                canonical_json(
                    {
                        "run_id": waiting.run_id,
                        "status": waiting.status,
                        "human_request": waiting.pending_human_request.model_dump(mode="json"),
                        "planning_error": str(exc),
                        "next_action": f"horizon plan set {waiting.run_id} <plan.yaml>",
                        "model_call_records": len(waiting.model_calls),
                        "campaign": ledger.summary(provider.campaign.campaign_id).model_dump(
                            mode="json"
                        ),
                    }
                )
            )
            raise typer.Exit(3) from None
        except BudgetExceeded:
            stopped = service.store.get(run_id)
            if stopped.budget_stop is None:
                release_quiescent_lease(
                    service,
                    run_id,
                    token,
                    f"resume_planning_budget_error_release_{uuid4().hex}",
                )
                raise
            typer.echo(
                canonical_json(
                    {
                        "run_id": stopped.run_id,
                        "status": stopped.status,
                        "failure_reason": stopped.failure_reason,
                        "budget_stop": stopped.budget_stop.model_dump(mode="json"),
                        "model_request_budget": (
                            stopped.model_request_budget.as_dict()
                            if stopped.model_request_budget
                            else None
                        ),
                        "workspace": str(workspace),
                        "campaign": ledger.summary(provider.campaign.campaign_id).model_dump(
                            mode="json"
                        ),
                    }
                )
            )
            raise typer.Exit(3) from None
        except HorizonError:
            release_quiescent_lease(
                service,
                run_id,
                token,
                f"resume_planning_error_release_{uuid4().hex}",
            )
            raise
        current = generated.run
        planning_response_reused = generated.reused_response
    if current.status == RunStatus.READY:
        current = service.transition(
            run_id,
            RunStatus.RUNNING,
            token,
            f"resume_start_{uuid4().hex}",
        )
    if current.plan is None:
        raise ValueError("Agent continuation has no validated Plan")

    sandbox = DockerSandbox(staging_root, image)
    checks = DockerAcceptanceExecutor(sandbox)
    tools = WorkspaceToolGateway(
        workspace,
        current.task,
        active_plan_item(current),
        snapshots,
        checks,
        SQLiteCodeRetriever(control_root / "retrieval.sqlite3", snapshots),
    )
    planning_calls = sum(record.purpose == "planning" for record in current.model_calls)
    try:
        result = CodingAgentRunner(
            service,
            gateway,
            ledger,
            tools,
            artifacts,
            provider_id=provider.provider_id,
            model_id=provider.model.id,
            pricing=provider.pricing,
            campaign=provider.campaign,
            config=AgentLoopConfig(
                max_model_iterations=min(
                    8,
                    current.task.budgets.max_model_calls - planning_calls,
                ),
                max_output_tokens=min(512, provider.request.max_output_tokens),
                max_run_cost=provider.run_budget.max_cost,
                enable_thinking=provider.request.enable_thinking,
                max_context_chars=provider.request.max_context_chars,
                max_input_tokens=provider.request.max_input_tokens,
                preserve_recent_context_units=provider.request.preserve_recent_context_units,
            ),
        ).run(
            run_id,
            token,
            max_iterations_this_invocation=slice_iterations,
        )
    except HorizonError:
        release_quiescent_lease(
            service,
            run_id,
            token,
            f"resume_execution_error_release_{uuid4().hex}",
        )
        raise
    if result.status == RunStatus.RUNNING:
        result = service.release_lease(
            run_id,
            token,
            f"yield_{uuid4().hex}",
        )
    campaign = ledger.summary(provider.campaign.campaign_id)
    typer.echo(
        canonical_json(
            {
                "run_id": result.run_id,
                "status": result.status,
                "failure_reason": result.failure_reason,
                "budget_stop": (
                    result.budget_stop.model_dump(mode="json") if result.budget_stop else None
                ),
                "model_request_budget": (
                    result.model_request_budget.as_dict() if result.model_request_budget else None
                ),
                "workspace": str(workspace),
                "continuation_required": result.status
                in {RunStatus.RUNNING, RunStatus.WAITING_FOR_USER},
                "human_request": (
                    result.pending_human_request.model_dump(mode="json")
                    if result.pending_human_request
                    else None
                ),
                "next_action": pending_human_next_action(result),
                "plan_source": plan_source_label(result),
                "plan_source_model_call_id": result.plan_source_model_call_id,
                "execution_replans": [
                    record.model_dump(mode="json") for record in result.execution_replans
                ],
                "planning_response_reused": planning_response_reused,
                "usage": result.usage.model_dump(mode="json"),
                "model_cost": str(result.model_occupied_cost),
                "model_currency": provider.pricing.currency,
                "model_call_records": len(result.model_calls),
                "tool_call_records": len(result.tool_calls),
                "check_cleanup_failures": tools.check_cleanup_failures,
                "validation": result.validation,
                "checkpoint": result.last_checkpoint,
                "agent_session": (
                    result.agent_session.model_dump(mode="json") if result.agent_session else None
                ),
                "campaign": campaign.model_dump(mode="json"),
            }
        )
    )
    if result.status not in {RunStatus.SUCCEEDED, RunStatus.RUNNING}:
        raise typer.Exit(3)


@agents.command("guide")
@guarded
def agent_guide(
    ctx: typer.Context,
    run_id: str,
    path: Path,
    key: Annotated[str | None, typer.Option(help="Idempotency key for safe retries.")] = None,
):
    path = path.resolve(strict=True)
    if not path.is_file() or path.stat().st_size > 32 * 1024:
        raise ValueError("Guidance must be a UTF-8 text file no larger than 32 KiB")
    guidance = path.read_text(encoding="utf-8")
    store = store_for(ctx)
    service = HarnessService(store)
    current = store.get(run_id)
    if not isinstance(current.pending_human_request, HumanGuidanceRequest):
        raise ValueError("Run is not waiting for operator guidance")
    leased = service.acquire_lease(
        run_id,
        "local-guidance",
        f"guidance_lease_{uuid4().hex}",
        ttl_seconds=60,
    )
    token = LeaseToken.from_run(leased)
    resolved = False
    try:
        result = OperatorGuidanceService(
            service,
            ArtifactStore(Path(".horizon") / "artifacts"),
        ).apply(
            run_id,
            guidance,
            token,
            key or f"guide_{uuid4().hex}",
        )
        resolved = True
    finally:
        if not resolved:
            release_quiescent_lease(
                service,
                run_id,
                token,
                f"guidance_error_release_{uuid4().hex}",
            )
    decision = result.run.human_decisions[-1]
    typer.echo(
        canonical_json(
            {
                "run_id": result.run.run_id,
                "status": result.run.status,
                "decision": decision.model_dump(mode="json"),
                "guided_session_artifact_ref": result.session_artifact_ref,
                "paid_model_called": False,
                "next_action": (
                    f"horizon agent resume {result.run.run_id} --image <existing-image> "
                    "--confirm-paid"
                ),
            }
        )
    )


@agents.command("reconcile")
@guarded
def agent_reconcile(
    ctx: typer.Context,
    run_id: str,
    config_path: Annotated[
        Path, typer.Option("--config", help="Provider configuration without secrets.")
    ] = Path("config/providers/siliconflow.yaml"),
    confirm_old_worker_stopped: Annotated[
        bool,
        typer.Option(
            "--confirm-old-worker-stopped",
            help=(
                "Allow takeover only after an old lease expired and its process is confirmed "
                "stopped."
            ),
        ),
    ] = False,
):
    provider = load_provider_config(config_path)
    store = store_for(ctx)
    service = HarnessService(store)
    current = store.get(run_id)
    if current.terminal:
        raise ValueError("A terminal Run cannot be reconciled by an Agent worker")
    if current.model_policy is not None:
        expected_policy = ModelPolicyBinding(
            policy_id=current.task.model_policy_id,
            provider_id=provider.provider_id,
            model=provider.model.id,
            campaign_id=provider.campaign.campaign_id,
            currency=provider.pricing.currency,
            max_run_cost=provider.run_budget.max_cost,
            price_card_hash=digest(provider.pricing),
        )
        if current.model_policy != expected_policy:
            raise ValueError("Run model policy does not match the provider configuration")
    ledger_path = Path(provider.ledger_path)
    if not ledger_path.is_file():
        raise NotFound("No provider campaign ledger exists for reconciliation")
    ledger = CampaignBudgetLedger(ledger_path)
    leased = service.acquire_lease(
        run_id,
        "local-agent-recovery",
        f"recovery_lease_{uuid4().hex}",
        ttl_seconds=60,
        prior_worker_stopped=confirm_old_worker_stopped,
    )
    token = LeaseToken.from_run(leased)
    report = RecoveryService(
        service,
        ledger,
        ArtifactStore(Path(".horizon") / "artifacts"),
    ).reconcile(run_id, token)
    service.release_lease(run_id, token, f"recovery_release_{uuid4().hex}")
    typer.echo(
        canonical_json(
            {
                **report.model_dump(mode="json"),
                "campaign": ledger.summary(provider.campaign.campaign_id).model_dump(mode="json"),
                "network_called": False,
            }
        )
    )


@agents.command("diff")
@guarded
def agent_diff(
    ctx: typer.Context,
    run_id: str,
    source: Annotated[Path, typer.Option("--source", help="Original source workspace.")],
):
    source = source.resolve(strict=True)
    if not source.is_dir():
        raise ValueError("Promotion source must be a directory")
    artifact_root = Path(".horizon") / "artifacts"
    if not artifact_root.is_dir():
        raise NotFound("No Agent artifact store exists for promotion planning")
    service = HarnessService(store_for(ctx))
    promoter = WorkspacePromoter(SnapshotManager(ArtifactStore(artifact_root)))
    plan = PromotionService(service, promoter).plan(run_id, source)
    typer.echo(
        canonical_json(
            {
                "run_id": run_id,
                "plan_hash": plan.sha256,
                "source_revision_before": plan.source_revision_before,
                "candidate_revision": plan.candidate_revision,
                "diff_artifact_ref": plan.diff_artifact_ref,
                "git_head_before": plan.git_head_before,
                "changes": [change.model_dump(mode="json") for change in plan.changes],
                "mutated": False,
                "network_called": False,
            }
        )
    )


@agents.command("promote")
@guarded
def agent_promote(
    ctx: typer.Context,
    run_id: str,
    source: Annotated[Path, typer.Option("--source", help="Original source workspace.")],
    confirm_promote: Annotated[
        bool,
        typer.Option(
            "--confirm-promote",
            help="Explicitly allow the verified staging change to modify the source workspace.",
        ),
    ] = False,
):
    if not confirm_promote:
        raise ValueError("Source promotion requires --confirm-promote")
    source = source.resolve(strict=True)
    if not source.is_dir():
        raise ValueError("Promotion source must be a directory")
    artifact_root = Path(".horizon") / "artifacts"
    if not artifact_root.is_dir():
        raise NotFound("No Agent artifact store exists for promotion")
    service = HarnessService(store_for(ctx))
    promoter = WorkspacePromoter(SnapshotManager(ArtifactStore(artifact_root)))
    result = PromotionService(service, promoter).promote(run_id, source)
    assert result.promotion_intent is not None
    assert result.promotion_receipt is not None
    typer.echo(
        canonical_json(
            {
                "run_id": run_id,
                "status": result.status,
                "promotion_id": result.promotion_intent.promotion_id,
                "plan_hash": result.promotion_receipt.plan_hash,
                "source_revision_after": result.promotion_receipt.source_revision_after,
                "git_head_after": result.promotion_receipt.git_head_after,
                "recovered_after_crash": result.promotion_receipt.recovered_after_crash,
                "git_commit_created": False,
                "network_called": False,
            }
        )
    )


@agents.command("resolve-tool")
@guarded
def agent_resolve_tool(
    ctx: typer.Context,
    run_id: str,
    call_id: str,
    retry_readonly: Annotated[
        bool,
        typer.Option(
            "--retry-readonly",
            help=(
                "Explicitly cancel and conservatively charge one uncertain "
                "search_repo/read_file/retrieve_code attempt, then permit a new call from the "
                "persisted model response."
            ),
        ),
    ] = False,
    accept_write: Annotated[
        bool,
        typer.Option(
            "--accept-write",
            "--accept-replace",
            help=(
                "Accept one uncertain replace_text/apply_patch/create_file only when the live "
                "workspace "
                "exactly matches the deterministic effect derived from its pre-dispatch "
                "manifest."
            ),
        ),
    ] = False,
    rollback_write: Annotated[
        bool,
        typer.Option(
            "--rollback-write",
            "--rollback-replace",
            help=(
                "Restore one uncertain replace_text/apply_patch/create_file to its pre-dispatch "
                "manifest when the live workspace is either unchanged or exactly matches the "
                "deterministic effect. For create_file this removes only the exact expected "
                "new file."
            ),
        ),
    ] = False,
    discard_check: Annotated[
        bool,
        typer.Option(
            "--discard-check",
            help=(
                "Cancel one uncertain run_check without inferring pass/failure or replaying its "
                "recorded model turn. Requires either controller verification through --image "
                "or --confirm-check-sandbox-stopped, plus an unchanged workspace."
            ),
        ),
    ] = False,
    accept_check_result: Annotated[
        bool,
        typer.Option(
            "--accept-check-result",
            help=(
                "Recover and record one exact naturally completed run_check result from its "
                "stopped labeled Docker attempt. Requires --image; the container is removed "
                "only after the receipt is durable."
            ),
        ),
    ] = False,
    confirm_check_sandbox_stopped: Annotated[
        bool,
        typer.Option(
            "--confirm-check-sandbox-stopped",
            help=(
                "Confirm that the old validation container/process is stopped before discarding "
                "an uncertain check result."
            ),
        ),
    ] = False,
    check_image: Annotated[
        str | None,
        typer.Option(
            "--image",
            help=(
                "Existing local image used by the uncertain check; enables lookup of its "
                "deterministically labeled Docker attempt without pulling."
            ),
        ),
    ] = None,
    stop_check_sandbox: Annotated[
        bool,
        typer.Option(
            "--stop-check-sandbox",
            help=(
                "Explicitly stop only the labeled Docker attempt for this tool call when it is "
                "still running; requires --image and --discard-check."
            ),
        ),
    ] = False,
    config_path: Annotated[
        Path, typer.Option("--config", help="Provider configuration without secrets.")
    ] = Path("config/providers/siliconflow.yaml"),
    confirm_old_worker_stopped: Annotated[
        bool,
        typer.Option(
            "--confirm-old-worker-stopped",
            help=(
                "Allow takeover only after an old lease expired and its process is confirmed "
                "stopped."
            ),
        ),
    ] = False,
):
    decisions = sum(
        (retry_readonly, accept_write, rollback_write, discard_check, accept_check_result)
    )
    if decisions != 1:
        raise ValueError(
            "Choose exactly one explicit decision: --retry-readonly, --accept-write, "
            "--rollback-write, --discard-check, or --accept-check-result"
        )
    if (confirm_check_sandbox_stopped or stop_check_sandbox) and not discard_check:
        raise ValueError("Stop/confirmation options are valid only with --discard-check")
    if check_image is not None and not (discard_check or accept_check_result):
        raise ValueError("--image is valid only with a run_check recovery decision")
    if stop_check_sandbox and check_image is None:
        raise ValueError("--stop-check-sandbox requires --image")
    if accept_check_result and check_image is None:
        raise ValueError("--accept-check-result requires --image")
    if discard_check and check_image is None and not confirm_check_sandbox_stopped:
        raise ValueError(
            "Discarding a check requires --image for controller verification or "
            "--confirm-check-sandbox-stopped"
        )
    provider = load_provider_config(config_path)
    store = store_for(ctx)
    service = HarnessService(store)
    current = store.get(run_id)
    if current.status != RunStatus.RUNNING or current.plan is None:
        raise ValueError("Only a nonterminal RUNNING Agent Run can resolve a tool intent")
    if current.agent_session is None:
        raise ValueError("Run has no persisted Agent session")
    if current.model_policy is not None:
        expected_policy = ModelPolicyBinding(
            policy_id=current.task.model_policy_id,
            provider_id=provider.provider_id,
            model=provider.model.id,
            campaign_id=provider.campaign.campaign_id,
            currency=provider.pricing.currency,
            max_run_cost=provider.run_budget.max_cost,
            price_card_hash=digest(provider.pricing),
        )
        if current.model_policy != expected_policy:
            raise ValueError("Run model policy does not match the provider configuration")
    if current.task.repository.source != "local" or current.task.repository.path is None:
        raise ValueError("Tool resolution requires a local staging workspace")

    control_root = Path(".horizon")
    staging_root = (control_root / "staging").resolve(strict=True)
    workspace = Path(current.task.repository.path).resolve(strict=True)
    if not workspace.is_dir() or not workspace.is_relative_to(staging_root):
        raise ValueError("Run workspace is not inside the controlled staging root")
    ledger_path = Path(provider.ledger_path)
    if not ledger_path.is_file():
        raise NotFound("No provider campaign ledger exists for tool resolution")

    artifacts = ArtifactStore(control_root / "artifacts")
    snapshots = SnapshotManager(artifacts)
    ledger = CampaignBudgetLedger(ledger_path)
    leased = service.acquire_lease(
        run_id,
        "local-agent-tool-resolution",
        f"tool_resolution_lease_{uuid4().hex}",
        ttl_seconds=600,
        prior_worker_stopped=confirm_old_worker_stopped,
    )
    token = LeaseToken.from_run(leased)
    released = False
    check_sandbox_resolution = None
    check_sandbox_cleanup_error = None
    check_sandbox_container_name = None
    recovered_check_result = None
    try:
        RecoveryService(service, ledger, artifacts).reconcile(run_id, token)
        recovery_tools = WorkspaceToolGateway(
            workspace,
            current.task,
            active_plan_item(current),
            snapshots,
            None,
        )
        tool_recovery = ToolRecoveryService(service, artifacts, recovery_tools)
        if retry_readonly:
            snapshot, manifest_ref = snapshots.capture(
                workspace,
                allowed_paths=current.task.constraints.allowed_paths,
                denied_paths=current.task.constraints.denied_paths,
            )
            resolved = tool_recovery.authorize_readonly_retry(
                run_id,
                call_id,
                current_workspace_revision=snapshot.workspace_revision,
                current_workspace_manifest_ref=manifest_ref,
                token=token,
            )
        elif discard_check or accept_check_result:
            snapshot, manifest_ref = snapshots.capture(
                workspace,
                allowed_paths=current.task.constraints.allowed_paths,
                denied_paths=current.task.constraints.denied_paths,
            )
            if accept_check_result:
                check_id = tool_recovery.validate_check_result_recovery(
                    run_id,
                    call_id,
                    current_workspace_revision=snapshot.workspace_revision,
                    current_workspace_manifest_ref=manifest_ref,
                    token=token,
                )
                check = next(item for item in current.task.acceptance if item.id == check_id)
                assert check_image is not None
                sandbox = DockerSandbox(staging_root, check_image)
                check_sandbox_container_name = sandbox.attempt_status(call_id).container_name
                recovered_check_result = DockerAcceptanceExecutor(sandbox).recover_attempt(
                    workspace,
                    check,
                    call_id,
                )
                settled_snapshot, settled_manifest_ref = snapshots.capture(
                    workspace,
                    allowed_paths=current.task.constraints.allowed_paths,
                    denied_paths=current.task.constraints.denied_paths,
                )
                if settled_snapshot.workspace_revision != snapshot.workspace_revision:
                    raise Conflict("Workspace changed while the check result was being recovered")
                resolved = tool_recovery.accept_check_result(
                    run_id,
                    call_id,
                    recovered_check_result,
                    current_workspace_revision=settled_snapshot.workspace_revision,
                    current_workspace_manifest_ref=settled_manifest_ref,
                    token=token,
                )
                try:
                    sandbox.remove_attempt(call_id)
                    check_sandbox_resolution = "result_recorded_and_removed"
                except HorizonError as exc:
                    check_sandbox_resolution = "result_recorded_cleanup_required"
                    check_sandbox_cleanup_error = str(exc)
            else:
                tool_recovery.validate_check_discard(
                    run_id,
                    call_id,
                    current_workspace_revision=snapshot.workspace_revision,
                    current_workspace_manifest_ref=manifest_ref,
                    token=token,
                )
                sandbox_stopped = confirm_check_sandbox_stopped
                sandbox_stop_evidence = "operator_confirmation"
                if check_image is not None:
                    sandbox = DockerSandbox(staging_root, check_image)
                    attempt = sandbox.attempt_status(call_id)
                    check_sandbox_container_name = attempt.container_name
                    if attempt.state == "running":
                        if not stop_check_sandbox:
                            raise ValueError(
                                "The labeled check sandbox is still running; use "
                                "--stop-check-sandbox to stop this exact attempt"
                            )
                        attempt = sandbox.stop_attempt(call_id)
                    if attempt.state == "stopped":
                        sandbox.remove_attempt(call_id)
                        sandbox_stopped = True
                        sandbox_stop_evidence = "controller_verified"
                        check_sandbox_resolution = "controller_verified_and_removed"
                    elif not sandbox_stopped:
                        raise ValueError(
                            "No labeled check sandbox was found; its absence is not proof that "
                            "the old process stopped. Add --confirm-check-sandbox-stopped only "
                            "after operator verification."
                        )
                    else:
                        check_sandbox_resolution = "operator_confirmed_missing_attempt"
                else:
                    check_sandbox_resolution = "operator_confirmed"
                resolved = tool_recovery.discard_check_result(
                    run_id,
                    call_id,
                    current_workspace_revision=snapshot.workspace_revision,
                    current_workspace_manifest_ref=manifest_ref,
                    sandbox_stopped=sandbox_stopped,
                    token=token,
                    sandbox_stop_evidence=sandbox_stop_evidence,
                )
        else:
            resolved = tool_recovery.resolve_write(
                run_id,
                call_id,
                decision="accept" if accept_write else "rollback",
                token=token,
            )
        report = RecoveryService(service, ledger, artifacts).reconcile(run_id, token)
        resolved_record = resolved.tool_calls[-1]
        service.release_lease(run_id, token, f"tool_resolution_release_{uuid4().hex}")
        released = True
    finally:
        if not released:
            latest = store.get(run_id)
            if (
                latest.lease_id == token.lease_id
                and latest.lease_epoch == token.epoch
                and not (set(latest.reservations) - latest.unknown_reservations)
            ):
                service.release_lease(
                    run_id,
                    token,
                    f"tool_resolution_error_release_{uuid4().hex}",
                )
    typer.echo(
        canonical_json(
            {
                **report.model_dump(mode="json"),
                "decision": resolved_record.recovery_disposition,
                "resolved_tool_call_id": resolved_record.call_id,
                "resolved_tool": resolved_record.name,
                "workspace_revision": resolved_record.workspace_revision_after,
                "check_sandbox_resolution": check_sandbox_resolution,
                "check_sandbox_cleanup_error": check_sandbox_cleanup_error,
                "check_sandbox_container_name": check_sandbox_container_name,
                "recovered_check_result": (
                    recovered_check_result.model_dump(mode="json")
                    if recovered_check_result is not None
                    else None
                ),
                "network_called": False,
                "paid_model_called": False,
            }
        )
    )


@app.command()
@guarded
def replay(ctx: typer.Context, run_id: str, at: Annotated[int | None, typer.Option(min=1)] = None):
    run = store_for(ctx, read_only=True).get(run_id, at)
    typer.echo(canonical_json({"state": run.as_dict(), "projection_hash": projection_hash(run)}))


@app.command()
@guarded
def doctor():
    with sqlite3.connect(":memory:") as db:
        try:
            db.execute("CREATE VIRTUAL TABLE probe USING fts5(content)")
            fts5 = True
        except sqlite3.OperationalError:
            fts5 = False
    typer.echo(
        canonical_json(
            {
                "horizon": __version__,
                "python": platform.python_version(),
                "sqlite": sqlite3.sqlite_version,
                "fts5": fts5,
                "model_backend": "openai_compatible",
                "autonomous_execution": True,
                "execution_profile": "bounded_sequential_work_item_dag",
                "multi_work_item_execution": True,
                "work_item_scheduler": "deterministic_dependency_ready_plan_order",
                "atomic_work_item_handoff": True,
                "parallel_work_items": False,
                "automatic_plan_generation": True,
                "automatic_plan_profile": "one_shot_validated_work_item_dag",
                "max_generated_work_items": 8,
                "planning_response_recovery": True,
                "planning_human_fallback": True,
                "planning_human_fallback_profile": "invalid_proposal_manual_plan",
                "no_progress_human_guidance": True,
                "no_progress_human_guidance_profile": "bounded_local_session_guidance",
                "general_hitl": False,
                "automatic_plan_retry": False,
                "dynamic_replanning": True,
                "dynamic_replanning_profile": "single_atomic_execution_evidence_revision",
                "max_execution_replans": MAX_EXECUTION_REPLANS,
                "automatic_replan_trigger": False,
                "completed_work_items_immutable_during_replan": True,
                "no_progress_detection": True,
                "no_progress_profile": "exact_action_patterns_same_revision",
                "no_progress_patterns": [
                    "identical_action",
                    "alternating_two_action_cycle",
                ],
                "no_progress_repeat_limit": 2,
                "no_progress_alternating_cycle_limit": 2,
                "semantic_no_progress_detection": False,
                "controller_policy_evaluation": True,
                "controller_policy_evaluation_profile": "frozen_offline_exact_policy_traces",
                "controller_policy_evaluation_external_calls": False,
                "full_run_ab_evaluation": True,
                "full_run_ab_evaluation_profile": "scripted_model_replayable_docker_validation",
                "full_run_ab_paid_model_called": False,
                "full_run_ab_initial_failure_preflight": True,
                "source_bound_run_ab_suite": True,
                "source_bound_run_ab_suite_profile": ("external_reduced_and_clean_full_checkout"),
                "source_bound_run_ab_full_checkout_gate": True,
                "bounded_multi_file_patch": True,
                "max_patch_files": 8,
                "patch_recovery": "exact_pre_or_expected_effect",
                "safe_turn_continuation": True,
                "response_receipt_continuation": True,
                "campaign_only_hold_recovery": True,
                "run_linked_call_reconciliation": True,
                "readonly_tool_retry_resolution": True,
                "replace_tool_accept_rollback": True,
                "write_tool_recovery": True,
                "run_check_attempt_identity": True,
                "run_check_attempt_status_query": True,
                "run_check_stopped_result_recovery": True,
                "run_check_stopped_result_recovery_profile": (
                    "exact_natural_exit_bounded_complete_log"
                ),
                "run_check_receipt_before_cleanup": True,
                "run_check_running_attempt_stop_requires_explicit": True,
                "run_check_missing_attempt_proof": False,
                "run_check_signal_timeout_result_recovery": False,
                "portfolio_hard_crash_demo": True,
                "portfolio_hard_crash_demo_profile": ("replace_effect_before_receipt_exact_accept"),
                "recovery_matrix_evaluation": True,
                "recovery_matrix_evaluation_profile": (
                    "one_real_crash_plus_exact_write_state_decisions"
                ),
                "recovery_matrix_case_count": 19,
                "complete_fault_injection_matrix": False,
                "arbitrary_crash_recovery": False,
                "promotion_enabled": True,
                "promotion_profile": "explicit_bounded_existing_files",
                "max_promotion_files": 8,
                "partial_promotion_recovery": True,
                "git_head_binding": True,
                "deterministic_context_projection": True,
                "context_projection_artifacts": True,
                "canonical_transcript_retained": True,
                "mandatory_fact_ledger": True,
                "mandatory_fact_artifacts": True,
                "tool_schema_fact_binding": True,
                "token_aware_compaction": False,
                "semantic_compaction": False,
                "memory_enabled": True,
                "memory_profile": "evidence_backed_run_projection",
                "run_memory_enabled": True,
                "project_memory_enabled": False,
                "model_claim_memory_promotion": False,
                "memory_revision_invalidation": True,
                "code_rag_enabled": True,
                "code_rag_profile": "revision_bound_lexical",
                "retrieval_fts5_with_scan_fallback": True,
                "fixed_retrieval_diagnostic": True,
                "vector_retrieval": False,
            }
        )
    )
