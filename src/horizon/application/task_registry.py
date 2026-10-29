from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from horizon.domain.common import digest
from horizon.domain.errors import IntegrityError
from horizon.domain.evaluation import RetrievalSourceManifest
from horizon.domain.run_evaluation import RunABEvalManifest, RunABSuiteManifest
from horizon.domain.task_registry import (
    IndependentTaskPreflightCase,
    IndependentTaskRegistry,
    IndependentTaskRegistryPreflightReport,
)


def _ratio_counts(tasks: tuple[IndependentTaskPreflightCase, ...], field: str, value: str) -> int:
    return sum(getattr(task, field) == value for task in tasks)


def _read_yaml_mapping(path: Path) -> Mapping[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise IntegrityError(f"Evidence YAML must contain a mapping: {path}")
    return raw


def _resolve_evidence(root: Path, relative: str) -> Path:
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root):
        raise IntegrityError("Task-registry evidence escapes the benchmark root")
    return path


class IndependentTaskRegistryPreflight:
    """Cross-check a frozen task registry without model, network, or repository execution."""

    def evaluate(
        self,
        registry: IndependentTaskRegistry,
        *,
        registry_path: Path,
    ) -> IndependentTaskRegistryPreflightReport:
        benchmark_root = registry_path.resolve(strict=True).parent
        selection_path = _resolve_evidence(benchmark_root, registry.selection.manifest_path)
        selection = _read_yaml_mapping(selection_path)
        selection_digest = digest(selection)
        if selection_digest != registry.selection.manifest_digest:
            raise IntegrityError("Frozen task selection digest does not match the registry")
        protocol = selection.get("protocol")
        if not isinstance(protocol, Mapping) or protocol.get("source_dataset") != (
            registry.source_dataset
        ):
            raise IntegrityError("Registry source dataset does not match the frozen selection")

        selected_cases = selection.get("cases")
        if not isinstance(selected_cases, list) or any(
            not isinstance(case, Mapping) for case in selected_cases
        ):
            raise IntegrityError("Frozen task selection has invalid cases")
        selected_by_id = {str(case.get("case_id")): case for case in selected_cases}
        registry_ids = {task.task_id for task in registry.tasks}
        if len(selected_by_id) != len(selected_cases) or set(selected_by_id) != registry_ids:
            raise IntegrityError("Registry must preserve every frozen selection case exactly once")

        licenses = {item.license_id: item for item in registry.licenses}
        source_cache: dict[Path, RetrievalSourceManifest] = {}
        suite_cache: dict[Path, RunABSuiteManifest] = {}
        results: list[IndependentTaskPreflightCase] = []

        for task in registry.tasks:
            selected = selected_by_id[task.task_id]
            expected_selection = {
                "partition": task.partition,
                "project": task.project,
                "bug_id": task.bug_id,
                "repository_url": task.repository_url.rstrip("/"),
                "buggy_commit": task.buggy_commit,
                "fixed_commit": task.fixed_commit,
                "failing_tests": list(task.failing_tests),
            }
            observed_selection = {
                "partition": selected.get("partition"),
                "project": selected.get("project"),
                "bug_id": str(selected.get("bug_id")),
                "repository_url": str(selected.get("repository_url", "")).rstrip("/"),
                "buggy_commit": selected.get("buggy_commit"),
                "fixed_commit": selected.get("fixed_commit"),
                "failing_tests": selected.get("failing_tests"),
            }
            if observed_selection != expected_selection:
                raise IntegrityError(f"Registry task drifted from frozen selection: {task.task_id}")

            evidence_refs = [registry.selection.manifest_path]
            source_readiness = "metadata_only"
            source_ref = task.evidence.source_recipe_manifest
            if source_ref is not None:
                source_path = _resolve_evidence(benchmark_root, source_ref)
                source_manifest = source_cache.get(source_path)
                if source_manifest is None:
                    source_manifest = RetrievalSourceManifest.model_validate(
                        _read_yaml_mapping(source_path)
                    )
                    source_cache[source_path] = source_manifest
                source_case = next(
                    (case for case in source_manifest.cases if case.case_id == task.task_id),
                    None,
                )
                if source_case is None:
                    raise IntegrityError(f"Source recipe omits registry task: {task.task_id}")
                if (
                    source_manifest.partition != task.partition
                    or source_case.case_id != task.task_id
                    or source_case.repository_url.rstrip("/") != task.repository_url.rstrip("/")
                    or source_case.buggy_commit != task.buggy_commit
                ):
                    raise IntegrityError(f"Source recipe drifted for registry task: {task.task_id}")
                source_readiness = "frozen_materialization_recipe"
                evidence_refs.append(source_ref)

            execution_readiness = "not_prepared"
            initial_failure_evidence = "benchmark_declaration_only"
            suite_ref = task.evidence.execution_suite_manifest
            if suite_ref is not None:
                suite_path = _resolve_evidence(benchmark_root, suite_ref)
                suite = suite_cache.get(suite_path)
                if suite is None:
                    suite = RunABSuiteManifest.model_validate(_read_yaml_mapping(suite_path))
                    suite_cache[suite_path] = suite
                suite_case = next(
                    (
                        case
                        for case in suite.cases
                        if case.case_id == task.evidence.execution_case_id
                    ),
                    None,
                )
                if suite_case is None:
                    raise IntegrityError(f"Execution suite omits registry task: {task.task_id}")
                source = suite_case.source
                license_record = licenses[task.license_id]
                expected_license_url = license_record.url_for(task.buggy_commit)
                upstream_tests = tuple(
                    test.strip() for test in source.upstream_test.split(";") if test.strip()
                )
                if (
                    source.benchmark != registry.source_dataset
                    or source.project != task.project
                    or source.bug_id != task.bug_id
                    or source.repository_url.rstrip("/") != task.repository_url.rstrip("/")
                    or source.buggy_commit != task.buggy_commit
                    or source.fixed_commit != task.fixed_commit
                    or not upstream_tests
                    or any(test not in task.failing_tests for test in upstream_tests)
                    or source.license_spdx != license_record.spdx
                    or source.license_url != expected_license_url
                ):
                    raise IntegrityError(
                        f"Execution evidence drifted for registry task: {task.task_id}"
                    )
                case_path = _resolve_evidence(suite_path.parent, suite_case.manifest_path)
                case_manifest = RunABEvalManifest.model_validate(_read_yaml_mapping(case_path))
                suite_case.check_manifest(case_manifest)
                execution_readiness = f"{source.reduction}_manifest"
                initial_failure_evidence = (
                    "public_historical_run_declared"
                    if task.evidence.historical_run_url is not None
                    else "benchmark_declaration_only"
                )
                evidence_refs.extend((suite_ref, case_path.relative_to(benchmark_root).as_posix()))
                if task.evidence.historical_run_url is not None:
                    evidence_refs.append(task.evidence.historical_run_url)

            license_record = licenses[task.license_id]
            results.append(
                IndependentTaskPreflightCase(
                    task_id=task.task_id,
                    partition=task.partition,
                    project=task.project,
                    bug_id=task.bug_id,
                    repository_url=task.repository_url,
                    buggy_commit=task.buggy_commit,
                    fixed_commit=task.fixed_commit,
                    license_spdx=license_record.spdx,
                    license_url=license_record.url_for(task.buggy_commit),
                    source_readiness=source_readiness,
                    initial_failure_evidence=initial_failure_evidence,
                    execution_readiness=execution_readiness,
                    evidence_refs=tuple(evidence_refs),
                )
            )

        task_results = tuple(results)
        return IndependentTaskRegistryPreflightReport(
            registry_id=registry.registry_id,
            manifest_digest=registry.sha256,
            selection_manifest_digest=selection_digest,
            task_count=len(task_results),
            unique_source_task_count=len(
                {
                    (task.repository_url.rstrip("/").casefold(), task.bug_id.casefold())
                    for task in task_results
                }
            ),
            development_task_count=_ratio_counts(task_results, "partition", "development"),
            holdout_task_count=_ratio_counts(task_results, "partition", "holdout"),
            frozen_source_recipe_count=_ratio_counts(
                task_results, "source_readiness", "frozen_materialization_recipe"
            ),
            metadata_only_source_count=_ratio_counts(
                task_results, "source_readiness", "metadata_only"
            ),
            public_historical_initial_failure_count=_ratio_counts(
                task_results,
                "initial_failure_evidence",
                "public_historical_run_declared",
            ),
            benchmark_declared_initial_failure_count=_ratio_counts(
                task_results,
                "initial_failure_evidence",
                "benchmark_declaration_only",
            ),
            execution_manifest_count=sum(
                task.execution_readiness != "not_prepared" for task in task_results
            ),
            full_checkout_manifest_count=_ratio_counts(
                task_results, "execution_readiness", "full_checkout_manifest"
            ),
            dependency_reduced_manifest_count=_ratio_counts(
                task_results, "execution_readiness", "dependency_reduced_manifest"
            ),
            not_prepared_execution_count=_ratio_counts(
                task_results, "execution_readiness", "not_prepared"
            ),
            claim_scope=(
                "Offline structural baseline for 24 unique frozen BugsInPy task identities, "
                "license metadata bindings, source recipes, and existing execution manifests."
            ),
            excluded_claims=(
                "All registered repositories are currently materialized locally.",
                "All initial failures were re-executed by this preflight.",
                "All registered tasks have executable Agent TaskSpecs.",
                "Any registered task was solved by a paid or real model in this preflight.",
                "Historical GitHub Run status was reverified online by this preflight.",
                "Every license file and SPDX term was fetched and independently audited.",
                "Holdout patch content or gold production paths were inspected.",
            ),
            tasks=task_results,
        )
