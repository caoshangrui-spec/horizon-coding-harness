from decimal import Decimal
from pathlib import Path

import yaml

from horizon.adapters.model.config import load_provider_config
from horizon.application.model_probe import conservative_input_estimate
from horizon.application.model_sizing import build_agent_execution_sizing_boundary
from horizon.domain.common import canonical_json, digest
from horizon.domain.pilot import RealModelPilotManifest
from horizon.domain.smoke_evaluation import RealModelSmokeSuite

ROOT = Path(__file__).resolve().parents[2]
SUITE_PATH = ROOT / "benchmarks" / "real-model-smoke-v1.yaml"
SELECTION_PATH = ROOT / "benchmarks" / "real-model-smoke-candidate-selection-v1.yaml"
SELECTION_V2_PATH = ROOT / "benchmarks" / "real-model-smoke-candidate-selection-v2.yaml"
PILOT_PATH = (
    ROOT
    / "benchmarks"
    / "run_ab"
    / "full"
    / "thefuck-2-executable-path-separator"
    / "real-model-smoke-pilot-v1.yaml"
)
PROVIDER_PATH = ROOT / "config" / "providers" / "siliconflow-thefuck2-smoke-v1.yaml"
LUIGI_PILOT_PATH = (
    ROOT
    / "benchmarks"
    / "run_ab"
    / "full"
    / "luigi-2-beam-target-path"
    / "real-model-smoke-pilot-v1.yaml"
)
LUIGI_PROVIDER_PATH = ROOT / "config" / "providers" / "siliconflow-luigi2-smoke-v1.yaml"
SANIC_PILOT_PATH = (
    ROOT
    / "benchmarks"
    / "run_ab"
    / "full"
    / "sanic-2-asyncio-start-serving"
    / "real-model-smoke-pilot-v1.yaml"
)
SANIC_PROVIDER_PATH = ROOT / "config" / "providers" / "siliconflow-sanic2-smoke-v1.yaml"
COMPARISON_PROVIDER_PATH = (
    ROOT / "config" / "providers" / "siliconflow-smoke-candidate-preflight-v1.yaml"
)
COMPARISON_MANIFESTS = {
    task_id: ROOT / "benchmarks" / path
    for task_id, path in {
        "httpie-1-unique-download-filename": (
            "run_ab/full/httpie-1-unique-download-filename/real-model-smoke-candidate-v1.yaml"
        ),
        "luigi-2-beam-target-path": (
            "run_ab/full/luigi-2-beam-target-path/real-model-smoke-candidate-v1.yaml"
        ),
        "sanic-2-asyncio-start-serving": (
            "run_ab/full/sanic-2-asyncio-start-serving/real-model-smoke-candidate-v1.yaml"
        ),
        "tornado-2-redirect-put-without-body": (
            "run_ab/full/tornado-2-redirect-put-without-body/real-model-smoke-candidate-v1.yaml"
        ),
    }.items()
}
FULL_MANIFESTS = {
    task_id: path.parent / "manifest.yaml" for task_id, path in COMPARISON_MANIFESTS.items()
}
RETRIEVAL_BASELINE_PATH = (
    ROOT / "benchmarks" / "retrieval" / "external-development-v3" / "baseline-index-v8.yaml"
)
QUERY_NORMALIZATION_BROAD_PATH = (
    ROOT
    / "benchmarks"
    / "retrieval"
    / "external-development-v3"
    / "query-normalization-gate-broad-v1"
    / "result-v1.yaml"
)
QUERY_ROBUSTNESS_PATH = (
    ROOT
    / "benchmarks"
    / "retrieval"
    / "external-development-v3"
    / "query-robustness-v1"
    / "result-v1.yaml"
)


