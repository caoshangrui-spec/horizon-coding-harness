from decimal import Decimal
from pathlib import Path

import yaml

from horizon.adapters.model.config import load_provider_config
from horizon.domain.common import canonical_json
from horizon.domain.pilot import RealModelPilotManifest
from horizon.domain.smoke_evaluation import RealModelSmokeSuite

ROOT = Path(__file__).resolve().parents[2]
SELECTION_PATH = ROOT / "benchmarks" / "real-model-post-smoke-candidate-selection-v1.yaml"
REGISTRY_PATH = ROOT / "benchmarks" / "independent-tasks-v1.yaml"
SMOKE_PATH = ROOT / "benchmarks" / "real-model-smoke-v1.yaml"
BASELINE_PATH = (
    ROOT / "benchmarks" / "retrieval" / "external-development-v3" / "baseline-index-v8.yaml"
)
BROAD_PATH = (
    ROOT
    / "benchmarks"
    / "retrieval"
    / "external-development-v3"
    / "query-normalization-gate-broad-v1"
    / "result-v1.yaml"
)
HARD_PATH = (
    ROOT
    / "benchmarks"
    / "retrieval"
    / "external-development-v3"
    / "query-robustness-v1"
    / "result-v1.yaml"
)
PROVIDER_PATH = ROOT / "config" / "providers" / "siliconflow-smoke-candidate-preflight-v1.yaml"
CANDIDATE_MANIFESTS = {
    "sanic-5-root-logger-config": (
        ROOT
        / "benchmarks"
        / "run_ab"
        / "full"
        / "sanic-5-root-logger-config"
        / "real-model-post-smoke-candidate-v1.yaml"
    ),
    "thefuck-5-git-push-bitbucket": (
        ROOT
        / "benchmarks"
        / "run_ab"
        / "full"
        / "thefuck-5-git-push-bitbucket"
        / "real-model-post-smoke-candidate-v1.yaml"
    ),
}


def _yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_post_smoke_selection_is_derived_from_the_revealed_development_population():
    selection = _yaml(SELECTION_PATH)
    registry = _yaml(REGISTRY_PATH)
    smoke = RealModelSmokeSuite.model_validate(_yaml(SMOKE_PATH))
    baseline = {row["case_id"]: row for row in _yaml(BASELINE_PATH)["cases"]}
    broad = {row["base_case_id"]: row for row in _yaml(BROAD_PATH)["groups"]}
    hard = {row["base_case_id"]: row for row in _yaml(HARD_PATH)["groups"]}
    rows = {row["task_id"]: row for row in selection["screened_population"]}

    development_ids = {
        task["task_id"] for task in registry["tasks"] if task["partition"] == "development"
    }
    smoke_ids = {task.task_id for task in smoke.tasks}
    paid_ids = set(selection["historical_paid_task_ids"])
    assert set(rows) == development_ids - smoke_ids - paid_ids
    assert len(rows) == 10
    assert smoke_ids.isdisjoint(rows)

    for task_id, row in rows.items():
        frozen = baseline[task_id]
        assert row["frozen_retrieval_status"] == frozen["retrieval_status"]
        assert row["frozen_target_path_rank"] == frozen["first_relevant_rank"]

        if task_id in broad:
            variants = broad[task_id]["variants"]
            ranks = {name: evidence["baseline_rank"] for name, evidence in variants.items()}
        else:
            ranks = {
                case["variant"]: case["first_relevant_rank"] for case in hard[task_id]["cases"]
            }
        assert row["raw_identifier_rank"] == ranks["raw_identifier"]
        assert row["english_paraphrase_rank"] == ranks["english_paraphrase"]
        assert row["additional_english_rank"] == ranks[row["additional_english_variant"]]

        manifest_path = row["full_checkout_manifest_path"]
        if manifest_path is None:
            replace_calls = None
            replaced_occurrences = None
        else:
            manifest = _yaml(ROOT / "benchmarks" / manifest_path)
            treatment = next(arm for arm in manifest["arms"] if arm["role"] == "single_replan")
            replacements = [
                action for action in treatment["actions"] if action["tool"] == "replace_text"
            ]
            replace_calls = len(replacements)
            replaced_occurrences = sum(
                action["arguments"].get("expected_occurrences", 1) for action in replacements
            )
        assert row["scripted_replace_calls"] == replace_calls
        assert row["scripted_replaced_occurrences"] == replaced_occurrences

        all_static_gates_passed = (
            manifest_path is not None
            and row["frozen_retrieval_status"] == "ok"
            and row["frozen_target_path_rank"] == 1
            and row["raw_identifier_rank"] == 1
            and row["english_paraphrase_rank"] == 1
            and replace_calls == 1
            and replaced_occurrences == 1
        )
        assert row["all_static_gates_passed"] is all_static_gates_passed

    survivors = {task_id for task_id, row in rows.items() if row["all_static_gates_passed"]}
    assert survivors == set(CANDIDATE_MANIFESTS)


