from decimal import Decimal
from pathlib import Path

import yaml

from horizon.adapters.model.config import load_provider_config
from horizon.domain.common import canonical_json, digest
from horizon.domain.pilot import RealModelPilotManifest
from horizon.domain.run_evaluation import RunABEvalManifest
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
PAID_PROVIDER_PATH = ROOT / "config" / "providers" / "siliconflow-sanic5-post-smoke-v1.yaml"
PAID_PILOT_PATH = (
    ROOT
    / "benchmarks"
    / "run_ab"
    / "full"
    / "sanic-5-root-logger-config"
    / "real-model-post-smoke-pilot-v1.yaml"
)
PAID_RESULT_PATH = ROOT / "benchmarks" / "real-model-post-smoke-sanic5-result-v1.yaml"
EVIDENCE_TRANSITION_PROVIDER_PATH = (
    ROOT / "config" / "providers" / "siliconflow-sanic5-evidence-transition-v2.yaml"
)
EVIDENCE_TRANSITION_PILOT_PATH = (
    ROOT
    / "benchmarks"
    / "run_ab"
    / "full"
    / "sanic-5-root-logger-config"
    / "real-model-evidence-transition-pilot-v2.yaml"
)
EVIDENCE_TRANSITION_RESULT_PATH = (
    ROOT / "benchmarks" / "real-model-post-smoke-sanic5-result-v2.yaml"
)
FIRST_CLOSURE_PROVIDER_PATH = (
    ROOT / "config" / "providers" / "siliconflow-thefuck5-first-closure-v1.yaml"
)
FIRST_CLOSURE_PILOT_PATH = (
    ROOT
    / "benchmarks"
    / "run_ab"
    / "full"
    / "thefuck-5-git-push-bitbucket"
    / "real-model-first-closure-pilot-v1.yaml"
)
FIRST_CLOSURE_RESULT_PATH = ROOT / "benchmarks" / "real-model-post-smoke-thefuck5-result-v1.yaml"
FIRST_CLOSURE_REPLAY_MANIFEST_PATH = (
    ROOT
    / "benchmarks"
    / "run_ab"
    / "full"
    / "thefuck-5-git-push-bitbucket"
    / "real-model-failed-payload-replay-v1.yaml"
)
FIRST_CLOSURE_REPLAY_RESULT_PATH = (
    ROOT / "benchmarks" / "real-model-post-smoke-thefuck5-payload-replay-v1.yaml"
)
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


def test_sanic5_paid_follow_up_has_an_independent_one_run_budget_contract():
    selection = _yaml(SELECTION_PATH)
    smoke = RealModelSmokeSuite.model_validate(_yaml(SMOKE_PATH))
    provider = load_provider_config(PAID_PROVIDER_PATH)
    pilot = RealModelPilotManifest.model_validate(_yaml(PAID_PILOT_PATH))
    visible = canonical_json(pilot.task).casefold()

    assert selection["selected_task_id"] == "sanic-5-root-logger-config"
    assert selection["original_smoke_suite_unchanged"] is True
    assert pilot.pilot_id == "sanic-5-root-logger-config-post-smoke-v1"
    assert pilot.task.task_id not in {task.task_id for task in smoke.tasks}

    assert provider.policy_id == "siliconflow-deepseek-v4-flash-sanic5-post-smoke-012-v1"
    assert provider.model.id == "deepseek-ai/DeepSeek-V4-Flash"
    assert provider.request.max_attempts == 1
    assert provider.fallback_enabled is False
    assert provider.run_budget.max_cost == Decimal("0.120")
    assert provider.campaign.max_cost == Decimal("0.120")
    assert provider.campaign.max_cost_per_call == Decimal("0.120")
    assert provider.campaign.campaign_id == "siliconflow-sanic5-post-smoke-2026-11-v1"
    assert provider.ledger_path.endswith(".horizon/siliconflow-sanic5-post-smoke-campaign.sqlite3")

    assert pilot.provider_policy_id == provider.policy_id
    assert pilot.task.model_policy_id == provider.policy_id
    assert pilot.max_paid_cost == Decimal("0.120")
    assert pilot.task.repository.base_commit == pilot.source.buggy_commit
    assert pilot.task.constraints.allowed_paths == ("sanic/**",)
    assert pilot.task.constraints.network == "deny"
    assert pilot.task.constraints.allowed_tools == (
        "read_file",
        "retrieve_code",
        "replace_text",
        "run_check",
    )
    assert pilot.task.budgets.max_model_calls == 14
    assert pilot.task.budgets.max_cost_usd == Decimal("0.120")
    assert pilot.source.fixed_commit not in visible
    assert pilot.source.fix_url.casefold() not in visible
    assert all(
        literal.casefold() not in visible
        for literal in pilot.solution_isolation.forbidden_task_literals
    )


