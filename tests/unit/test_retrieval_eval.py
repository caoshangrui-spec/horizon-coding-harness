import math
from pathlib import Path

import pytest
import yaml

from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.adapters.retrieval.query_normalization import (
    normalize_retrieval_query,
    normalize_retrieval_query_conservatively,
    normalize_retrieval_query_selectively,
)
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
    assert sources.sha256 == "74a87b4dbf6c54f8f9fd003473dcc0a7995f5b7be9953e080a1f5d672ec16431"
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
        "8726f5d1ea513269542eab17b1b788cd1ebb7a4d30f02f3f323fa2f315b482a3"
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
    benchmark_root = root / "benchmarks/retrieval/external-development-v3"
    result_root = root / "benchmarks/retrieval/external-development-v3/query-robustness-v1"
    selection = yaml.safe_load((result_root / "selection.yaml").read_text(encoding="utf-8"))
    result = yaml.safe_load((result_root / "result-v1.yaml").read_text(encoding="utf-8"))
    source_manifest = RetrievalSourceManifest.model_validate(
        yaml.safe_load((benchmark_root / "source-materialization.yaml").read_text(encoding="utf-8"))
    )
    observations = []

    assert result["selection_commit"] == "03261ed2f68a99f9a48e82bf9781590232e959d0"
    assert result["execution"]["holdout_status"] == "sealed"
    assert result["execution"]["evaluator_network_called"] is False
    assert result["execution"]["paid_model_called"] is False
    assert result["execution"]["repository_code_executed"] is False
    assert result["execution"]["source_manifest_digest"] == source_manifest.sha256
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


def test_checked_in_query_normalization_treatment_preserves_non_query_contracts():
    root = Path(__file__).resolve().parents[2]
    benchmark_root = root / "benchmarks/retrieval/external-development-v3"
    treatment_root = benchmark_root / "query-normalization-v1"
    selection = yaml.safe_load((treatment_root / "selection.yaml").read_text(encoding="utf-8"))

    assert selection["partition"] == "development"
    assert selection["model_called"] is False
    assert selection["holdout_status"] == "sealed"
    assert selection["implementation"]["external_model_or_service"] is False
    assert len(selection["cases"]) == 4

    for selected in selection["cases"]:
        baseline_path = (treatment_root / selected["baseline_manifest"]).resolve()
        treatment_path = (treatment_root / selected["treatment_manifest"]).resolve()
        assert baseline_path.is_relative_to(benchmark_root)
        assert treatment_path.is_relative_to(treatment_root)
        baseline = RetrievalEvalManifest.model_validate(
            yaml.safe_load(baseline_path.read_text(encoding="utf-8"))
        )
        treatment = RetrievalEvalManifest.model_validate(
            yaml.safe_load(treatment_path.read_text(encoding="utf-8"))
        )

        assert treatment.allowed_paths == baseline.allowed_paths
        assert treatment.denied_paths == baseline.denied_paths
        assert len(treatment.cases) == len(baseline.cases) == 4
        for original, normalized in zip(baseline.cases, treatment.cases, strict=True):
            rewrite = normalize_retrieval_query(original.query)
            assert normalized.case_id == original.case_id
            assert normalized.expected_paths == original.expected_paths
            assert normalized.forbidden_paths == original.forbidden_paths
            assert normalized.max_chunks == original.max_chunks
            assert normalized.query == rewrite.normalized_query


