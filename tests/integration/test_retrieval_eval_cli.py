import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from horizon.interfaces.cli.app import app

runner = CliRunner()


def test_retrieval_eval_cli_is_offline_and_persists_report(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src/service.py").write_text(
        "def reconcile_reservation():\n    return 'settled'\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        """schema_version: 1
benchmark_id: cli-fixture
allowed_paths: [src/**]
cases:
  - case_id: service
    query: reconcile reservation settled
    expected_paths: [src/service.py]
    max_chunks: 3
""",
        encoding="utf-8",
    )
    state = tmp_path / "state"

    result = runner.invoke(
        app,
        [
            "eval",
            "retrieval",
            str(manifest),
            "--source",
            str(workspace),
            "--state-dir",
            str(state),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    report = payload["report"]
    assert report["hit_count"] == 1
    assert report["hit_rate_at_case_k"] == 1.0
    assert report["paid_model_called"] is False
    assert report["network_called"] is False
    report_ref = payload["report_ref"]
    report_path = state / "artifacts" / report_ref[:2] / report_ref
    assert report_path.is_file()
    assert json.loads(report_path.read_text(encoding="utf-8"))["benchmark_id"] == "cli-fixture"


def test_retrieval_eval_rejects_visible_state_inside_source(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        """benchmark_id: invalid-state
cases:
  - case_id: missing
    query: missing
    expected_paths: [src/missing.py]
""",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "eval",
            "retrieval",
            str(manifest),
            "--source",
            str(workspace),
            "--state-dir",
            str(workspace / "visible-state"),
        ],
    )

    assert result.exit_code == 2
    assert "source/.horizon" in result.output
    assert not (workspace / "visible-state").exists()


def test_checked_in_symbol_ambiguity_diagnostic_prefers_module_context(tmp_path):
    root = Path(__file__).resolve().parents[2]
    result = runner.invoke(
        app,
        [
            "eval",
            "retrieval",
            str(root / "benchmarks/retrieval/horizon-symbol-ambiguity-v1.yaml"),
            "--source",
            str(root / "benchmarks/retrieval/fixtures/symbol-ambiguity-v1"),
            "--state-dir",
            str(tmp_path / "state"),
        ],
    )

    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)["report"]
    assert report["hit_count"] == 2
    assert report["hit_rate_at_case_k"] == 1.0
    assert report["mean_reciprocal_rank"] == 1.0
    assert report["leakage_count"] == 0
    assert report["paid_model_called"] is False
    assert report["network_called"] is False


def test_retrieval_suite_cli_reports_cutoff_metrics_and_reuses_warm_indexes(tmp_path):
    suite_root = tmp_path / "suite"
    cases_root = suite_root / "cases"
    sources_root = tmp_path / "sources"
    cases_root.mkdir(parents=True)
    for name, content in (
        ("first", "def reconcile_alpha():\n    return 'alpha'\n"),
        ("second", "def unrelated_beta():\n    return 'beta'\n"),
    ):
        source = sources_root / name / "src"
        source.mkdir(parents=True)
        (source / f"{name}.py").write_text(content, encoding="utf-8")

    (cases_root / "first.yaml").write_text(
        """schema_version: 1
benchmark_id: first-benchmark
allowed_paths: [src/**]
cases:
  - case_id: first
    query: reconcile alpha
    expected_paths: [src/first.py]
    max_chunks: 3
""",
        encoding="utf-8",
    )
    (cases_root / "second.yaml").write_text(
        """schema_version: 1
benchmark_id: second-benchmark
allowed_paths: [src/**]
cases:
  - case_id: second
    query: symbol that is absent
    expected_paths: [src/second.py]
    max_chunks: 3
""",
        encoding="utf-8",
    )
    suite = suite_root / "suite.yaml"
    suite.write_text(
        """schema_version: 1
suite_id: cli-suite
selection_commit: "0000000000000000000000000000000000000000"
partition: development
cutoffs: [1, 3]
cases:
  - case_id: first
    manifest_path: cases/first.yaml
    source_subdir: first
    buggy_commit: "1111111111111111111111111111111111111111"
  - case_id: second
    manifest_path: cases/second.yaml
    source_subdir: second
    buggy_commit: "2222222222222222222222222222222222222222"
""",
        encoding="utf-8",
    )
    state = tmp_path / "state"

    result = runner.invoke(
        app,
        [
            "eval",
            "retrieval-suite",
            str(suite),
            "--sources-root",
            str(sources_root),
            "--state-dir",
            str(state),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    report = payload["report"]
    assert report["case_count"] == 2
    assert [metric["cutoff"] for metric in report["metrics_at_k"]] == [1, 3]
    assert report["metrics_at_k"][0]["hit_count"] == 1
    assert report["metrics_at_k"][0]["hit_rate"] == 0.5
    assert report["empty_count"] == 1
    assert report["cold_duration_p50_ms"] >= 0
    assert report["warm_duration_p95_ms"] >= 0
    assert report["paid_model_called"] is False
    assert report["network_called"] is False
    assert report["repository_code_executed"] is False
    for case in report["cases"]:
        report_ref = case["report_ref"]
        assert (state / "artifacts" / report_ref[:2] / report_ref).is_file()
    suite_ref = payload["report_ref"]
    assert (state / "artifacts" / suite_ref[:2] / suite_ref).is_file()

    baseline = {
        "suite_id": report["suite_id"],
        "selection_commit": report["selection_commit"],
        "aggregate": {
            "case_count": report["case_count"],
            "expected_path_count": report["metrics_at_k"][0]["expected_path_count"],
            "mean_reciprocal_rank": report["mean_reciprocal_rank"],
            "leakage_count": report["leakage_count"],
            "empty_count": report["empty_count"],
            "degraded_count": report["degraded_count"],
        },
        "metrics_at_k": report["metrics_at_k"],
        "cases": [
            {
                "case_id": case["case_id"],
                "expected_paths": case["report"]["cases"][0]["expected_paths"],
                "returned_paths": case["report"]["cases"][0]["returned_paths"],
                "first_relevant_rank": case["report"]["cases"][0]["first_relevant_rank"],
                "relevant_path_count": case["report"]["cases"][0]["relevant_path_count"],
                "path_recall_at_8": case["report"]["cases"][0]["path_recall_at_k"],
                "retrieval_status": case["report"]["cases"][0]["retrieval_status"],
                "degradation_reasons": case["report"]["cases"][0]["degradation_reasons"],
                "report_ref": case["report_ref"],
            }
            for case in report["cases"]
        ],
    }
    baseline_path = suite_root / "baseline.yaml"
    baseline_path.write_text(yaml.safe_dump(baseline, sort_keys=False), encoding="utf-8")
    verified = runner.invoke(
        app,
        [
            "eval",
            "retrieval-suite",
            str(suite),
            "--sources-root",
            str(sources_root),
            "--state-dir",
            str(tmp_path / "verified-state"),
            "--baseline",
            str(baseline_path),
        ],
    )
    assert verified.exit_code == 0, verified.output
    verified_payload = json.loads(verified.stdout)
    assert verified_payload["baseline_verified"] is True
    assert len(verified_payload["baseline_projection_digest"]) == 64

    repeated = runner.invoke(
        app,
        [
            "eval",
            "retrieval-suite",
            str(suite),
            "--sources-root",
            str(sources_root),
            "--state-dir",
            str(state),
        ],
    )
    assert repeated.exit_code == 2
    assert "must be new" in repeated.output
