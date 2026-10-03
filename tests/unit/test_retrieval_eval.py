from pathlib import Path

import pytest

from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.adapters.retrieval.sqlite_fts import SQLiteCodeRetriever
from horizon.adapters.workspace.snapshot import SnapshotManager
from horizon.application.retrieval_eval import RetrievalEvaluator
from horizon.domain.evaluation import RetrievalEvalManifest


def setup_evaluator(tmp_path: Path):
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src/alpha.py").write_text(
        "def alpha_parser(value):\n    return normalize_value(value)\n",
        encoding="utf-8",
    )
    (workspace / "src/beta.py").write_text(
        "def beta_writer(value):\n    return persist_value(value)\n",
        encoding="utf-8",
    )
    artifacts = ArtifactStore(tmp_path / "artifacts")
    snapshots = SnapshotManager(artifacts)
    snapshot, source_manifest_ref = snapshots.capture(workspace)
    retriever = SQLiteCodeRetriever(tmp_path / "retrieval.sqlite3", snapshots)
    return snapshot, source_manifest_ref, RetrievalEvaluator(retriever), retriever


def test_evaluator_reports_hits_recall_mrr_leakage_and_empty(tmp_path):
    snapshot, source_manifest_ref, evaluator, _ = setup_evaluator(tmp_path)
    manifest = RetrievalEvalManifest.model_validate(
        {
            "benchmark_id": "fixture",
            "allowed_paths": ["src/**"],
            "cases": [
                {
                    "case_id": "hit-with-leak",
                    "query": "alpha_parser beta_writer",
                    "expected_paths": ["src/alpha.py"],
                    "forbidden_paths": ["src/beta.py"],
                    "max_chunks": 2,
                },
                {
                    "case_id": "empty",
                    "query": "symbol_that_does_not_exist",
                    "expected_paths": ["src/beta.py"],
                    "max_chunks": 2,
                },
            ],
        }
    )

    report = evaluator.evaluate(
        manifest,
        source_manifest_ref=source_manifest_ref,
        workspace_revision=snapshot.workspace_revision,
    )

    assert report.case_count == 2
    assert report.hit_count == 1
    assert report.hit_rate_at_case_k == 0.5
    assert report.micro_path_recall_at_case_k == 0.5
    assert report.mean_reciprocal_rank == 0.5
    assert report.leakage_count == 1
    assert report.empty_count == 1
    assert report.degraded_count == 0
    assert report.paid_model_called is False
    assert report.network_called is False
    assert report.cases[0].first_relevant_rank == 1
    assert report.cases[0].leaked_paths == ("src/beta.py",)
    assert report.cases[1].retrieval_status == "empty"


def test_evaluator_counts_explicit_fallback_degradation(tmp_path):
    snapshot, source_manifest_ref, evaluator, retriever = setup_evaluator(tmp_path)
    retriever.fts5_available = False
    manifest = RetrievalEvalManifest.model_validate(
        {
            "benchmark_id": "fallback",
            "cases": [
                {
                    "case_id": "fallback-hit",
                    "query": "alpha_parser",
                    "expected_paths": ["src/alpha.py"],
                }
            ],
        }
    )

    report = evaluator.evaluate(
        manifest,
        source_manifest_ref=source_manifest_ref,
        workspace_revision=snapshot.workspace_revision,
    )

    assert report.hit_count == 1
    assert report.degraded_count == 1
    assert report.cases[0].backend == "lexical_scan"
    assert report.cases[0].degradation_reasons == ("fts5_unavailable",)


@pytest.mark.parametrize(
    ("update", "message"),
    [
        (
            {
                "cases": [
                    {
                        "case_id": "duplicate",
                        "query": "alpha",
                        "expected_paths": ["src/alpha.py"],
                    },
                    {
                        "case_id": "duplicate",
                        "query": "beta",
                        "expected_paths": ["src/beta.py"],
                    },
                ]
            },
            "case IDs",
        ),
        (
            {
                "cases": [
                    {
                        "case_id": "overlap",
                        "query": "alpha",
                        "expected_paths": ["src/alpha.py"],
                        "forbidden_paths": ["src/alpha.py"],
                    }
                ]
            },
            "disjoint",
        ),
        (
            {
                "cases": [
                    {
                        "case_id": "glob-target",
                        "query": "alpha",
                        "expected_paths": ["src/*.py"],
                    }
                ]
            },
            "literal paths",
        ),
    ],
)
def test_manifest_rejects_ambiguous_cases(update, message):
    raw = {
        "benchmark_id": "invalid",
        "cases": [
            {
                "case_id": "base",
                "query": "alpha",
                "expected_paths": ["src/alpha.py"],
            }
        ],
    }
    raw.update(update)

    with pytest.raises(ValueError, match=message):
        RetrievalEvalManifest.model_validate(raw)