def _load_model(path: Path, model_type):
    return model_type.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def test_thefuck_smoke_pilot_preserves_the_frozen_suite_contract():
    suite = _load_model(SUITE_PATH, RealModelSmokeSuite)
    pilot = _load_model(PILOT_PATH, RealModelPilotManifest)
    selected = next(
        task for task in suite.tasks if task.task_id == "thefuck-2-executable-path-separator"
    )

    assert pilot.source.project.casefold() == "thefuck"
    assert pilot.source.bug_id == "2"
    assert pilot.task.constraints.allowed_paths == selected.allowed_path_patterns
    assert pilot.task.constraints.allowed_tools == selected.allowed_tools
    assert pilot.task.budgets.max_steps == suite.execution_policy.max_steps
    assert pilot.task.budgets.max_model_calls == suite.execution_policy.max_model_calls
    assert pilot.task.budgets.max_tool_calls == suite.execution_policy.max_tool_calls
    assert pilot.task.budgets.max_wall_time_seconds == (
        suite.execution_policy.max_wall_time_seconds
    )
    assert pilot.task.budgets.max_repair_cycles == suite.execution_policy.max_repair_cycles
    assert pilot.sha256 == "565416d2335e7a1342b9fcc85138234959bb58aeb0254df4decb6f8d0059ce1c"


def test_thefuck_smoke_provider_is_single_attempt_unapproved_and_solution_isolated():
    pilot = _load_model(PILOT_PATH, RealModelPilotManifest)
    provider = load_provider_config(PROVIDER_PATH)

    assert provider.policy_id == pilot.provider_policy_id
    assert provider.model.id == "deepseek-ai/DeepSeek-V4-Flash"
    assert provider.request.max_attempts == 1
    assert provider.fallback_enabled is False
    assert provider.run_budget.max_cost == Decimal("0.060")
    assert provider.campaign.max_cost == Decimal("0.060")
    assert digest(provider) == "5374ada50d08c01adfe36d69a5f30633a987f5be83bb14c9682cec53b71358de"

    visible = canonical_json(pilot.task).casefold()
    assert pilot.source.fixed_commit not in visible
    assert pilot.source.fix_url.casefold() not in visible
    assert all(
        literal.casefold() not in visible
        for literal in pilot.solution_isolation.forbidden_task_literals
    )


def test_thefuck_smoke_budget_cannot_fund_the_bounded_execution_sizing_case():
    pilot = _load_model(PILOT_PATH, RealModelPilotManifest)
    provider = load_provider_config(PROVIDER_PATH)
    assert pilot.task.constraints.allowed_tools is not None

    boundary = build_agent_execution_sizing_boundary(
        provider.model.id,
        allowed_tool_names=pilot.task.constraints.allowed_tools,
        max_context_chars=provider.request.max_context_chars,
        max_output_tokens=provider.request.max_output_tokens,
        enable_thinking=provider.request.enable_thinking,
    )
    estimate = conservative_input_estimate(boundary.request)
    reservation = provider.pricing.reserve_cost(
        estimate.token_ceiling,
        provider.request.max_output_tokens,
    )

    assert boundary.context_chars == 8_000
    assert estimate.request_bytes == 10_308
    assert estimate.token_ceiling == 21_640
    assert reservation == Decimal("0.069528")
    assert reservation > provider.campaign.max_cost_per_call
    assert reservation > provider.run_budget.max_cost


def test_luigi_smoke_paid_contract_is_dedicated_bounded_and_solution_isolated():
    suite = _load_model(SUITE_PATH, RealModelSmokeSuite)
    pilot = _load_model(LUIGI_PILOT_PATH, RealModelPilotManifest)
    provider = load_provider_config(LUIGI_PROVIDER_PATH)
    selected = next(task for task in suite.tasks if task.task_id == "luigi-2-beam-target-path")

    assert pilot.source.project == "luigi"
    assert pilot.source.bug_id == "2"
    assert pilot.task.constraints.allowed_paths == selected.allowed_path_patterns
    assert pilot.task.constraints.allowed_tools == selected.allowed_tools
    assert pilot.provider_policy_id == provider.policy_id
    assert provider.model.id == "deepseek-ai/DeepSeek-V4-Flash"
    assert provider.request.max_attempts == 1
    assert provider.fallback_enabled is False
    assert provider.run_budget.max_cost == Decimal("0.120")
    assert provider.campaign.max_cost == Decimal("0.120")
    assert provider.campaign.max_cost_per_call == Decimal("0.120")
    assert provider.campaign.campaign_id == "siliconflow-luigi2-smoke-2026-11-v1"
    assert pilot.sha256 == "7a60400f58e15d4f160d57db62ec3968fda1a4be5ad169d3fa765e11cd74d632"
    assert digest(provider) == "15c43c7af4f32904d2c057446983904df2f7e49118ce13e57bfa1354ae2f91f9"

    visible = canonical_json(pilot.task).casefold()
    assert pilot.source.fixed_commit not in visible
    assert pilot.source.fix_url.casefold() not in visible
    assert all(
        literal.casefold() not in visible
        for literal in pilot.solution_isolation.forbidden_task_literals
    )


