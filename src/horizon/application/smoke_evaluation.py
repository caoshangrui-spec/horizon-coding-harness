from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from horizon.application.task_registry import IndependentTaskRegistryPreflight
from horizon.domain.errors import IntegrityError
from horizon.domain.smoke_evaluation import (
    RealModelSmokePreflightReport,
    RealModelSmokeSuite,
    SmokePreflightTask,
)
from horizon.domain.task_registry import IndependentTaskRegistry


def _read_yaml_mapping(path: Path) -> Mapping[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise IntegrityError(f"Smoke evaluation YAML must contain a mapping: {path}")
    return raw


def _resolve_manifest(root: Path, relative: str) -> Path:
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root):
        raise IntegrityError("Smoke-suite reference escapes the benchmark root")
    return path


class RealModelSmokePreflight:
    """Validate a frozen five-task smoke contract without executing or paying for it."""

    def evaluate(
        self,
        suite: RealModelSmokeSuite,
        *,
        suite_path: Path,
    ) -> RealModelSmokePreflightReport:
        benchmark_root = suite_path.resolve(strict=True).parent
        registry_path = _resolve_manifest(benchmark_root, suite.registry_path)
        registry = IndependentTaskRegistry.model_validate(_read_yaml_mapping(registry_path))
        if registry.sha256 != suite.registry_digest:
            raise IntegrityError("Smoke-suite registry digest does not match the frozen registry")

        registry_report = IndependentTaskRegistryPreflight().evaluate(
            registry,
            registry_path=registry_path,
        )
        registry_tasks = {task.task_id: task for task in registry_report.tasks}
        if not set(suite.selection_policy.excluded_task_ids) <= set(registry_tasks):
            raise IntegrityError("Smoke-suite exclusion list names an unknown registry task")

        selected: list[SmokePreflightTask] = []
        for contract in suite.tasks:
            evidence = registry_tasks.get(contract.task_id)
            if evidence is None:
                raise IntegrityError(f"Smoke task is absent from the registry: {contract.task_id}")
            if contract.task_id in suite.selection_policy.excluded_task_ids:
                raise IntegrityError(f"Smoke task is explicitly excluded: {contract.task_id}")
            if evidence.partition != suite.selection_policy.required_partition:
                raise IntegrityError(f"Smoke task is not in development: {contract.task_id}")
            if evidence.source_readiness != suite.selection_policy.required_source_readiness:
                raise IntegrityError(f"Smoke task lacks a frozen source recipe: {contract.task_id}")
            if evidence.execution_readiness != (
                suite.selection_policy.required_execution_readiness
            ):
                raise IntegrityError(
                    f"Smoke task lacks a full-checkout execution manifest: {contract.task_id}"
                )
            if (
                suite.selection_policy.require_public_historical_initial_failure
                and evidence.initial_failure_evidence != "public_historical_run_declared"
            ):
                raise IntegrityError(
                    f"Smoke task lacks declared public initial-failure evidence: {contract.task_id}"
                )
            selected.append(
                SmokePreflightTask(
                    task_id=contract.task_id,
                    project=evidence.project,
                    difficulty_band=contract.difficulty_band,
                    allowed_path_patterns=contract.allowed_path_patterns,
                    allowed_tools=contract.allowed_tools,
                )
            )

        unique_projects = {task.project.casefold() for task in selected}
        if len(unique_projects) != suite.selection_policy.required_unique_projects:
            raise IntegrityError("Smoke-suite tasks must come from five distinct projects")

        policy = suite.execution_policy
        return RealModelSmokePreflightReport(
            suite_id=suite.suite_id,
            manifest_digest=suite.sha256,
            registry_id=registry.registry_id,
            registry_digest=registry.sha256,
            success_criteria=suite.success_criteria,
            required_metrics=suite.required_metrics,
            failure_taxonomy=suite.failure_taxonomy,
            max_steps=policy.max_steps,
            max_model_calls=policy.max_model_calls,
            max_tool_calls=policy.max_tool_calls,
            max_wall_time_seconds=policy.max_wall_time_seconds,
            max_repair_cycles=policy.max_repair_cycles,
            max_execution_replans=policy.max_execution_replans,
            launch_blockers=(
                "Bind an exact provider policy without implicit retry or fallback.",
                "Bind an exact model identifier and re-run per-task zero-cost preflight.",
                "Obtain a new explicit per-run and cumulative Campaign CNY cost authorization.",
            ),
            claim_scope=(
                "Offline structural preflight for five author-selected development smoke tasks "
                "with frozen source recipes, full-checkout manifests, and declared public "
                "initial-failure evidence."
            ),
            excluded_claims=(
                "A provider, model, per-run budget, or Campaign budget is authorized.",
                "Any third-party checkout was materialized or executed by this preflight.",
                "Any task was solved by a real model or Scripted Model in this preflight.",
                "The five author-selected development tasks estimate benchmark generalization.",
                "Historical GitHub Run status was reverified online by this preflight.",
                "Evaluator-only scripted repair actions may be shown to a future model.",
            ),
            tasks=tuple(selected),
        )
