import math
from pathlib import Path

import pytest
import yaml

from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.adapters.retrieval.sqlite_fts import SQLiteCodeRetriever
from horizon.adapters.workspace.snapshot import SnapshotManager
from horizon.application.retrieval_eval import (
    RetrievalEvaluator,
    build_retrieval_suite_report,
    verify_retrieval_suite_baseline,
)
from horizon.domain.errors import IntegrityError
from horizon.domain.evaluation import (
    RetrievalEvalCaseResult,
    RetrievalEvalManifest,
    RetrievalEvalReport,
    RetrievalEvalSuiteCaseResult,
    RetrievalEvalSuiteManifest,
    RetrievalSourceManifest,
)


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


def _suite_case_result(
    *,
    case_id: str,
    expected_paths: tuple[str, ...],
    returned_paths: tuple[str, ...],
    cold_duration_ms: float,
    warm_duration_ms: float,
) -> RetrievalEvalSuiteCaseResult:
    expected = set(expected_paths)
    relevant = expected.intersection(returned_paths)
    first_rank = next(
        (rank for rank, path in enumerate(returned_paths, start=1) if path in expected),
        None,
    )
    case = RetrievalEvalCaseResult(
        case_id=case_id,
        query=case_id,
        max_chunks=8,
        expected_paths=expected_paths,
        returned_paths=returned_paths,
        hit_at_k=first_rank is not None,
        expected_path_count=len(expected),
        relevant_path_count=len(relevant),
        path_recall_at_k=len(relevant) / len(expected),
        first_relevant_rank=first_rank,
        reciprocal_rank=1 / first_rank if first_rank is not None else 0.0,
        retrieval_status="ok" if returned_paths else "empty",
        backend="sqlite_fts5",
    )
    report = RetrievalEvalReport(
        benchmark_id=f"{case_id}-benchmark",
        manifest_digest="a" * 64,
        workspace_revision="b" * 64,
        source_manifest_ref="c" * 64,
        scope_hash="d" * 64,
        index_key="e" * 64,
        case_count=1,
        hit_count=int(case.hit_at_k),
        hit_rate_at_case_k=float(case.hit_at_k),
        expected_path_count=case.expected_path_count,
        relevant_path_count=case.relevant_path_count,
        micro_path_recall_at_case_k=case.path_recall_at_k,
        mean_reciprocal_rank=case.reciprocal_rank,
        leakage_count=0,
        empty_count=int(case.retrieval_status == "empty"),
        degraded_count=0,
        cases=(case,),
    )
    return RetrievalEvalSuiteCaseResult(
        case_id=case_id,
        buggy_commit="1" * 40,
        report_ref="f" * 64,
        cold_duration_ms=cold_duration_ms,
        warm_duration_ms=warm_duration_ms,
        report=report,
    )