def test_sanic_smoke_paid_contract_is_dedicated_bounded_and_solution_isolated():
    suite = _load_model(SUITE_PATH, RealModelSmokeSuite)
    pilot = _load_model(SANIC_PILOT_PATH, RealModelPilotManifest)
    provider = load_provider_config(SANIC_PROVIDER_PATH)
    selected = next(task for task in suite.tasks if task.task_id == "sanic-2-asyncio-start-serving")

    assert pilot.source.project == "sanic"
    assert pilot.source.bug_id == "2"
    assert pilot.task.constraints.allowed_paths == selected.allowed_path_patterns
    assert pilot.task.constraints.allowed_tools == selected.allowed_tools
    assert pilot.provider_policy_id == provider.policy_id
    assert provider.model.id == "deepseek-ai/DeepSeek-V4-Flash"
    assert provider.request.max_attempts == 1
    assert provider.fallback_enabled is False
    assert provider.run_budget.max_cost == Decimal("0.120")
    assert provider.campaign.max_cost == Decimal("0.120")
    assert provider.campaign.max_cost_per_call == Decimal("0.120")
    assert provider.campaign.campaign_id == "siliconflow-sanic2-smoke-2026-11-v1"
    assert pilot.sha256 == "a0397ab9790bab1404c68a559120c4d08b656ea16ae54f5ce6c6072c5ce5f881"
    assert digest(provider) == "b10a5a2475590a28bf894e665f870bd465d86adfb4a64891e302d9b54983716b"

    visible = canonical_json(pilot.task).casefold()
    assert pilot.source.fixed_commit not in visible
    assert pilot.source.fix_url.casefold() not in visible
    assert all(
        literal.casefold() not in visible
        for literal in pilot.solution_isolation.forbidden_task_literals
    )


def test_remaining_smoke_candidates_share_one_zero_spend_comparison_contract():
    suite = _load_model(SUITE_PATH, RealModelSmokeSuite)
    provider = load_provider_config(COMPARISON_PROVIDER_PATH)
    selected = {task.task_id: task for task in suite.tasks}

    assert provider.model.id == "deepseek-ai/DeepSeek-V4-Flash"
    assert provider.request.max_attempts == 1
    assert provider.fallback_enabled is False
    assert provider.run_budget.max_cost == Decimal("0.120")
    assert provider.campaign.max_cost == Decimal("0.120")
    assert provider.campaign.max_cost_per_call == Decimal("0.120")

    for task_id, path in COMPARISON_MANIFESTS.items():
        pilot = _load_model(path, RealModelPilotManifest)
        smoke_task = selected[task_id]
        assert pilot.provider_policy_id == provider.policy_id
        assert pilot.max_paid_cost == provider.run_budget.max_cost
        assert pilot.task.repository.base_commit == pilot.source.buggy_commit
        assert pilot.task.constraints.allowed_paths == smoke_task.allowed_path_patterns
        assert pilot.task.constraints.allowed_tools == smoke_task.allowed_tools
        assert pilot.task.budgets.max_steps == suite.execution_policy.max_steps
        assert pilot.task.budgets.max_model_calls == suite.execution_policy.max_model_calls
        assert pilot.task.budgets.max_tool_calls == suite.execution_policy.max_tool_calls
        assert pilot.task.budgets.max_wall_time_seconds == (
            suite.execution_policy.max_wall_time_seconds
        )
        assert pilot.task.budgets.max_repair_cycles == (suite.execution_policy.max_repair_cycles)
        visible = canonical_json(pilot.task).casefold()
        assert pilot.source.fixed_commit not in visible
        assert pilot.source.fix_url.casefold() not in visible
        assert all(
            literal.casefold() not in visible
            for literal in pilot.solution_isolation.forbidden_task_literals
        )