def test_checked_in_selective_query_normalization_preserves_non_query_contracts():
    root = Path(__file__).resolve().parents[2]
    benchmark_root = root / "benchmarks/retrieval/external-development-v3"
    treatment_root = benchmark_root / "query-normalization-gate-v1"
    selection = yaml.safe_load((treatment_root / "selection.yaml").read_text(encoding="utf-8"))
    changed_queries = 0

    assert selection["partition"] == "development"
    assert selection["model_called"] is False
    assert selection["holdout_status"] == "sealed"
    assert selection["implementation"]["external_model_or_service"] is False
    assert selection["implementation"]["accepted_shapes"] == [
        "cjk_text_v1",
        "test_identifier_v1",
    ]
    assert len(selection["cases"]) == 4

    for selected in selection["cases"]:
        baseline_path = (treatment_root / selected["baseline_manifest"]).resolve()
        treatment_path = (treatment_root / selected["treatment_manifest"]).resolve()
        assert baseline_path.is_relative_to(benchmark_root)
        assert treatment_path.is_relative_to(treatment_root)
        baseline = RetrievalEvalManifest.model_validate(
            yaml.safe_load(baseline_path.read_text(encoding="utf-8"))
        )
        treatment = RetrievalEvalManifest.model_validate(
            yaml.safe_load(treatment_path.read_text(encoding="utf-8"))
        )

        assert treatment.allowed_paths == baseline.allowed_paths
        assert treatment.denied_paths == baseline.denied_paths
        assert len(treatment.cases) == len(baseline.cases) == 4
        for original, normalized in zip(baseline.cases, treatment.cases, strict=True):
            rewrite = normalize_retrieval_query_selectively(original.query)
            assert normalized.case_id == original.case_id
            assert normalized.expected_paths == original.expected_paths
            assert normalized.forbidden_paths == original.forbidden_paths
            assert normalized.max_chunks == original.max_chunks
            assert normalized.query == rewrite.normalized_query
            changed_queries += normalized.query != original.query

    assert changed_queries == 8


