from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import yaml

from horizon.adapters.model.config import ProviderConfig
from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.adapters.vcs.git import verify_clean_git_checkout
from horizon.adapters.workspace.snapshot import SnapshotManager
from horizon.domain.common import canonical_json, digest
from horizon.domain.errors import Conflict, IntegrityError, PolicyDenied
from horizon.domain.model import CampaignSummary
from horizon.domain.pilot import RealModelPilotManifest, RealModelPilotPreflightReport
from horizon.domain.ports import AcceptanceExecutorPort
from horizon.domain.task import TaskSpec


@dataclass(frozen=True)
class PilotPreflightArtifacts:
    report: RealModelPilotPreflightReport
    report_ref: str
    report_path: Path
    prepared_task_path: Path


def _artifact_path(store: ArtifactStore, reference: str) -> Path:
    return store.root / reference[:2] / reference


def _private_values(manifest: RealModelPilotManifest) -> tuple[str, ...]:
    return (
        manifest.source.fixed_commit,
        manifest.source.fix_url,
        *manifest.solution_isolation.forbidden_task_literals,
    )


def _solution_isolated(task: TaskSpec, manifest: RealModelPilotManifest) -> bool:
    visible = canonical_json(task).casefold()
    return not any(value.casefold() in visible for value in _private_values(manifest))


def compute_harness_source_digest(package_root: Path | None = None) -> str:
    """Fingerprint the loaded Horizon Python sources without including runtime state."""

    root = (package_root or Path(__file__).resolve().parents[1]).resolve(strict=True)
    if not root.is_dir():
        raise IntegrityError("Harness package root must be a directory")
    entries: list[dict[str, str | int]] = []
    for path in sorted(root.rglob("*.py")):
        if path.is_symlink() or not path.is_file():
            raise IntegrityError("Harness source fingerprint rejects linked Python files")
        payload = path.read_bytes()
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
        )
    if not entries:
        raise IntegrityError("Harness source fingerprint found no Python files")
    return digest(entries)


class PilotPreflightService:
    """Prove a real-model pilot is source-bound, initially failing, and solution-blind."""

    def __init__(
        self,
        acceptance: AcceptanceExecutorPort,
        *,
        validation_backend_ref: str,
    ):
        self.acceptance = acceptance
        self.validation_backend_ref = validation_backend_ref

    def evaluate(
        self,
        manifest: RealModelPilotManifest,
        *,
        source: Path,
        state_dir: Path,
        provider: ProviderConfig,
        campaign: CampaignSummary,
    ) -> PilotPreflightArtifacts:
        source = source.resolve(strict=True)
        if not source.is_dir():
            raise PolicyDenied("Pilot source must be a directory")
        state_dir = state_dir.resolve()
        if state_dir == source or state_dir.is_relative_to(source):
            raise PolicyDenied("Pilot state directory cannot live inside the source checkout")
        if provider.policy_id != manifest.provider_policy_id:
            raise Conflict("Pilot manifest does not match the provider policy")
        if not provider.model.supports_tools:
            raise PolicyDenied("Pilot model must support tool calling")
        if provider.pricing.currency != manifest.currency:
            raise Conflict("Pilot manifest and provider currency do not match")
        if provider.run_budget.max_cost != manifest.max_paid_cost:
            raise Conflict("Provider Run cost cap must exactly match the pilot cap")
        if campaign.campaign_id != provider.campaign.campaign_id:
            raise Conflict("Campaign summary does not match the provider campaign")
        if campaign.currency != manifest.currency:
            raise Conflict("Campaign summary and pilot currency do not match")

        verify_clean_git_checkout(source, manifest.source.buggy_commit)
        state_dir.mkdir(parents=True, exist_ok=True)
        artifacts = ArtifactStore(state_dir / "artifacts")
        snapshots = SnapshotManager(artifacts)
        source_snapshot, source_manifest_ref = snapshots.capture(
            source,
            denied_paths=manifest.task.constraints.denied_paths,
        )

        staging_root = state_dir / "staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        workspace = staging_root / f"preflight-{uuid4().hex}"
        snapshots.restore(source_manifest_ref, workspace)
        initial_validation = tuple(
            self.acceptance.execute(workspace, check) for check in manifest.task.acceptance
        )

        verify_clean_git_checkout(source, manifest.source.buggy_commit)
        source_after, _ = snapshots.capture(
            source,
            denied_paths=manifest.task.constraints.denied_paths,
        )
        source_unchanged = source_after.workspace_revision == source_snapshot.workspace_revision

        task_data = manifest.task.model_dump(mode="json")
        task_data["repository"] = {
            "source": "local",
            "path": str(source),
            "base_commit": manifest.source.buggy_commit,
        }
        prepared_task = TaskSpec.model_validate(task_data)
        solution_isolation_passed = _solution_isolated(prepared_task, manifest)
        if not solution_isolation_passed:
            raise PolicyDenied("Prepared TaskSpec contains private solution information")
        task_payload = yaml.safe_dump(
            prepared_task.model_dump(mode="json"),
            allow_unicode=True,
            sort_keys=False,
        ).encode("utf-8")
        prepared_task_ref = artifacts.put(task_payload)
        if artifacts.read(prepared_task_ref) != task_payload:
            raise IntegrityError("Prepared pilot TaskSpec failed artifact verification")

        observed_failures = tuple(
            sorted(result.check_id for result in initial_validation if not result.passed)
        )
        expected_failures = tuple(sorted(manifest.expected_initial_failed_checks))
        initial_failure_matches = observed_failures == expected_failures
        git_metadata_excluded = not any(
            entry.path == ".git" or entry.path.startswith(".git/")
            for entry in source_snapshot.files
        ) and any(path == ".git/" for path in source_snapshot.excluded_paths)
        full_checkout_verified = True
        ready = all(
            (
                source_unchanged,
                full_checkout_verified,
                git_metadata_excluded,
                initial_failure_matches,
                solution_isolation_passed,
                campaign.remaining_cost >= manifest.max_paid_cost,
            )
        )
        report = RealModelPilotPreflightReport(
            pilot_id=manifest.pilot_id,
            manifest_digest=manifest.sha256,
            source=manifest.source,
            source_git_head=manifest.source.buggy_commit,
            source_snapshot_revision=source_snapshot.workspace_revision,
            source_manifest_ref=source_manifest_ref,
            source_unchanged=source_unchanged,
            full_checkout_verified=full_checkout_verified,
            git_metadata_excluded=git_metadata_excluded,
            prepared_task_digest=prepared_task.sha256,
            prepared_task_ref=prepared_task_ref,
            provider_config_digest=digest(provider),
            harness_source_digest=compute_harness_source_digest(),
            provider_policy_id=provider.policy_id,
            provider_id=provider.provider_id,
            model_id=provider.model.id,
            validation_backend_ref=self.validation_backend_ref,
            currency=manifest.currency,
            run_cost_cap=manifest.max_paid_cost,
            campaign_remaining_cost=campaign.remaining_cost,
            max_model_calls=prepared_task.budgets.max_model_calls,
            initial_validation=initial_validation,
            expected_initial_failed_checks=expected_failures,
            observed_initial_failed_checks=observed_failures,
            initial_failure_matches=initial_failure_matches,
            forbidden_literal_hashes=tuple(
                digest(value) for value in manifest.solution_isolation.forbidden_task_literals
            ),
            solution_isolation_passed=solution_isolation_passed,
            ready=ready,
        )
        report_payload = canonical_json(report).encode("utf-8")
        report_ref = artifacts.put(report_payload)
        if artifacts.read(report_ref) != report_payload:
            raise IntegrityError("Pilot preflight report failed artifact verification")
        return PilotPreflightArtifacts(
            report=report,
            report_ref=report_ref,
            report_path=_artifact_path(artifacts, report_ref),
            prepared_task_path=_artifact_path(artifacts, prepared_task_ref),
        )