def test_candidate_selection_keeps_known_evidence_and_limitations_explicit():
    selection = yaml.safe_load(SELECTION_PATH.read_text(encoding="utf-8"))
    suite = _load_model(SUITE_PATH, RealModelSmokeSuite)
    suite_tasks = {task.task_id: task for task in suite.tasks}
    retrieval = yaml.safe_load(RETRIEVAL_BASELINE_PATH.read_text(encoding="utf-8"))
    retrieval_cases = {case["case_id"]: case for case in retrieval["cases"]}
    rows = {row["task_id"]: row for row in selection["candidates"]}

    expected_ids = {task.task_id for task in suite.tasks} - {"thefuck-2-executable-path-separator"}
    assert set(rows) == expected_ids == set(COMPARISON_MANIFESTS)
    assert selection["selected_task_id"] == "luigi-2-beam-target-path"
    assert selection["paid_execution_authorized"] is False
    assert selection["external_model_called"] is False
    assert selection["network_called_by_evaluator"] is False
    assert all(row["exact_checkout_preflight_ready"] for row in rows.values())
    assert all(row["initial_failure_observed"] for row in rows.values())
    assert all(row["source_unchanged"] for row in rows.values())

    for task_id, row in rows.items():
        full_manifest = yaml.safe_load(FULL_MANIFESTS[task_id].read_text(encoding="utf-8"))
        treatment = next(arm for arm in full_manifest["arms"] if arm["role"] == "single_replan")
        replacements = [
            action for action in treatment["actions"] if action["tool"] == "replace_text"
        ]
        replaced_occurrences = sum(
            action["arguments"].get("expected_occurrences", 1) for action in replacements
        )
        frozen = retrieval_cases[task_id]

        assert row["difficulty_band"] == suite_tasks[task_id].difficulty_band
        assert row["scripted_replace_calls"] == len(replacements)
        assert row["scripted_replaced_occurrences"] == replaced_occurrences
        assert row["frozen_target_path_rank"] == frozen["first_relevant_rank"]
        assert row["frozen_retrieval_status"] == frozen["retrieval_status"]
        assert row.get("frozen_degradation_reasons", []) == frozen["degradation_reasons"]

    assert rows["luigi-2-beam-target-path"]["scripted_replace_calls"] == 1
    assert rows["luigi-2-beam-target-path"]["scripted_replaced_occurrences"] == 1
    assert rows["luigi-2-beam-target-path"]["frozen_target_path_rank"] == 1
    assert rows["luigi-2-beam-target-path"]["frozen_retrieval_status"] == "degraded"
    assert "holdout" in " ".join(selection["limitations"])