def test_checked_in_query_normalization_result_recomputes_ab_deltas():
    root = Path(__file__).resolve().parents[2]
    benchmark_root = root / "benchmarks/retrieval/external-development-v3"
    treatment_root = benchmark_root / "query-normalization-v1"
    selection = yaml.safe_load((treatment_root / "selection.yaml").read_text(encoding="utf-8"))
    result = yaml.safe_load((treatment_root / "result-v1.yaml").read_text(encoding="utf-8"))
    baseline = yaml.safe_load(
        (benchmark_root / "query-robustness-v1/result-v1.yaml").read_text(encoding="utf-8")
    )
    source_manifest = RetrievalSourceManifest.model_validate(
        yaml.safe_load((benchmark_root / "source-materialization.yaml").read_text(encoding="utf-8"))
    )
    observations = []
    changed_queries = 0
    variant_names = (
        "raw_identifier",
        "english_paraphrase",
        "failure_description",
        "chinese_description",
    )

    assert result["selection_commit"] == "109b268b1515a2f1e6b2f5ee5dfd37b7daaaeb89"
    assert result["execution"]["holdout_status"] == "sealed"
    assert result["execution"]["source_manifest_digest"] == source_manifest.sha256
    assert result["execution"]["evaluator_network_called"] is False
    assert result["execution"]["paid_model_called"] is False
    assert result["execution"]["repository_code_executed"] is False
    assert len({group["report_ref"] for group in result["groups"]}) == 4

    for selected, group, baseline_group in zip(
        selection["cases"], result["groups"], baseline["groups"], strict=True
    ):
        original_manifest = RetrievalEvalManifest.model_validate(
            yaml.safe_load(
                (treatment_root / selected["baseline_manifest"]).read_text(encoding="utf-8")
            )
        )
        treatment_manifest = RetrievalEvalManifest.model_validate(
            yaml.safe_load(
                (treatment_root / selected["treatment_manifest"]).read_text(encoding="utf-8")
            )
        )
        assert group["base_case_id"] == baseline_group["base_case_id"]
        relevant_in_group = 0
        hits_in_group = 0
        reciprocal_ranks = []

        for original, treatment, case_result, baseline_case in zip(
            original_manifest.cases,
            treatment_manifest.cases,
            group["cases"],
            baseline_group["cases"],
            strict=True,
        ):
            variant = next(
                name for name in variant_names if treatment.case_id.endswith(name.replace("_", "-"))
            )
            expected = set(treatment.expected_paths)
            returned = tuple(case_result["returned_paths_after"])
            unique_returned = tuple(dict.fromkeys(returned))
            first_rank = next(
                (rank for rank, path in enumerate(returned, start=1) if path in expected),
                None,
            )
            relevant_count = len(expected.intersection(returned))

            assert case_result["variant"] == baseline_case["variant"] == variant
            assert case_result["normalized_query"] == treatment.query
            assert treatment.query == normalize_retrieval_query(original.query).normalized_query
            assert case_result["first_relevant_rank_before"] == baseline_case["first_relevant_rank"]
            assert case_result["relevant_path_count_before"] == baseline_case["relevant_path_count"]
            assert case_result["first_relevant_rank_after"] == first_rank
            assert case_result["relevant_path_count_after"] == relevant_count

            changed_queries += treatment.query != original.query
            relevant_in_group += relevant_count
            hits_in_group += first_rank is not None
            reciprocal_ranks.append(1 / first_rank if first_rank is not None else 0.0)
            observations.append(
                {
                    "variant": variant,
                    "expected": expected,
                    "returned": unique_returned,
                    "first_rank": first_rank,
                }
            )

        assert group["hit_count"] == hits_in_group
        assert group["micro_path_recall_at_8"] == relevant_in_group / sum(
            len(case.expected_paths) for case in treatment_manifest.cases
        )
        assert group["mean_reciprocal_rank"] == pytest.approx(
            sum(reciprocal_ranks) / len(reciprocal_ranks), abs=1e-6
        )

    aggregate = result["aggregate"]
    assert result["execution"]["normalized_query_count"] == changed_queries == 14
    assert aggregate["query_case_count"] == len(observations) == 16
    assert aggregate["expected_path_count"] == sum(
        len(observation["expected"]) for observation in observations
    )
    treatment_mrr = sum(
        1 / observation["first_rank"] if observation["first_rank"] is not None else 0.0
        for observation in observations
    ) / len(observations)
    assert (
        aggregate["mean_reciprocal_rank"]["baseline"]
        == baseline["aggregate"]["mean_reciprocal_rank"]
    )
    assert aggregate["mean_reciprocal_rank"]["treatment"] == pytest.approx(treatment_mrr, abs=1e-6)
    assert aggregate["mean_reciprocal_rank"]["delta"] == pytest.approx(
        treatment_mrr - baseline["aggregate"]["mean_reciprocal_rank"], abs=1e-6
    )
    assert aggregate["empty_count"]["treatment"] == sum(
        group["empty_count"] for group in result["groups"]
    )
    assert aggregate["degraded_count"]["treatment"] == sum(
        group["degraded_count"] for group in result["groups"]
    )

    for metric, baseline_metric in zip(
        aggregate["metrics_at_k"], baseline["aggregate"]["metrics_at_k"], strict=True
    ):
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

        treatment_values = {
            "hit_count": hit_count,
            "relevant_path_count": relevant_path_count,
            "micro_path_recall": relevant_path_count / aggregate["expected_path_count"],
            "mean_average_precision": sum(average_precisions) / len(average_precisions),
            "mean_ndcg": sum(ndcgs) / len(ndcgs),
        }
        for name, actual in treatment_values.items():
            assert metric[name]["baseline"] == baseline_metric[name]
            assert metric[name]["treatment"] == pytest.approx(actual, abs=1e-6)
            assert metric[name]["delta"] == pytest.approx(actual - baseline_metric[name], abs=1e-6)

    for variant, summary in result["by_variant"].items():
        subset = [item for item in observations if item["variant"] == variant]
        hits = sum(item["first_rank"] is not None for item in subset)
        relevant = sum(len(item["expected"].intersection(item["returned"][:8])) for item in subset)
        expected = sum(len(item["expected"]) for item in subset)
        reciprocal_rank = sum(
            1 / item["first_rank"] if item["first_rank"] is not None else 0.0 for item in subset
        ) / len(subset)
        assert summary["hit_at_8"]["treatment"] == f"{hits}/{len(subset)}"
        assert summary["relevant_paths_at_8"]["treatment"] == f"{relevant}/{expected}"
        assert summary["micro_path_recall_at_8"]["treatment"] == relevant / expected
        assert summary["mean_reciprocal_rank"]["treatment"] == pytest.approx(
            reciprocal_rank, abs=1e-6
        )