def test_sanic5_evidence_transition_follow_up_has_a_fresh_strict_budget_contract():
    smoke = RealModelSmokeSuite.model_validate(_yaml(SMOKE_PATH))
    provider = load_provider_config(EVIDENCE_TRANSITION_PROVIDER_PATH)
    pilot = RealModelPilotManifest.model_validate(_yaml(EVIDENCE_TRANSITION_PILOT_PATH))
    visible = canonical_json(pilot.task).casefold()

    assert pilot.pilot_id == "sanic-5-root-logger-config-evidence-transition-v2"
    assert pilot.task.task_id not in {task.task_id for task in smoke.tasks}
    assert provider.policy_id == pilot.provider_policy_id == pilot.task.model_policy_id
    assert provider.model.id == "deepseek-ai/DeepSeek-V4-Flash"
    assert provider.request.max_attempts == 1
    assert provider.fallback_enabled is False
    assert provider.run_budget.max_cost == Decimal("0.180")
    assert provider.campaign.max_cost == Decimal("0.180")
    assert provider.campaign.max_cost_per_call == Decimal("0.180")
    assert provider.campaign.campaign_id == ("siliconflow-sanic5-evidence-transition-2026-11-v2")
    assert provider.ledger_path.endswith(
        ".horizon/siliconflow-sanic5-evidence-transition-v2-campaign.sqlite3"
    )
    assert pilot.max_paid_cost == Decimal("0.180")
    assert pilot.task.budgets.max_cost_usd == Decimal("0.180")
    assert pilot.task.repository.base_commit == pilot.source.buggy_commit
    assert pilot.task.constraints.allowed_paths == ("sanic/**",)
    assert pilot.task.constraints.network == "deny"
    assert pilot.source.fixed_commit not in visible
    assert pilot.source.fix_url.casefold() not in visible
    assert all(
        literal.casefold() not in visible
        for literal in pilot.solution_isolation.forbidden_task_literals
    )


def test_thefuck5_first_closure_proposal_has_a_fresh_strict_budget_contract():
    selection = _yaml(SELECTION_PATH)
    smoke = RealModelSmokeSuite.model_validate(_yaml(SMOKE_PATH))
    provider = load_provider_config(FIRST_CLOSURE_PROVIDER_PATH)
    pilot = RealModelPilotManifest.model_validate(_yaml(FIRST_CLOSURE_PILOT_PATH))
    visible = canonical_json(pilot.task).casefold()

    assert selection["original_smoke_suite_unchanged"] is True
    assert selection["paid_execution_authorized"] is False
    assert pilot.pilot_id == "thefuck-5-git-push-bitbucket-first-closure-v1"
    assert pilot.task.task_id not in {task.task_id for task in smoke.tasks}
    assert provider.policy_id == pilot.provider_policy_id == pilot.task.model_policy_id
    assert provider.model.id == "deepseek-ai/DeepSeek-V4-Flash"
    assert provider.request.max_attempts == 1
    assert provider.fallback_enabled is False
    assert provider.run_budget.max_cost == Decimal("0.180")
    assert provider.campaign.max_cost == Decimal("0.180")
    assert provider.campaign.max_cost_per_call == Decimal("0.180")
    assert provider.campaign.campaign_id == ("siliconflow-thefuck5-first-closure-2026-11-v1")
    assert provider.ledger_path.endswith(
        ".horizon/siliconflow-thefuck5-first-closure-v1-campaign.sqlite3"
    )
    assert pilot.max_paid_cost == Decimal("0.180")
    assert pilot.task.budgets.max_cost_usd == Decimal("0.180")
    assert pilot.task.budgets.max_model_calls == 14
    assert pilot.task.repository.base_commit == pilot.source.buggy_commit
    assert pilot.task.constraints.allowed_paths == ("thefuck/**",)
    assert pilot.task.constraints.network == "deny"
    assert pilot.task.constraints.allowed_tools == (
        "read_file",
        "retrieve_code",
        "replace_text",
        "run_check",
    )
    assert pilot.source.fixed_commit not in visible
    assert pilot.source.fix_url.casefold() not in visible
    assert all(
        literal.casefold() not in visible
        for literal in pilot.solution_isolation.forbidden_task_literals
    )