def test_post_run_candidate_selection_abstains_when_no_candidate_passes_both_gates():
    selection = yaml.safe_load(SELECTION_V2_PATH.read_text(encoding="utf-8"))
    suite = _load_model(SUITE_PATH, RealModelSmokeSuite)
    suite_tasks = {task.task_id: task for task in suite.tasks}
    retrieval = yaml.safe_load(RETRIEVAL_BASELINE_PATH.read_text(encoding="utf-8"))
    retrieval_cases = {case["case_id"]: case for case in retrieval["cases"]}
    broad = yaml.safe_load(QUERY_NORMALIZATION_BROAD_PATH.read_text(encoding="utf-8"))
    broad_groups = {group["base_case_id"]: group for group in broad["groups"]}
    robustness = yaml.safe_load(QUERY_ROBUSTNESS_PATH.read_text(encoding="utf-8"))
    robustness_groups = {group["base_case_id"]: group for group in robustness["groups"]}
    observed = {row["task_id"]: row for row in selection["observed_paid_runs"]}
    rows = {row["task_id"]: row for row in selection["candidates"]}

    assert selection["decision"] == "abstain"
    assert selection["selected_task_id"] is None
    assert selection["paid_execution_authorized"] is False
    assert selection["external_model_called"] is False
    assert selection["network_called_by_evaluator"] is False
    assert set(observed) == {
        "thefuck-2-executable-path-separator",
        "luigi-2-beam-target-path",
        "sanic-2-asyncio-start-serving",
    }
    assert (
        set(rows)
        == set(suite_tasks) - set(observed)
        == {
            "httpie-1-unique-download-filename",
            "tornado-2-redirect-put-without-body",
        }
    )

    summary = selection["observed_paid_runs_summary"]
    assert summary["evaluated_task_count"] == len(observed) == 3
    assert summary["suite_task_count"] == len(suite_tasks) == 5
    assert summary["evaluated_success_count"] == 0
    assert summary["budget_gate_stop_count"] == 3
    assert summary["valid_edit_count"] == 0
    assert summary["protected_validation_pass_count"] == 0
    assert summary["settled_model_call_count"] == sum(
        row["model_calls"] for row in observed.values()
    )
    assert Decimal(summary["settled_cost_cny"]) == sum(
        (Decimal(row["settled_cost_cny"]) for row in observed.values()),
        start=Decimal("0"),
    )
    assert all(row["task_success"] is False for row in observed.values())
    assert all(row["valid_edit_produced"] is False for row in observed.values())
    assert all(row["stop_category"] == "budget_gate" for row in observed.values())
    assert all(len(row["smoke_result_ref"]) == 64 for row in observed.values())
    assert all(len(row["trace_sha256"]) == 64 for row in observed.values())
    for row in observed.values():
        regression_path = row["zero_cost_regression"].split("::", maxsplit=1)[0]
        assert (ROOT / regression_path).exists()

    httpie_variants = {
        name: evidence["baseline_rank"]
        for name, evidence in broad_groups["httpie-1-unique-download-filename"]["variants"].items()
    }
    tornado_variants = {
        case["variant"]: case["first_relevant_rank"]
        for case in robustness_groups["tornado-2-redirect-put-without-body"]["cases"]
    }
    frozen_variants = {
        "httpie-1-unique-download-filename": httpie_variants,
        "tornado-2-redirect-put-without-body": tornado_variants,
    }

    for task_id, row in rows.items():
        full_manifest = yaml.safe_load(FULL_MANIFESTS[task_id].read_text(encoding="utf-8"))
        treatment = next(arm for arm in full_manifest["arms"] if arm["role"] == "single_replan")
        replacements = [
            action for action in treatment["actions"] if action["tool"] == "replace_text"
        ]
        replaced_occurrences = sum(
            action["arguments"].get("expected_occurrences", 1) for action in replacements
        )
        planning = Decimal(row["local_planning_reserved_cost_cny"])
        execution = Decimal(row["local_bounded_execution_reserved_cost_cny"])
        cumulative = Decimal(row["local_planning_execution_reserved_cost_cny"])
        rank_gate = (
            row["frozen_target_path_rank"] == 1
            and row["frozen_query_variant_ranks"]["raw_identifier"] == 1
            and row["frozen_query_variant_ranks"]["english_paraphrase"] == 1
        )
        single_replace_gate = len(replacements) == 1

        assert row["difficulty_band"] == suite_tasks[task_id].difficulty_band
        assert row["exact_checkout_preflight_ready"] is True
        assert row["initial_failure_observed"] is True
        assert row["source_unchanged"] is True
        assert row["solution_isolation_passed"] is True
        assert cumulative == planning + execution
        assert cumulative <= Decimal(selection["proposal_run_cap_cny"])
        assert len(row["local_preflight_report_ref"]) == 64
        assert row["frozen_target_path_rank"] == retrieval_cases[task_id]["first_relevant_rank"]
        assert row["frozen_query_variant_ranks"] == frozen_variants[task_id]
        assert row["scripted_replace_calls"] == len(replacements)
        assert row["scripted_replaced_occurrences"] == replaced_occurrences
        assert row["gates"]["target_rank_1_for_base_and_english_paraphrase"] is rank_gate
        assert row["gates"]["bounded_single_replace_call"] is single_replace_gate
        assert row["gates"]["all_required_gates_passed"] is (
            row["exact_checkout_preflight_ready"] and rank_gate and single_replace_gate
        )

    assert rows["httpie-1-unique-download-filename"]["gates"] == {
        "exact_checkout_preflight_ready": True,
        "target_rank_1_for_base_and_english_paraphrase": True,
        "bounded_single_replace_call": False,
        "all_required_gates_passed": False,
    }
    assert rows["tornado-2-redirect-put-without-body"]["gates"] == {
        "exact_checkout_preflight_ready": True,
        "target_rank_1_for_base_and_english_paraphrase": False,
        "bounded_single_replace_call": True,
        "all_required_gates_passed": False,
    }
    assert not any(row["gates"]["all_required_gates_passed"] for row in rows.values())
    assert "holdout" in " ".join(selection["limitations"])