def test_checked_in_selective_query_normalization_result_recomputes_comparison():
    root = Path(__file__).resolve().parents[2]
    benchmark_root = root / "benchmarks/retrieval/external-development-v3"
    treatment_root = benchmark_root / "query-normalization-gate-v1"
    selection = yaml.safe_load((treatment_root / "selection.yaml").read_text(encoding="utf-8"))
    result = yaml.safe_load((treatment_root / "result-v1.yaml").read_text(encoding="utf-8"))
    baseline = yaml.safe_load(
        (benchmark_root / "query-robustness-v1/result-v1.yaml").read_text(encoding="utf-8")
    )
    always_on = yaml.safe_load(
        (benchmark_root / "query-normalization-v1/result-v1.yaml").read_text(encoding="utf-8")
    )
    source_manifest = RetrievalSourceManifest.model_validate(
        yaml.safe_load((benchmark_root / "source-materialization.yaml").read_text(encoding="utf-8"))
    )
    observations = []
    variant_names = (
        "raw_identifier",
        "english_paraphrase",
        "failure_description",
        "chinese_description",
    )
    gate_applied = 0

    assert result["selection_commit"] == "2626daabac7e303904c864119563fe4c04a9e046"
    assert result["execution"]["holdout_status"] == "sealed"
    assert result["execution"]["source_manifest_digest"] == source_manifest.sha256
    assert result["execution"]["evaluator_network_called"] is False
    assert result["execution"]["paid_model_called"] is False
    assert result["execution"]["repository_code_executed"] is False
    assert len({group["report_ref"] for group in result["groups"]}) == 4

    for selected, group, baseline_group, always_group in zip(
        selection["cases"],
        result["groups"],
        baseline["groups"],
        always_on["groups"],
        strict=True,
    ):
        original_manifest = RetrievalEvalManifest.model_validate(
            yaml.safe_load(
                (treatment_root / selected["baseline_manifest"]).read_text(encoding="utf-8")
            )
        )
        treatment_manifest = RetrievalEvalManifest.model_validate(
            yaml.safe_load(
                (treatment_root / selected["treatment_manifest"]).read_text(encoding="utf-8")
            )
        )
        assert group["base_case_id"] == baseline_group["base_case_id"]
        assert group["base_case_id"] == always_group["base_case_id"]
        reciprocal_ranks = []
        relevant_in_group = 0
        hits_in_group = 0

        for original, treatment, case_result, baseline_case, always_case in zip(
            original_manifest.cases,
            treatment_manifest.cases,
            group["cases"],
            baseline_group["cases"],
            always_group["cases"],
            strict=True,
        ):
            variant = next(
                name for name in variant_names if treatment.case_id.endswith(name.replace("_", "-"))
            )
            rewrite = normalize_retrieval_query_selectively(original.query)
            expected = set(treatment.expected_paths)
            returned = tuple(case_result["returned_paths_selective"])
            unique_returned = tuple(dict.fromkeys(returned))
            first_rank = next(
                (rank for rank, path in enumerate(returned, start=1) if path in expected),
                None,
            )
            relevant_count = len(expected.intersection(returned))
            gate_reason = (
                "bypass"
                if rewrite.applied_rules[0] == "gate_bypass_v1"
                else rewrite.applied_rules[0].removeprefix("gate_")
            )

            assert case_result["variant"] == baseline_case["variant"] == variant
            assert case_result["variant"] == always_case["variant"]
            assert case_result["selected_query"] == treatment.query == rewrite.normalized_query
            assert case_result["gate_reason"] == gate_reason
            assert case_result["first_relevant_rank"] == {
                "baseline": baseline_case["first_relevant_rank"],
                "always_on": always_case["first_relevant_rank_after"],
                "selective": first_rank,
            }
            assert case_result["relevant_path_count"] == {
                "baseline": baseline_case["relevant_path_count"],
                "always_on": always_case["relevant_path_count_after"],
                "selective": relevant_count,
            }

            gate_applied += gate_reason != "bypass"
            relevant_in_group += relevant_count
            hits_in_group += first_rank is not None
            reciprocal_ranks.append(1 / first_rank if first_rank is not None else 0.0)
            observations.append(
                {
                    "variant": variant,
                    "expected": expected,
                    "returned": unique_returned,
                    "first_rank": first_rank,
                }
            )

        assert group["hit_count"] == hits_in_group
        assert group["micro_path_recall_at_8"] == relevant_in_group / sum(
            len(case.expected_paths) for case in treatment_manifest.cases
        )
        assert group["mean_reciprocal_rank"] == pytest.approx(
            sum(reciprocal_ranks) / len(reciprocal_ranks), abs=1e-6
        )

    aggregate = result["aggregate"]
    assert result["execution"]["gate_applied_query_count"] == gate_applied == 8
    assert result["execution"]["bypassed_query_count"] == len(observations) - gate_applied == 8
    assert aggregate["query_case_count"] == len(observations) == 16
    assert aggregate["expected_path_count"] == sum(
        len(observation["expected"]) for observation in observations
    )
    selective_mrr = sum(
        1 / observation["first_rank"] if observation["first_rank"] is not None else 0.0
        for observation in observations
    ) / len(observations)
    assert (
        aggregate["mean_reciprocal_rank"]["baseline"]
        == baseline["aggregate"]["mean_reciprocal_rank"]
    )
    assert (
        aggregate["mean_reciprocal_rank"]["always_on"]
        == always_on["aggregate"]["mean_reciprocal_rank"]["treatment"]
    )
    assert aggregate["mean_reciprocal_rank"]["selective"] == pytest.approx(selective_mrr, abs=1e-6)
    assert aggregate["empty_count"]["selective"] == sum(
        group["empty_count"] for group in result["groups"]
    )
    assert aggregate["degraded_count"]["selective"] == sum(
        group["degraded_count"] for group in result["groups"]
    )

    for metric, baseline_metric, always_metric in zip(
        aggregate["metrics_at_k"],
        baseline["aggregate"]["metrics_at_k"],
        always_on["aggregate"]["metrics_at_k"],
        strict=True,
    ):
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

        selective_values = {
            "hit_count": hit_count,
            "relevant_path_count": relevant_path_count,
            "micro_path_recall": relevant_path_count / aggregate["expected_path_count"],
            "mean_average_precision": sum(average_precisions) / len(average_precisions),
            "mean_ndcg": sum(ndcgs) / len(ndcgs),
        }
        for name, actual in selective_values.items():
            assert metric[name]["baseline"] == baseline_metric[name]
            assert metric[name]["always_on"] == always_metric[name]["treatment"]
            assert metric[name]["selective"] == pytest.approx(actual, abs=1e-6)

    for variant, summary in result["by_variant"].items():
        subset = [item for item in observations if item["variant"] == variant]
        hits = sum(item["first_rank"] is not None for item in subset)
        relevant = sum(len(item["expected"].intersection(item["returned"][:8])) for item in subset)
        expected = sum(len(item["expected"]) for item in subset)
        mrr = sum(
            1 / item["first_rank"] if item["first_rank"] is not None else 0.0 for item in subset
        ) / len(subset)
        assert summary["hit_at_8"]["selective"] == f"{hits}/{len(subset)}"
        assert summary["relevant_paths_at_8"]["selective"] == f"{relevant}/{expected}"
        assert summary["micro_path_recall_at_8"]["selective"] == relevant / expected
        assert summary["mean_reciprocal_rank"]["selective"] == pytest.approx(mrr, abs=1e-6)