def test_thefuck5_first_closure_result_freezes_the_failed_single_run():
    result = _yaml(FIRST_CLOSURE_RESULT_PATH)
    provider = load_provider_config(FIRST_CLOSURE_PROVIDER_PATH)
    smoke = RealModelSmokeSuite.model_validate(_yaml(SMOKE_PATH))
    run = result["run"]
    quality = result["observed_quality"]
    trace = result["trace"]

    assert result["classification"] == "post_hoc_development_follow_up"
    assert result["scope"] == {
        "original_smoke_suite_unchanged": True,
        "included_in_original_smoke_aggregate": False,
        "blind_or_holdout_evidence": False,
        "retry_performed": False,
        "fallback_performed": False,
    }
    assert result["task_id"] not in {task.task_id for task in smoke.tasks}
    assert run["run_id"] == "run_9695db52a95740a5a71ef02bf2b9910d"
    assert run["status"] == "FAILED"
    assert run["task_succeeded"] is False
    assert run["failure_reason"] == "campaign_cost_limit"
    assert run["provider_policy_id"] == provider.policy_id
    assert run["campaign_id"] == provider.campaign.campaign_id
    assert Decimal(run["run_cost_cap_cny"]) == provider.run_budget.max_cost
    assert (
        Decimal(run["settled_cost_cny"]) + Decimal(run["campaign_remaining_cost_cny"])
        == provider.campaign.max_cost
    )
    assert Decimal(run["campaign_reserved_cost_cny"]) == 0
    assert Decimal(run["campaign_unknown_cost_cny"]) == 0
    assert Decimal(run["budget_stop"]["required_cost_cny"]) > Decimal(
        run["budget_stop"]["available_cost_cny"]
    )
    assert run["model_calls"] == 13
    assert run["planning_calls"] == 1
    assert run["execution_calls"] == 12
    assert run["tool_calls"] == 12
    assert run["source_workspace_unchanged"] is True

    assert quality["correct_location_reached"] is True
    assert quality["correct_location_evidence"]["path"] == "thefuck/rules/git_push.py"
    assert quality["edit_intent_observed"] is True
    assert quality["valid_edit_produced"] is False
    assert quality["protected_validation_passed"] is None
    assert quality["acceptance_evidence_unknown"] is True
    assert quality["write_tool_calls"] == 1
    assert quality["successful_write_tool_calls"] == 0
    assert quality["protected_check_calls"] == 0
    assert quality["tool_sequence"][1] == {
        "name": "replace_text",
        "status": "error",
        "error": "exact_replacement_occurrence_mismatch",
    }

    assert trace["event_count"] == 185
    assert trace["terminal_pack_verified"] is True
    assert trace["open_effects_present"] is False
    assert trace["unknown_effects_present"] is False
    assert trace["open_reservations_present"] is False
    assert len(trace["trace_sha256"]) == 64
    assert len(trace["projection_hash"]) == 64


