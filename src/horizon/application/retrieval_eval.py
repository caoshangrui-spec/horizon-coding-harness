from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from horizon.adapters.retrieval.sqlite_fts import SQLiteCodeRetriever
from horizon.domain.common import digest
from horizon.domain.errors import IntegrityError
from horizon.domain.evaluation import (
    RetrievalEvalAtK,
    RetrievalEvalCaseResult,
    RetrievalEvalManifest,
    RetrievalEvalReport,
    RetrievalEvalSuiteCaseResult,
    RetrievalEvalSuiteManifest,
    RetrievalEvalSuiteReport,
)


def _ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round(numerator / denominator, 6)


class RetrievalEvaluator:
    """Run deterministic retrieval diagnostics without a model or network access."""

    def __init__(self, retriever: SQLiteCodeRetriever):
        self.retriever = retriever

    def evaluate(
        self,
        manifest: RetrievalEvalManifest,
        *,
        source_manifest_ref: str,
        workspace_revision: str,
    ) -> RetrievalEvalReport:
        results: list[RetrievalEvalCaseResult] = []
        scope_hash: str | None = None
        index_key: str | None = None

        for case in manifest.cases:
            pack = self.retriever.retrieve(
                source_manifest_ref=source_manifest_ref,
                workspace_revision=workspace_revision,
                allowed_paths=manifest.allowed_paths,
                denied_paths=manifest.denied_paths,
                query=case.query,
                max_chunks=case.max_chunks,
            )
            if scope_hash is None:
                scope_hash = pack.scope_hash
                index_key = pack.index_key
            elif scope_hash != pack.scope_hash or index_key != pack.index_key:
                raise IntegrityError(
                    "One evaluation manifest unexpectedly produced multiple retrieval indexes"
                )

            expected = set(case.expected_paths)
            returned_paths = tuple(chunk.path for chunk in pack.chunks)
            relevant_paths = expected.intersection(returned_paths)
            first_rank = next(
                (chunk.rank for chunk in pack.chunks if chunk.path in expected),
                None,
            )
            leaked_paths = tuple(
                path for path in dict.fromkeys(returned_paths) if path in case.forbidden_paths
            )
            results.append(
                RetrievalEvalCaseResult(
                    case_id=case.case_id,
                    query=case.query,
                    max_chunks=case.max_chunks,
                    expected_paths=case.expected_paths,
                    returned_paths=returned_paths,
                    hit_at_k=first_rank is not None,
                    expected_path_count=len(expected),
                    relevant_path_count=len(relevant_paths),
                    path_recall_at_k=_ratio(len(relevant_paths), len(expected)),
                    first_relevant_rank=first_rank,
                    reciprocal_rank=(round(1 / first_rank, 6) if first_rank is not None else 0.0),
                    leaked_paths=leaked_paths,
                    retrieval_status=pack.status,
                    backend=pack.backend,
                    degradation_reasons=pack.degradation_reasons,
                )
            )

        hit_count = sum(result.hit_at_k for result in results)
        expected_path_count = sum(result.expected_path_count for result in results)
        relevant_path_count = sum(result.relevant_path_count for result in results)
        return RetrievalEvalReport(
            benchmark_id=manifest.benchmark_id,
            manifest_digest=manifest.sha256,
            workspace_revision=workspace_revision,
            source_manifest_ref=source_manifest_ref,
            scope_hash=scope_hash or "0" * 64,
            index_key=index_key or "0" * 64,
            case_count=len(results),
            hit_count=hit_count,
            hit_rate_at_case_k=_ratio(hit_count, len(results)),
            expected_path_count=expected_path_count,
            relevant_path_count=relevant_path_count,
            micro_path_recall_at_case_k=_ratio(relevant_path_count, expected_path_count),
            mean_reciprocal_rank=round(
                sum(result.reciprocal_rank for result in results) / len(results),
                6,
            ),
            leakage_count=sum(len(result.leaked_paths) for result in results),
            empty_count=sum(result.retrieval_status == "empty" for result in results),
            degraded_count=sum(result.retrieval_status == "degraded" for result in results),
            cases=tuple(results),
        )


def _unique_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(paths))


def _average_precision_at_k(
    returned_paths: tuple[str, ...], expected_paths: set[str], cutoff: int
) -> float:
    hits = 0
    precision_sum = 0.0
    for rank, path in enumerate(returned_paths[:cutoff], start=1):
        if path in expected_paths:
            hits += 1
            precision_sum += hits / rank
    return precision_sum / len(expected_paths)