def test_checked_in_broad_gate_suite_uses_remaining_development_cases_and_frozen_pairs():
    root = Path(__file__).resolve().parents[2]
    benchmark_root = root / "benchmarks/retrieval/external-development-v3"
    broad_root = benchmark_root / "query-normalization-gate-broad-v1"
    base_suite = RetrievalEvalSuiteManifest.model_validate(
        yaml.safe_load((benchmark_root / "development-suite.yaml").read_text(encoding="utf-8"))
    )
    design_selection = yaml.safe_load(
        (benchmark_root / "query-normalization-gate-v1/selection.yaml").read_text(encoding="utf-8")
    )
    selection = yaml.safe_load((broad_root / "selection.yaml").read_text(encoding="utf-8"))
    base_case_ids = {case.case_id for case in base_suite.cases}
    design_case_ids = {case["base_case_id"] for case in design_selection["cases"]}
    selected_case_ids = {case["base_case_id"] for case in selection["cases"]}
    gate_positive_count = 0
    bypass_count = 0

    assert selection["partition"] == "development"
    assert selection["holdout_status"] == "sealed"
    assert selection["relationship_to_gate_design"] == (
        "out-of-design cases with post-design frozen queries"
    )
    assert selected_case_ids == base_case_ids - design_case_ids
    assert len(selected_case_ids) == 12
    assert selected_case_ids.isdisjoint(design_case_ids)

    suffixes = (
        "raw-baseline",
        "raw-selective",
        "english-bypass",
        "bare-test-bypass",
        "chinese-baseline",
        "chinese-selective",
    )
    for selected in selection["cases"]:
        base_path = (broad_root / selected["base_manifest"]).resolve()
        paired_path = (broad_root / selected["paired_manifest"]).resolve()
        assert base_path.is_relative_to(benchmark_root)
        assert paired_path.is_relative_to(broad_root)
        base = RetrievalEvalManifest.model_validate(
            yaml.safe_load(base_path.read_text(encoding="utf-8"))
        )
        paired = RetrievalEvalManifest.model_validate(
            yaml.safe_load(paired_path.read_text(encoding="utf-8"))
        )
        cases = {
            suffix: next(case for case in paired.cases if case.case_id.endswith(suffix))
            for suffix in suffixes
        }

        assert len(base.cases) == 1
        assert len(paired.cases) == len(suffixes)
        assert paired.allowed_paths == base.allowed_paths
        assert paired.denied_paths == base.denied_paths
        assert cases["english-bypass"].query == base.cases[0].query
        for case in paired.cases:
            assert case.expected_paths == base.cases[0].expected_paths
            assert case.forbidden_paths == base.cases[0].forbidden_paths
            assert case.max_chunks == base.cases[0].max_chunks == 8

        raw = normalize_retrieval_query_selectively(cases["raw-baseline"].query)
        chinese = normalize_retrieval_query_selectively(cases["chinese-baseline"].query)
        english = normalize_retrieval_query_selectively(cases["english-bypass"].query)
        bare_test = normalize_retrieval_query_selectively(cases["bare-test-bypass"].query)

        assert raw.applied_rules[0] == "gate_test_identifier_v1"
        assert raw.normalized_query == cases["raw-selective"].query
        assert chinese.applied_rules[0] == "gate_cjk_text_v1"
        assert chinese.normalized_query == cases["chinese-selective"].query
        assert english.applied_rules == ("gate_bypass_v1",)
        assert english.normalized_query == cases["english-bypass"].query
        assert bare_test.applied_rules == ("gate_bypass_v1",)
        assert bare_test.normalized_query == cases["bare-test-bypass"].query
        assert "test" in cases["bare-test-bypass"].query.casefold().split()

        gate_positive_count += 2
        bypass_count += 2

    assert gate_positive_count == 24
    assert bypass_count == 24