def test_thefuck5_failed_model_payload_replay_is_exact_and_zero_spend():
    manifest = RunABEvalManifest.model_validate(_yaml(FIRST_CLOSURE_REPLAY_MANIFEST_PATH))
    result = _yaml(FIRST_CLOSURE_REPLAY_RESULT_PATH)
    source = result["source_failure"]
    replay = result["replay"]

    treatment = next(arm for arm in manifest.arms if arm.role == "single_replan")
    edit = next(action for action in treatment.actions if action.tool == "replace_text")
    assert digest(edit.arguments) == source["arguments_hash"]
    assert edit.arguments == {
        "path": "thefuck/rules/git_push.py",
        "old": (
            "@git_support\n"
            "def match(command):\n"
            "    return ('push' in command.script_parts\n"
            "            and 'set-upstream' in command.output)"
        ),
        "new": (
            "@git_support\n"
            "def match(command):\n"
            "    return ('push' in command.script_parts\n"
            "            and re.search(r'git push --set-upstream', command.output) "
            "is not None)"
        ),
    }
    assert "\r" not in edit.arguments["old"]
    assert "\r" not in edit.arguments["new"]

    assert result["classification"] == "zero_spend_post_hoc_harness_regression"
    assert result["scope"] == {
        "paid_model_called": False,
        "network_called": False,
        "original_run_unchanged": True,
        "retry_or_fallback": False,
        "counts_as_real_model_success": False,
        "included_in_original_smoke_aggregate": False,
    }
    assert source["run_id"] == "run_9695db52a95740a5a71ef02bf2b9910d"
    assert source["target_path"] == "thefuck/rules/git_push.py"
    assert source["model_fragment_line_ending"] == "LF"
    assert source["source_file_line_ending"] == "CRLF"
    assert source["original_tool_status"] == "error"
    assert source["original_workspace_effect"] is False
    assert replay["manifest_digest"] == manifest.sha256
    assert replay["all_expectations_met"] is True
    assert replay["treatment_recovered"] is True
    assert replay["protected_validation_passed"] is True
    assert replay["successful_write_count"] == 1
    assert replay["evidence_backed_write_count"] == 1
    assert replay["context_bound_write_count"] == 1
    assert replay["trace_replay_verified"] is True
    assert replay["source_workspace_unchanged"] is True


def test_sanic5_paid_follow_up_freezes_the_negative_without_changing_smoke():
    result = _yaml(PAID_RESULT_PATH)
    provider = load_provider_config(PAID_PROVIDER_PATH)
    smoke = RealModelSmokeSuite.model_validate(_yaml(SMOKE_PATH))
    run = result["run"]
    quality = result["observed_quality"]
    trace = result["trace"]

    assert result["task_id"] == "sanic-5-root-logger-config"
    assert result["classification"] == "post_hoc_development_follow_up"
    assert result["scope"] == {
        "original_smoke_suite_unchanged": True,
        "included_in_original_smoke_aggregate": False,
        "blind_or_holdout_evidence": False,
        "retry_performed": False,
        "fallback_performed": False,
    }
    assert result["task_id"] not in {task.task_id for task in smoke.tasks}

    assert run["run_id"] == "run_6e40579b14734e7e979c0a059b0844df"
    assert run["status"] == "FAILED"
    assert run["task_succeeded"] is False
    assert run["failure_reason"] == "campaign_cost_limit"
    assert run["provider_policy_id"] == provider.policy_id
    assert run["campaign_id"] == provider.campaign.campaign_id
    assert Decimal(run["run_cost_cap_cny"]) == provider.run_budget.max_cost
    assert Decimal(run["campaign_cost_cap_cny"]) == provider.campaign.max_cost
    assert Decimal(run["settled_cost_cny"]) + Decimal(
        run["campaign_remaining_cost_cny"]
    ) == Decimal(run["campaign_cost_cap_cny"])
    assert Decimal(run["campaign_reserved_cost_cny"]) == 0
    assert Decimal(run["campaign_unknown_cost_cny"]) == 0
    assert Decimal(run["budget_stop"]["required_cost_cny"]) > Decimal(
        run["budget_stop"]["available_cost_cny"]
    )
    assert run["model_calls"] == 7
    assert run["tool_calls"] == 6
    assert run["source_workspace_unchanged"] is True

    assert quality["correct_location_reached"] is True
    assert quality["correct_location_evidence"]["path"] == "sanic/log.py"
    assert quality["correct_location_evidence"]["rank"] == 1
    assert quality["valid_edit_produced"] is False
    assert quality["protected_validation_passed"] is None
    assert quality["acceptance_evidence_unknown"] is True
    assert quality["write_tool_calls"] == 0
    assert quality["protected_check_calls"] == 0
    assert [item["name"] for item in quality["tool_sequence"]].count("read_file") == 4
    assert [item["name"] for item in quality["tool_sequence"]].count("retrieve_code") == 2

    assert trace["terminal_pack_verified"] is True
    assert trace["open_effects_present"] is False
    assert trace["unknown_effects_present"] is False
    assert trace["open_reservations_present"] is False
    assert len(trace["trace_sha256"]) == 64
    assert len(trace["projection_hash"]) == 64