def load_pilot_preflight_report(path: Path) -> tuple[RealModelPilotPreflightReport, str]:
    content = path.resolve(strict=True).read_bytes()
    reference = hashlib.sha256(content).hexdigest()
    if path.name != reference:
        raise IntegrityError("Pilot preflight must be the original content-addressed artifact")
    try:
        report = RealModelPilotPreflightReport.model_validate_json(content)
    except ValueError as exc:
        raise IntegrityError("Pilot preflight report is invalid") from exc
    return report, reference


def verify_pilot_launch_binding(
    report: RealModelPilotPreflightReport,
    *,
    task: TaskSpec,
    provider: ProviderConfig,
    source_git_head: str | None,
    source_snapshot_revision: str,
    validation_backend_ref: str,
    current_harness_source_digest: str | None = None,
) -> None:
    if not report.ready:
        raise PolicyDenied("Pilot preflight did not pass every readiness gate")
    if task.sha256 != report.prepared_task_digest:
        raise Conflict("Pilot TaskSpec changed after preflight")
    if digest(provider) != report.provider_config_digest:
        raise Conflict("Pilot provider policy changed after preflight")
    if report.harness_source_digest is None:
        raise PolicyDenied(
            "Pilot preflight predates Harness source binding; regenerate it before launch"
        )
    current_harness_source_digest = current_harness_source_digest or compute_harness_source_digest()
    if current_harness_source_digest != report.harness_source_digest:
        raise Conflict("Harness source changed after pilot preflight")
    if source_git_head != report.source_git_head:
        raise Conflict("Pilot source Git commit changed after preflight")
    if source_snapshot_revision != report.source_snapshot_revision:
        raise Conflict("Pilot source content changed after preflight")
    if validation_backend_ref != report.validation_backend_ref:
        raise Conflict("Pilot validation image changed after preflight")
    if task.model_policy_id != report.provider_policy_id:
        raise Conflict("Pilot TaskSpec no longer names the preflighted provider policy")
    if task.budgets.max_model_calls != report.max_model_calls:
        raise Conflict("Pilot model-call cap changed after preflight")
    if provider.run_budget.max_cost != report.run_cost_cap:
        raise Conflict("Pilot Run cost cap changed after preflight")