def test_checked_in_broad_gate_result_recomputes_from_paired_ranks():
    root = Path(__file__).resolve().parents[2]
    benchmark_root = root / "benchmarks/retrieval/external-development-v3"
    broad_root = benchmark_root / "query-normalization-gate-broad-v1"
    selection = yaml.safe_load((broad_root / "selection.yaml").read_text(encoding="utf-8"))
    result = yaml.safe_load((broad_root / "result-v1.yaml").read_text(encoding="utf-8"))
    source_manifest = RetrievalSourceManifest.model_validate(
        yaml.safe_load((benchmark_root / "source-materialization.yaml").read_text(encoding="utf-8"))
    )
    observations = {"baseline": [], "selective": []}
    physical_case_count = 0
    changed_query_count = 0
    suffixes = {
        "raw_identifier": ("raw-baseline", "raw-selective"),
        "english_paraphrase": ("english-bypass", "english-bypass"),
        "bare_test_sentence": ("bare-test-bypass", "bare-test-bypass"),
        "chinese_description": ("chinese-baseline", "chinese-selective"),
    }

    assert result["selection_commit"] == "7bb44b6bce647d1d6b080b4c6a6e2705fa85aa79"
    assert result["execution"]["holdout_status"] == "sealed"
    assert result["execution"]["source_manifest_digest"] == source_manifest.sha256
    assert result["execution"]["evaluator_network_called"] is False
    assert result["execution"]["paid_model_called"] is False
    assert result["execution"]["repository_code_executed"] is False
    assert len({group["report_ref"] for group in result["groups"]}) == 12

    for selected, group in zip(selection["cases"], result["groups"], strict=True):
        manifest = RetrievalEvalManifest.model_validate(
            yaml.safe_load((broad_root / selected["paired_manifest"]).read_text(encoding="utf-8"))
        )
        physical_case_count += len(manifest.cases)
        cases = {
            suffix: next(case for case in manifest.cases if case.case_id.endswith(suffix))
            for pair in suffixes.values()
            for suffix in pair
        }
        assert group["base_case_id"] == selected["base_case_id"]

        for variant, (baseline_suffix, selective_suffix) in suffixes.items():
            baseline_case = cases[baseline_suffix]
            selective_case = cases[selective_suffix]
            frozen = group["variants"][variant]
            changed_query_count += baseline_case.query != selective_case.query
            for arm, case, rank_key, status_key in (
                ("baseline", baseline_case, "baseline_rank", "baseline_status"),
                ("selective", selective_case, "selective_rank", "selective_status"),
            ):
                observations[arm].append(
                    {
                        "variant": variant,
                        "rank": frozen[rank_key],
                        "status": frozen[status_key],
                        "expected_count": len(case.expected_paths),
                    }
                )

    execution = result["execution"]
    assert execution["base_case_count"] == len(selection["cases"]) == 12
    assert execution["source_query_count"] == 48
    assert execution["physical_evaluation_case_count"] == physical_case_count == 72
    assert execution["logical_query_count_per_arm"] == len(observations["baseline"]) == 48
    assert len(observations["selective"]) == 48
    assert result["gate_contract"]["query_text_changed_count"] == changed_query_count == 23

    aggregate = result["aggregate"]
    assert aggregate["expected_path_count_per_arm"] == 48
    for arm, items in observations.items():
        mrr = sum(1 / item["rank"] if item["rank"] is not None else 0.0 for item in items) / len(
            items
        )
        empty_count = sum(item["status"] == "empty" for item in items)
        degraded_count = sum(item["status"] == "degraded" for item in items)
        nonempty_miss_count = sum(
            item["rank"] is None and item["status"] != "empty" for item in items
        )
        assert aggregate["mean_reciprocal_rank"][arm] == pytest.approx(mrr, abs=1e-6)
        assert aggregate["empty_count"][arm] == empty_count
        assert aggregate["degraded_count"][arm] == degraded_count
        assert aggregate["nonempty_miss_count"][arm] == nonempty_miss_count

        for metric in aggregate["metrics_at_k"]:
            cutoff = metric["cutoff"]
            hits = sum(item["rank"] is not None and item["rank"] <= cutoff for item in items)
            average_precision = sum(
                1 / item["rank"] if item["rank"] is not None and item["rank"] <= cutoff else 0.0
                for item in items
            ) / len(items)
            ndcg = sum(
                1 / math.log2(item["rank"] + 1)
                if item["rank"] is not None and item["rank"] <= cutoff
                else 0.0
                for item in items
            ) / len(items)
            assert metric["hit_count"][arm] == hits
            assert metric["micro_path_recall"][arm] == pytest.approx(hits / len(items), abs=1e-6)
            assert metric["mean_average_precision"][arm] == pytest.approx(
                average_precision, abs=1e-6
            )
            assert metric["mean_ndcg"][arm] == pytest.approx(ndcg, abs=1e-6)

    for variant, summary in result["by_variant"].items():
        subsets = {
            arm: [item for item in items if item["variant"] == variant]
            for arm, items in observations.items()
        }
        assert summary["query_count"] == len(subsets["baseline"]) == 12
        for arm, items in subsets.items():
            hits = sum(item["rank"] is not None and item["rank"] <= 8 for item in items)
            mrr = sum(
                1 / item["rank"] if item["rank"] is not None else 0.0 for item in items
            ) / len(items)
            empty_count = sum(item["status"] == "empty" for item in items)
            assert summary["hit_at_8"][arm] == f"{hits}/{len(items)}"
            assert summary["mean_reciprocal_rank"][arm] == pytest.approx(mrr, abs=1e-6)
            assert summary["empty_count"][arm] == empty_count

        paired = zip(subsets["baseline"], subsets["selective"], strict=True)
        pairs = list(paired)
        improved = sum(
            selective["rank"] is not None
            and (baseline["rank"] is None or selective["rank"] < baseline["rank"])
            for baseline, selective in pairs
        )
        regressed = sum(
            baseline["rank"] is not None
            and (selective["rank"] is None or selective["rank"] > baseline["rank"])
            for baseline, selective in pairs
        )
        assert summary["rank_improved_count"] == improved
        assert summary["rank_regressed_count"] == regressed

    assert result["gate_contract"]["expected_apply_count"] == 24
    assert result["gate_contract"]["observed_apply_count"] == 24
    assert result["gate_contract"]["expected_bypass_count"] == 24
    assert result["gate_contract"]["observed_bypass_count"] == 24
    assert result["gate_contract"]["false_activation_count"] == 0
    assert result["gate_contract"]["false_bypass_count"] == 0


