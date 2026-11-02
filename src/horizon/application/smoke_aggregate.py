from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from horizon.domain.common import canonical_json
from horizon.domain.errors import IntegrityError
from horizon.domain.smoke_evaluation import (
    RealModelSmokeAggregateReport,
    RealModelSmokeRunResult,
    RealModelSmokeSuite,
    SmokeMetricTally,
    SmokeRunResultReference,
    SmokeStopCategoryCount,
)


@dataclass(frozen=True)
class SmokeAggregateArtifacts:
    output_dir: Path
    report_path: Path
    summary_path: Path
    report_ref: str
    report: RealModelSmokeAggregateReport


def _tally(values: list[bool | None]) -> SmokeMetricTally:
    return SmokeMetricTally(
        true_count=sum(value is True for value in values),
        false_count=sum(value is False for value in values),
        unknown_count=sum(value is None for value in values),
    )


def _summary(report: RealModelSmokeAggregateReport) -> str:
    stop_counts = "\n".join(
        f"- `{item.category}`: `{item.run_count}`" for item in report.stop_category_counts
    )
    missing = "\n".join(f"- `{task_id}`" for task_id in report.unevaluated_task_ids)
    if not missing:
        missing = "- none"
    return (
        "# Real-model Smoke Aggregate\n\n"
        f"- Suite: `{report.suite_id}`\n"
        f"- Coverage: `{report.evaluated_task_count}/{report.suite_task_count}` "
        f"(`{report.suite_coverage_rate:.3f}`)\n"
        f"- Success among evaluated tasks: "
        f"`{report.task_success_count}/{report.evaluated_task_count}` "
        f"(`{report.task_success_rate_evaluated:.3f}`)\n"
        f"- Settled model calls: `{report.total_model_calls}`\n"
        f"- Settled CNY price-card cost: `{report.total_actual_cost_cny}`\n\n"
        "## Metric tallies (true / false / unknown)\n\n"
        f"- Correct location: `{report.correct_location.true_count} / "
        f"{report.correct_location.false_count} / {report.correct_location.unknown_count}`\n"
        f"- Valid edit: `{report.valid_edit.true_count} / {report.valid_edit.false_count} / "
        f"{report.valid_edit.unknown_count}`\n"
        f"- Protected validation: `{report.protected_validation.true_count} / "
        f"{report.protected_validation.false_count} / "
        f"{report.protected_validation.unknown_count}`\n\n"
        "## Stop categories\n\n"
        f"{stop_counts}\n\n"
        "## Not yet evaluated\n\n"
        f"{missing}\n\n"
        "The success rate above uses evaluated tasks only. Missing tasks remain explicit and are "
        "not counted as either successes or failures.\n"
    )


def aggregate_real_model_smoke_results(
    suite: RealModelSmokeSuite,
    result_paths: tuple[Path, ...],
    *,
    output_dir: Path,
) -> SmokeAggregateArtifacts:
    """Aggregate existing result files without executing or reclassifying any Run."""

    if not result_paths:
        raise ValueError("At least one smoke result is required")
    results: list[tuple[Path, bytes, RealModelSmokeRunResult]] = []
    seen_tasks: set[str] = set()
    seen_runs: set[str] = set()
    suite_task_ids = tuple(task.task_id for task in suite.tasks)
    for path in result_paths:
        resolved = path.resolve(strict=True)
        content = resolved.read_bytes()
        try:
            result = RealModelSmokeRunResult.model_validate_json(content)
        except ValueError as exc:
            raise IntegrityError(f"Invalid smoke result: {resolved}") from exc
        if result.suite_id != suite.suite_id or result.suite_digest != suite.sha256:
            raise IntegrityError("Smoke result does not match the frozen suite")
        if result.smoke_task_id not in suite_task_ids:
            raise IntegrityError("Smoke result names a task outside the frozen suite")
        if result.smoke_task_id in seen_tasks or result.run_id in seen_runs:
            raise IntegrityError("Smoke aggregate cannot contain duplicate tasks or Runs")
        seen_tasks.add(result.smoke_task_id)
        seen_runs.add(result.run_id)
        results.append((resolved, content, result))

    ordered = sorted(results, key=lambda item: suite_task_ids.index(item[2].smoke_task_id))
    result_values = [item[2] for item in ordered]
    stop_counts = Counter(result.stop_category for result in result_values)
    references = tuple(
        SmokeRunResultReference(
            smoke_task_id=result.smoke_task_id,
            run_id=result.run_id,
            result_path=str(path),
            result_sha256=hashlib.sha256(content).hexdigest(),
            trace_sha256=result.trace_sha256,
            terminal_evidence_pack_sha256=result.terminal_evidence_pack_sha256,
        )
        for path, content, result in ordered
    )
    evaluated = len(result_values)
    success_count = sum(result.task_success for result in result_values)
    report = RealModelSmokeAggregateReport(
        suite_id=suite.suite_id,
        suite_digest=suite.sha256,
        evaluated_task_count=evaluated,
        unevaluated_task_ids=tuple(
            task_id for task_id in suite_task_ids if task_id not in seen_tasks
        ),
        suite_coverage_rate=evaluated / len(suite_task_ids),
        task_success_count=success_count,
        task_success_rate_evaluated=success_count / evaluated,
        correct_location=_tally([result.correct_location_reached for result in result_values]),
        valid_edit=_tally([result.valid_edit_produced for result in result_values]),
        protected_validation=_tally(
            [result.protected_validation_passed for result in result_values]
        ),
        total_actual_cost_cny=sum(
            (result.actual_cost_cny for result in result_values), Decimal("0")
        ),
        total_model_calls=sum(result.model_calls for result in result_values),
        total_unknown_model_calls=sum(result.unknown_model_calls for result in result_values),
        stop_category_counts=tuple(
            SmokeStopCategoryCount(category=category, run_count=count)
            for category, count in sorted(stop_counts.items())
        ),
        results=references,
        limitations=(
            "Rates are computed over evaluated tasks only; suite coverage and missing task IDs "
            "remain explicit.",
            "The aggregator verifies result schemas, suite binding, uniqueness, and result-file "
            "hashes; it does not re-execute Runs, providers, repositories, or protected checks.",
            "Each child result retains its own evaluator-known development and price-card cost "
            "boundaries.",
        ),
    )
    destination = output_dir.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    report_content = (canonical_json(report) + "\n").encode("utf-8")
    summary_content = _summary(report).encode("utf-8")
    report_path = destination / "smoke-aggregate.json"
    summary_path = destination / "SUMMARY.md"
    with report_path.open("xb") as stream:
        stream.write(report_content)
    with summary_path.open("xb") as stream:
        stream.write(summary_content)
    return SmokeAggregateArtifacts(
        output_dir=destination,
        report_path=report_path,
        summary_path=summary_path,
        report_ref=hashlib.sha256(report_content).hexdigest(),
        report=report,
    )