def test_sanic5_evidence_transition_result_freezes_the_failed_single_run():
    result = _yaml(EVIDENCE_TRANSITION_RESULT_PATH)
    provider = load_provider_config(EVIDENCE_TRANSITION_PROVIDER_PATH)
    smoke = RealModelSmokeSuite.model_validate(_yaml(SMOKE_PATH))
    run = result["run"]
    quality = result["observed_quality"]
    trace = result["trace"]

    assert result["classification"] == "post_hoc_development_follow_up"
    assert result["scope"] == {
        "original_smoke_suite_unchanged": True,
        "included_in_original_smoke_aggregate": False,
        "blind_or_holdout_evidence": False,
        "retry_performed": False,
        "fallback_performed": False,
    }
    assert result["task_id"] not in {task.task_id for task in smoke.tasks}
    assert run["run_id"] == "run_b9236f3a191440e3ad63ced0db826564"
    assert run["status"] == "FAILED"
    assert run["task_succeeded"] is False
    assert run["failure_reason"] == "model_iteration_limit"
    assert run["provider_policy_id"] == provider.policy_id
    assert run["campaign_id"] == provider.campaign.campaign_id
    assert Decimal(run["run_cost_cap_cny"]) == provider.run_budget.max_cost
    assert (
        Decimal(run["settled_cost_cny"]) + Decimal(run["campaign_remaining_cost_cny"])
        == provider.campaign.max_cost
    )
    assert Decimal(run["campaign_reserved_cost_cny"]) == 0
    assert Decimal(run["campaign_unknown_cost_cny"]) == 0
    assert run["model_calls"] == 9
    assert run["planning_calls"] == 1
    assert run["execution_calls"] == 8
    assert run["tool_calls"] == 9
    assert run["source_workspace_unchanged"] is True

    assert quality["correct_location_reached"] is True
    assert quality["correct_location_evidence"]["path"] == "sanic/log.py"
    assert quality["correct_location_evidence"]["retrieval_rank"] == 1
    assert quality["correct_location_evidence"]["subsequent_read_target_line_visible"] is True
    assert quality["valid_edit_produced"] is False
    assert quality["protected_validation_passed"] is None
    assert quality["acceptance_evidence_unknown"] is True
    assert quality["evidence_transition_feedback_count"] == 3
    assert quality["write_tool_calls"] == 0
    assert quality["protected_check_calls"] == 0
    assert [item["name"] for item in quality["tool_sequence"]].count("read_file") == 7
    assert [item["name"] for item in quality["tool_sequence"]].count("retrieve_code") == 2

    assert trace["event_count"] == 128
    assert trace["terminal_pack_verified"] is True
    assert trace["open_effects_present"] is False
    assert trace["unknown_effects_present"] is False
    assert trace["open_reservations_present"] is False
    assert len(trace["trace_sha256"]) == 64
    assert len(trace["projection_hash"]) == 64