def test_post_smoke_tie_breakers_select_sanic_without_authorizing_spend():
    selection = _yaml(SELECTION_PATH)
    rows = {row["task_id"]: row for row in selection["screened_population"]}
    preflights = {row["task_id"]: row for row in selection["fresh_zero_spend_preflights"]}
    sanic = preflights["sanic-5-root-logger-config"]
    thefuck = preflights["thefuck-5-git-push-bitbucket"]

    assert selection["selected_task_id"] == "sanic-5-root-logger-config"
    assert selection["original_smoke_suite_unchanged"] is True
    assert selection["paid_execution_authorized"] is False
    assert selection["external_model_called"] is False
    assert selection["network_called_by_evaluator"] is False
    assert set(preflights) == set(CANDIDATE_MANIFESTS)
    assert all(row["ready"] for row in preflights.values())
    assert all(row["initial_failure_observed"] for row in preflights.values())
    assert all(row["source_unchanged"] for row in preflights.values())
    assert all(row["solution_isolation_passed"] for row in preflights.values())

    for row in preflights.values():
        planning = Decimal(row["planning_reserved_cost_cny"])
        execution = Decimal(row["bounded_execution_reserved_cost_cny"])
        cumulative = Decimal(row["planning_execution_reserved_cost_cny"])
        assert cumulative == planning + execution
        assert cumulative <= Decimal(selection["proposal_run_cap_cny"])
        assert len(row["local_report_ref"]) == 64

    assert (
        rows["sanic-5-root-logger-config"]["additional_english_rank"]
        < rows["thefuck-5-git-push-bitbucket"]["additional_english_rank"]
    )
    assert sanic["exact_checkout_file_count"] < thefuck["exact_checkout_file_count"]
    assert Decimal(sanic["planning_execution_reserved_cost_cny"]) < Decimal(
        thefuck["planning_execution_reserved_cost_cny"]
    )
    assert "post-hoc" in " ".join(selection["limitations"])


def test_post_smoke_candidate_manifests_are_bounded_and_solution_isolated():
    selection = _yaml(SELECTION_PATH)
    smoke = RealModelSmokeSuite.model_validate(_yaml(SMOKE_PATH))
    smoke_ids = {task.task_id for task in smoke.tasks}
    provider = load_provider_config(PROVIDER_PATH)

    assert provider.model.id == "deepseek-ai/DeepSeek-V4-Flash"
    assert provider.request.max_attempts == 1
    assert provider.fallback_enabled is False
    assert provider.run_budget.max_cost == Decimal("0.120")
    assert provider.campaign.max_cost == Decimal("0.120")

    for task_id, path in CANDIDATE_MANIFESTS.items():
        pilot = RealModelPilotManifest.model_validate(_yaml(path))
        visible = canonical_json(pilot.task).casefold()

        assert task_id not in smoke_ids
        assert pilot.provider_policy_id == provider.policy_id
        assert pilot.max_paid_cost == Decimal(selection["proposal_run_cap_cny"])
        assert pilot.task.repository.base_commit == pilot.source.buggy_commit
        assert pilot.task.constraints.network == "deny"
        assert pilot.task.constraints.allowed_tools == (
            "read_file",
            "retrieve_code",
            "replace_text",
            "run_check",
        )
        assert pilot.task.budgets.max_steps == 24
        assert pilot.task.budgets.max_model_calls == 14
        assert pilot.task.budgets.max_tool_calls == 24
        assert pilot.task.budgets.max_wall_time_seconds == 900
        assert pilot.task.budgets.max_repair_cycles == 2
        assert pilot.source.fixed_commit not in visible
        assert pilot.source.fix_url.casefold() not in visible
        assert all(
            literal.casefold() not in visible
            for literal in pilot.solution_isolation.forbidden_task_literals
        )
