from __future__ import annotations

from horizon.adapters.retrieval.sqlite_fts import SQLiteCodeRetriever
from horizon.domain.errors import IntegrityError
from horizon.domain.evaluation import (
    RetrievalEvalCaseResult,
    RetrievalEvalManifest,
    RetrievalEvalReport,
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
