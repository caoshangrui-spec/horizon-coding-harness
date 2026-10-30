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
PILOT_PATH = (
    ROOT
    / "benchmarks"
    / "run_ab"
    / "full"
    / "thefuck-2-executable-path-separator"
    / "real-model-smoke-pilot-v1.yaml"
)
PROVIDER_PATH = ROOT / "config" / "providers" / "siliconflow-thefuck2-smoke-v1.yaml"
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
    assert estimate.request_bytes == 10_293
    assert estimate.token_ceiling == 21_610
    assert reservation == Decimal("0.069438")
    assert reservation > provider.campaign.max_cost_per_call
    assert reservation > provider.run_budget.max_cost


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