def test_checked_in_conservative_cjk_treatment_preserves_contracts_and_bypasses_low_coverage():
    root = Path(__file__).resolve().parents[2]
    benchmark_root = root / "benchmarks/retrieval/external-development-v3"
    treatment_root = benchmark_root / "query-normalization-cjk-conservative-v1"
    broad_root = benchmark_root / "query-normalization-gate-broad-v1"
    selection = yaml.safe_load((treatment_root / "selection.yaml").read_text(encoding="utf-8"))
    broad_selection = yaml.safe_load((broad_root / "selection.yaml").read_text(encoding="utf-8"))

    assert selection["partition"] == "development"
    assert selection["holdout_status"] == "sealed"
    assert selection["implementation"]["minimum_cjk_glossary_matches"] == 2
    assert selection["implementation"]["preserve_original_query"] is True
    assert selection["implementation"]["external_model_or_service"] is False
    assert len(selection["cases"]) == 4

    for selected in selection["cases"]:
        baseline = RetrievalEvalManifest.model_validate(
            yaml.safe_load(
                (treatment_root / selected["baseline_manifest"]).read_text(encoding="utf-8")
            )
        )
        treatment = RetrievalEvalManifest.model_validate(
            yaml.safe_load(
                (treatment_root / selected["treatment_manifest"]).read_text(encoding="utf-8")
            )
        )
        original = next(
            case for case in baseline.cases if case.case_id.endswith("chinese-description")
        )
        additive = treatment.cases[0]
        rewrite = normalize_retrieval_query_conservatively(original.query)

        assert treatment.allowed_paths == baseline.allowed_paths
        assert treatment.denied_paths == baseline.denied_paths
        assert len(treatment.cases) == 1
        assert additive.expected_paths == original.expected_paths
        assert additive.forbidden_paths == original.forbidden_paths
        assert additive.max_chunks == original.max_chunks == 8
        assert additive.query == rewrite.normalized_query
        assert additive.query.startswith(f"{original.query} ")
        assert rewrite.applied_rules[0] == "gate_cjk_additive_v1"

    low_coverage_bypass_count = 0
    for selected in broad_selection["cases"]:
        manifest = RetrievalEvalManifest.model_validate(
            yaml.safe_load((broad_root / selected["paired_manifest"]).read_text(encoding="utf-8"))
        )
        original = next(
            case for case in manifest.cases if case.case_id.endswith("chinese-baseline")
        )
        rewrite = normalize_retrieval_query_conservatively(original.query)
        assert rewrite.normalized_query == original.query
        assert rewrite.applied_rules == ("gate_cjk_insufficient_coverage_v1",)
        low_coverage_bypass_count += 1

    assert low_coverage_bypass_count == 12