def _ndcg_at_k(returned_paths: tuple[str, ...], expected_paths: set[str], cutoff: int) -> float:
    dcg = sum(
        1 / math.log2(rank + 1)
        for rank, path in enumerate(returned_paths[:cutoff], start=1)
        if path in expected_paths
    )
    ideal_hits = min(len(expected_paths), cutoff)
    ideal_dcg = sum(1 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    return dcg / ideal_dcg if ideal_dcg else 0.0


def _nearest_rank(values: tuple[float, ...], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 3)


def build_retrieval_suite_report(
    manifest: RetrievalEvalSuiteManifest,
    cases: tuple[RetrievalEvalSuiteCaseResult, ...],
) -> RetrievalEvalSuiteReport:
    """Aggregate independent one-case reports without re-running retrieval or repository code."""
    if len(cases) != len(manifest.cases):
        raise ValueError("Retrieval suite result count does not match its manifest")

    max_cutoff = max(manifest.cutoffs)
    for spec, result in zip(manifest.cases, cases, strict=True):
        if spec.case_id != result.case_id or spec.buggy_commit != result.buggy_commit:
            raise ValueError("Retrieval suite results must preserve manifest order and identity")
        if result.report.cases[0].max_chunks < max_cutoff:
            raise ValueError("Retrieval suite case max_chunks is below the largest metric cutoff")

    rankings = tuple(_unique_paths(result.report.cases[0].returned_paths) for result in cases)
    expected = tuple(set(result.report.cases[0].expected_paths) for result in cases)
    metrics: list[RetrievalEvalAtK] = []
    for cutoff in manifest.cutoffs:
        hit_count = 0
        relevant_path_count = 0
        average_precisions: list[float] = []
        ndcgs: list[float] = []
        for returned_paths, expected_paths in zip(rankings, expected, strict=True):
            relevant = expected_paths.intersection(returned_paths[:cutoff])
            hit_count += bool(relevant)
            relevant_path_count += len(relevant)
            average_precisions.append(
                _average_precision_at_k(returned_paths, expected_paths, cutoff)
            )
            ndcgs.append(_ndcg_at_k(returned_paths, expected_paths, cutoff))
        expected_path_count = sum(len(paths) for paths in expected)
        metrics.append(
            RetrievalEvalAtK(
                cutoff=cutoff,
                hit_count=hit_count,
                hit_rate=_ratio(hit_count, len(cases)),
                expected_path_count=expected_path_count,
                relevant_path_count=relevant_path_count,
                micro_path_recall=_ratio(relevant_path_count, expected_path_count),
                mean_average_precision=round(sum(average_precisions) / len(cases), 6),
                mean_ndcg=round(sum(ndcgs) / len(cases), 6),
            )
        )

    reciprocal_ranks: list[float] = []
    for returned_paths, expected_paths in zip(rankings, expected, strict=True):
        first_rank = next(
            (rank for rank, path in enumerate(returned_paths, start=1) if path in expected_paths),
            None,
        )
        reciprocal_ranks.append(1 / first_rank if first_rank is not None else 0.0)

    cold_durations = tuple(case.cold_duration_ms for case in cases)
    warm_durations = tuple(case.warm_duration_ms for case in cases)
    return RetrievalEvalSuiteReport(
        suite_id=manifest.suite_id,
        suite_manifest_digest=manifest.sha256,
        selection_commit=manifest.selection_commit,
        partition=manifest.partition,
        case_count=len(cases),
        metrics_at_k=tuple(metrics),
        mean_reciprocal_rank=round(sum(reciprocal_ranks) / len(cases), 6),
        leakage_count=sum(case.report.leakage_count for case in cases),
        empty_count=sum(case.report.empty_count for case in cases),
        degraded_count=sum(case.report.degraded_count for case in cases),
        cold_duration_p50_ms=_nearest_rank(cold_durations, 0.50),
        cold_duration_p95_ms=_nearest_rank(cold_durations, 0.95),
        warm_duration_p50_ms=_nearest_rank(warm_durations, 0.50),
        warm_duration_p95_ms=_nearest_rank(warm_durations, 0.95),
        cases=cases,
    )


def _baseline_projection(report: RetrievalEvalSuiteReport) -> dict[str, Any]:
    expected_path_count = report.metrics_at_k[0].expected_path_count
    return {
        "suite_id": report.suite_id,
        "selection_commit": report.selection_commit,
        "aggregate": {
            "case_count": report.case_count,
            "expected_path_count": expected_path_count,
            "mean_reciprocal_rank": report.mean_reciprocal_rank,
            "leakage_count": report.leakage_count,
            "empty_count": report.empty_count,
            "degraded_count": report.degraded_count,
        },
        "metrics_at_k": [metric.model_dump(mode="json") for metric in report.metrics_at_k],
        "cases": [
            {
                "case_id": result.case_id,
                "expected_paths": list(result.report.cases[0].expected_paths),
                "returned_paths": list(result.report.cases[0].returned_paths),
                "first_relevant_rank": result.report.cases[0].first_relevant_rank,
                "relevant_path_count": result.report.cases[0].relevant_path_count,
                "path_recall_at_8": result.report.cases[0].path_recall_at_k,
                "retrieval_status": result.report.cases[0].retrieval_status,
                "degradation_reasons": list(result.report.cases[0].degradation_reasons),
                "report_ref": result.report_ref,
            }
            for result in report.cases
        ],
    }


def _stored_baseline_projection(baseline: Mapping[str, Any]) -> dict[str, Any]:
    try:
        aggregate = baseline["aggregate"]
        if not isinstance(aggregate, Mapping):
            raise TypeError
        metrics = baseline["metrics_at_k"]
        cases = baseline["cases"]
        if not isinstance(metrics, Sequence) or isinstance(metrics, (str, bytes)):
            raise TypeError
        if not isinstance(cases, Sequence) or isinstance(cases, (str, bytes)):
            raise TypeError
        return {
            "suite_id": baseline["suite_id"],
            "selection_commit": baseline["selection_commit"],
            "aggregate": {
                key: aggregate[key]
                for key in (
                    "case_count",
                    "expected_path_count",
                    "mean_reciprocal_rank",
                    "leakage_count",
                    "empty_count",
                    "degraded_count",
                )
            },
            "metrics_at_k": [
                {
                    key: metric[key]
                    for key in (
                        "cutoff",
                        "hit_count",
                        "hit_rate",
                        "expected_path_count",
                        "relevant_path_count",
                        "micro_path_recall",
                        "mean_average_precision",
                        "mean_ndcg",
                    )
                }
                for metric in metrics
            ],
            "cases": [
                {
                    key: case[key]
                    for key in (
                        "case_id",
                        "expected_paths",
                        "returned_paths",
                        "first_relevant_rank",
                        "relevant_path_count",
                        "path_recall_at_8",
                        "retrieval_status",
                        "degradation_reasons",
                        "report_ref",
                    )
                }
                for case in cases
            ],
        }
    except (KeyError, TypeError) as exc:
        raise IntegrityError("Retrieval baseline is missing stable evidence fields") from exc


def _first_difference(expected: Any, actual: Any, path: str = "$") -> str | None:
    if type(expected) is not type(actual):
        return f"{path}: expected type {type(expected).__name__}, got {type(actual).__name__}"
    if isinstance(expected, dict):
        if expected.keys() != actual.keys():
            return f"{path}: field names differ"
        for key in expected:
            difference = _first_difference(expected[key], actual[key], f"{path}.{key}")
            if difference is not None:
                return difference
        return None
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return f"{path}: expected {len(expected)} items, got {len(actual)}"
        for index, (expected_item, actual_item) in enumerate(zip(expected, actual, strict=True)):
            difference = _first_difference(expected_item, actual_item, f"{path}[{index}]")
            if difference is not None:
                return difference
        return None
    if expected != actual:
        return f"{path}: expected {expected!r}, got {actual!r}"
    return None


def verify_retrieval_suite_baseline(
    report: RetrievalEvalSuiteReport,
    baseline: Mapping[str, Any],
) -> str:
    """Verify deterministic rankings and metrics while excluding machine-specific timings."""
    expected = _stored_baseline_projection(baseline)
    actual = _baseline_projection(report)
    difference = _first_difference(expected, actual)
    if difference is not None:
        raise IntegrityError(f"Retrieval suite does not match its frozen baseline: {difference}")
    return digest(actual)