def test_retrieval_suite_aggregates_cutoff_curves_and_path_ranking():
    manifest = RetrievalEvalSuiteManifest.model_validate(
        {
            "suite_id": "suite-fixture",
            "selection_commit": "0" * 40,
            "partition": "development",
            "cutoffs": [1, 3],
            "cases": [
                {
                    "case_id": "first",
                    "manifest_path": "cases/first.yaml",
                    "source_subdir": "first",
                    "buggy_commit": "1" * 40,
                },
                {
                    "case_id": "second",
                    "manifest_path": "cases/second.yaml",
                    "source_subdir": "second",
                    "buggy_commit": "1" * 40,
                },
            ],
        }
    )
    cases = (
        _suite_case_result(
            case_id="first",
            expected_paths=("src/a.py", "src/b.py"),
            returned_paths=("src/noise.py", "src/a.py", "src/b.py"),
            cold_duration_ms=20,
            warm_duration_ms=2,
        ),
        _suite_case_result(
            case_id="second",
            expected_paths=("src/c.py",),
            returned_paths=("src/c.py", "src/c.py", "src/noise.py"),
            cold_duration_ms=10,
            warm_duration_ms=1,
        ),
    )

    report = build_retrieval_suite_report(manifest, cases)

    assert tuple(metric.cutoff for metric in report.metrics_at_k) == (1, 3)
    assert report.metrics_at_k[0].hit_count == 1
    assert report.metrics_at_k[0].micro_path_recall == pytest.approx(1 / 3, abs=1e-6)
    assert report.metrics_at_k[1].hit_count == 2
    assert report.metrics_at_k[1].micro_path_recall == 1.0
    assert report.metrics_at_k[1].mean_average_precision == pytest.approx(0.791667)
    assert report.mean_reciprocal_rank == 0.75
    assert report.cold_duration_p50_ms == 10
    assert report.cold_duration_p95_ms == 20
    assert report.warm_duration_p50_ms == 1
    assert report.warm_duration_p95_ms == 2
    assert report.network_called is False
    assert report.paid_model_called is False

    baseline = {
        "suite_id": report.suite_id,
        "selection_commit": report.selection_commit,
        "aggregate": {
            "case_count": report.case_count,
            "expected_path_count": 3,
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
    assert len(verify_retrieval_suite_baseline(report, baseline)) == 64

    baseline["cases"][0]["returned_paths"] = ["src/wrong.py"]
    with pytest.raises(IntegrityError, match=r"cases\[0\]\.returned_paths"):
        verify_retrieval_suite_baseline(report, baseline)


@pytest.mark.parametrize(
    "cutoffs",
    [
        [3, 1],
        [1, 1],
        [0, 1],
        [1, 9],
    ],
)
def test_retrieval_suite_rejects_invalid_cutoffs(cutoffs):
    with pytest.raises(ValueError, match="cutoffs"):
        RetrievalEvalSuiteManifest.model_validate(
            {
                "suite_id": "invalid-cutoffs",
                "selection_commit": "0" * 40,
                "partition": "development",
                "cutoffs": cutoffs,
                "cases": [
                    {
                        "case_id": "only",
                        "manifest_path": "cases/only.yaml",
                        "source_subdir": "only",
                        "buggy_commit": "1" * 40,
                    }
                ],
            }
        )


def test_checked_in_external_v3_suite_preserves_selection_and_sealed_holdout():
    root = Path(__file__).resolve().parents[2]
    benchmark_root = root / "benchmarks/retrieval/external-development-v3"
    selection = yaml.safe_load((benchmark_root / "selection.yaml").read_text(encoding="utf-8"))
    suite = RetrievalEvalSuiteManifest.model_validate(
        yaml.safe_load((benchmark_root / "development-suite.yaml").read_text(encoding="utf-8"))
    )
    sources = RetrievalSourceManifest.model_validate(
        yaml.safe_load((benchmark_root / "source-materialization.yaml").read_text(encoding="utf-8"))
    )
    selected = {case["case_id"]: case for case in selection["cases"]}
    development = {
        case_id for case_id, case in selected.items() if case["partition"] == "development"
    }
    holdout = {case_id for case_id, case in selected.items() if case["partition"] == "holdout"}

    assert len(selected) == 24
    assert len(development) == 16
    assert len(holdout) == 8
    assert {case.case_id for case in suite.cases} == development
    assert tuple(case.case_id for case in sources.cases) == tuple(
        case.case_id for case in suite.cases
    )
    assert sources.suite_id == suite.suite_id
    assert sources.partition == suite.partition == "development"
    assert sources.evaluation_suite_path == "development-suite.yaml"
    assert suite.selection_commit == "57776b89d1f7a99723f61c5a8b15db5c5ff579ae"
    assert all("expected_paths" not in selected[case_id] for case_id in holdout)
    assert not any((benchmark_root / "cases" / f"{case_id}.yaml").exists() for case_id in holdout)

    for spec in suite.cases:
        source = selected[spec.case_id]
        source_spec = next(case for case in sources.cases if case.case_id == spec.case_id)
        manifest = RetrievalEvalManifest.model_validate(
            yaml.safe_load((benchmark_root / spec.manifest_path).read_text(encoding="utf-8"))
        )
        assert spec.buggy_commit == source["buggy_commit"]
        assert source_spec.buggy_commit == source["buggy_commit"]
        assert source_spec.repository_url.removesuffix(".git") == source[
            "repository_url"
        ].removesuffix(".git")
        assert all(
            any(
                expected == root or expected.startswith(f"{root}/")
                for root in source_spec.archive_paths
            )
            for expected in manifest.cases[0].expected_paths
        )
        assert manifest.cases[0].case_id == spec.case_id
        assert manifest.cases[0].query == source["query"]
        assert manifest.cases[0].max_chunks == 8

    baseline = yaml.safe_load((benchmark_root / "baseline-v0.3.0.yaml").read_text(encoding="utf-8"))
    assert baseline["selection_commit"] == suite.selection_commit
    assert baseline["aggregate"]["case_count"] == 16
    assert baseline["aggregate"]["expected_path_count"] == 20
    assert baseline["aggregate"]["report_ref"] == (
        "8f68016ce398da6464e669aadb8855de2bb850beb9c6ba78d993bd9291a653a5"
    )
    assert baseline["metrics_at_k"][0]["hit_count"] == 11
    assert baseline["metrics_at_k"][2]["hit_count"] == 16
    assert baseline["metrics_at_k"][2]["micro_path_recall"] == 0.9


def test_checked_in_query_robustness_selection_has_four_frozen_variants_per_case():
    root = Path(__file__).resolve().parents[2]
    benchmark_root = root / "benchmarks/retrieval/external-development-v3"
    base_suite = RetrievalEvalSuiteManifest.model_validate(
        yaml.safe_load((benchmark_root / "development-suite.yaml").read_text(encoding="utf-8"))
    )
    selection_path = benchmark_root / "query-robustness-v1/selection.yaml"
    selection = yaml.safe_load(selection_path.read_text(encoding="utf-8"))
    expected_kinds = {
        "raw-identifier",
        "english-paraphrase",
        "failure-description",
        "chinese-description",
    }
    base_cases = {case.case_id: case for case in base_suite.cases}

    assert selection["partition"] == "development"
    assert selection["holdout_status"] == "sealed"
    assert selection["model_called"] is False
    assert len(selection["cases"]) == 4
    for selected in selection["cases"]:
        assert selected["base_case_id"] in base_cases
        assert selected["source_subdir"] == base_cases[selected["base_case_id"]].source_subdir
        manifest_path = (selection_path.parent / selected["manifest_path"]).resolve()
        assert manifest_path.is_relative_to(selection_path.parent)
        manifest = RetrievalEvalManifest.model_validate(
            yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        )
        assert len(manifest.cases) == 4
        assert len({case.query for case in manifest.cases}) == 4
        assert {
            next(kind for kind in expected_kinds if case.case_id.endswith(kind))
            for case in manifest.cases
        } == expected_kinds
        assert all(case.max_chunks == 8 for case in manifest.cases)


def test_checked_in_query_robustness_result_recomputes_from_rankings():
    root = Path(__file__).resolve().parents[2]
    result_root = root / "benchmarks/retrieval/external-development-v3/query-robustness-v1"
    selection = yaml.safe_load((result_root / "selection.yaml").read_text(encoding="utf-8"))
    result = yaml.safe_load((result_root / "result-v1.yaml").read_text(encoding="utf-8"))
    observations = []

    assert result["selection_commit"] == "03261ed2f68a99f9a48e82bf9781590232e959d0"
    assert result["execution"]["holdout_status"] == "sealed"
    assert result["execution"]["evaluator_network_called"] is False
    assert result["execution"]["paid_model_called"] is False
    assert result["execution"]["repository_code_executed"] is False
    assert len({group["report_ref"] for group in result["groups"]}) == 4

    for selected, group in zip(selection["cases"], result["groups"], strict=True):
        assert group["base_case_id"] == selected["base_case_id"]
        manifest = RetrievalEvalManifest.model_validate(
            yaml.safe_load((result_root / selected["manifest_path"]).read_text(encoding="utf-8"))
        )
        reciprocal_ranks = []
        for case, case_result in zip(manifest.cases, group["cases"], strict=True):
            expected = set(case.expected_paths)
            returned = tuple(case_result["returned_paths"])
            first_rank = next(
                (rank for rank, path in enumerate(returned, start=1) if path in expected),
                None,
            )
            relevant_count = len(expected.intersection(returned))
            assert case.case_id.endswith(case_result["variant"].replace("_", "-"))
            assert case_result["first_relevant_rank"] == first_rank
            assert case_result["relevant_path_count"] == relevant_count
            reciprocal_ranks.append(1 / first_rank if first_rank is not None else 0.0)
            observations.append(
                {
                    "variant": case_result["variant"],
                    "expected": expected,
                    "returned": tuple(dict.fromkeys(returned)),
                    "first_rank": first_rank,
                }
            )
        assert group["mean_reciprocal_rank"] == pytest.approx(
            sum(reciprocal_ranks) / len(reciprocal_ranks), abs=1e-6
        )

    aggregate = result["aggregate"]
    assert aggregate["query_case_count"] == len(observations) == 16
    assert aggregate["expected_path_count"] == sum(
        len(observation["expected"]) for observation in observations
    )
    assert aggregate["mean_reciprocal_rank"] == pytest.approx(
        sum(
            1 / observation["first_rank"] if observation["first_rank"] is not None else 0.0
            for observation in observations
        )
        / len(observations),
        abs=1e-6,
    )
    assert aggregate["empty_count"] == sum(group["empty_count"] for group in result["groups"])
    assert aggregate["degraded_count"] == sum(group["degraded_count"] for group in result["groups"])

    for metric in aggregate["metrics_at_k"]:
        cutoff = metric["cutoff"]
        hit_count = 0
        relevant_path_count = 0
        average_precisions = []
        ndcgs = []
        for observation in observations:
            expected = observation["expected"]
            returned = observation["returned"][:cutoff]
            relevant = expected.intersection(returned)
            hit_count += bool(relevant)
            relevant_path_count += len(relevant)
            hits = 0
            precision_sum = 0.0
            for rank, path in enumerate(returned, start=1):
                if path in expected:
                    hits += 1
                    precision_sum += hits / rank
            average_precisions.append(precision_sum / len(expected))
            dcg = sum(
                1 / math.log2(rank + 1)
                for rank, path in enumerate(returned, start=1)
                if path in expected
            )
            ideal_dcg = sum(
                1 / math.log2(rank + 1) for rank in range(1, min(len(expected), cutoff) + 1)
            )
            ndcgs.append(dcg / ideal_dcg)
        assert metric["hit_count"] == hit_count
        assert metric["hit_rate"] == hit_count / len(observations)
        assert metric["relevant_path_count"] == relevant_path_count
        assert metric["micro_path_recall"] == relevant_path_count / aggregate["expected_path_count"]
        assert metric["mean_average_precision"] == pytest.approx(
            sum(average_precisions) / len(average_precisions), abs=1e-6
        )
        assert metric["mean_ndcg"] == pytest.approx(sum(ndcgs) / len(ndcgs), abs=1e-6)

    for variant, summary in result["by_variant"].items():
        subset = [item for item in observations if item["variant"] == variant]
        hits = sum(item["first_rank"] is not None for item in subset)
        relevant = sum(len(item["expected"].intersection(item["returned"][:8])) for item in subset)
        expected = sum(len(item["expected"]) for item in subset)
        assert summary["hit_at_8"] == f"{hits}/{len(subset)}"
        assert summary["relevant_paths_at_8"] == f"{relevant}/{expected}"
        assert summary["micro_path_recall_at_8"] == relevant / expected
