from decimal import Decimal
from pathlib import Path

import yaml

from horizon.adapters.model.config import load_provider_config
from horizon.domain.pilot import RealModelPilotManifest
from horizon.domain.run_evaluation import RunABEvalManifest, RunABSuiteManifest

ROOT = Path(__file__).resolve().parents[2]
FULL_MANIFEST_PATH = (
    ROOT / "benchmarks" / "run_ab" / "full" / "cookiecutter-3-read-user-choice" / "manifest.yaml"
)
PILOT_PATH = FULL_MANIFEST_PATH.with_name("real-model-first-closure-candidate-v1.yaml")
PAID_PILOT_PATH = FULL_MANIFEST_PATH.with_name("real-model-first-closure-pilot-v1.yaml")
SUITE_V11_PATH = ROOT / "benchmarks" / "run_ab" / "bugsinpy-full-checkout-pilot-v11.yaml"
SUITE_V12_PATH = ROOT / "benchmarks" / "run_ab" / "bugsinpy-full-checkout-pilot-v12.yaml"
RESULT_PATH = ROOT / "benchmarks" / "real-model-first-closure-cookiecutter3-candidate-v1.yaml"
PROVIDER_PATH = ROOT / "config" / "providers" / "siliconflow-smoke-candidate-preflight-v1.yaml"
PAID_RESULT_PATH = ROOT / "benchmarks" / "real-model-first-closure-cookiecutter3-result-v1.yaml"
PAID_PROVIDER_PATH = (
    ROOT / "config" / "providers" / "siliconflow-cookiecutter3-first-closure-v1.yaml"
)
TARGET_EVIDENCE_PILOT_PATH = FULL_MANIFEST_PATH.with_name(
    "real-model-target-evidence-pilot-v2.yaml"
)
TARGET_EVIDENCE_PROVIDER_PATH = (
    ROOT / "config" / "providers" / "siliconflow-cookiecutter3-target-evidence-v2.yaml"
)
TARGET_EVIDENCE_CANDIDATE_PATH = (
    ROOT / "benchmarks" / "real-model-target-evidence-cookiecutter3-candidate-v2.yaml"
)
TARGET_EVIDENCE_RESULT_PATH = (
    ROOT / "benchmarks" / "real-model-target-evidence-cookiecutter3-result-v2.yaml"
)


def _yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_cookiecutter3_full_checkout_extends_v11_with_one_bounded_repair():
    v11 = RunABSuiteManifest.model_validate(_yaml(SUITE_V11_PATH))
    v12 = RunABSuiteManifest.model_validate(_yaml(SUITE_V12_PATH))
    manifest = RunABEvalManifest.model_validate(_yaml(FULL_MANIFEST_PATH))

    assert list(v12.cases[:-1]) == list(v11.cases)
    assert v12.sha256 == "89a124b58e42dd37a36ef21bc19278d9871e8c525658a23a734ae20ab0a04731"
    assert v12.cases[-1].case_id == "cookiecutter-3-read-user-choice-full"
    v12.cases[-1].check_manifest(manifest)
    assert manifest.sha256 == "663c8d28b1e538813fb04a58bc94885df333b0abc131263fbf94bedb109c34cb"
    assert manifest.task.constraints.allowed_paths == ("cookiecutter/**",)

    treatment = next(arm for arm in manifest.arms if arm.role == "single_replan")
    writes = [action for action in treatment.actions if action.tool == "replace_text"]
    assert len(writes) == 1
    assert writes[0].arguments["path"] == "cookiecutter/prompt.py"
    assert writes[0].arguments["new"].endswith("show_choices=False")


def test_cookiecutter3_real_model_candidate_is_ready_but_does_not_authorize_spend():
    pilot = RealModelPilotManifest.model_validate(_yaml(PILOT_PATH))
    provider = load_provider_config(PROVIDER_PATH)
    result = _yaml(RESULT_PATH)
    preflight = result["real_model_preflight"]
    scripted = result["scripted_ab"]

    assert pilot.provider_policy_id == provider.policy_id
    assert pilot.task.repository.base_commit == pilot.source.buggy_commit
    assert pilot.task.constraints.allowed_paths == ("cookiecutter/**",)
    assert pilot.task.constraints.network == "deny"
    assert pilot.max_paid_cost == provider.run_budget.max_cost == Decimal("0.120")
    assert provider.request.max_attempts == 1
    assert provider.fallback_enabled is False

    assert result["scope"]["paid_execution_authorized"] is False
    assert result["corrected_evidence"]["tracked_symlink_count"] == 0
    assert result["corrected_evidence"]["distinct_cookiecutter_1_symlink_count"] == 7
    assert scripted["all_expectations_met"] is True
    assert scripted["protected_validation_passed"] is True
    assert scripted["successful_write_count"] == 1
    assert scripted["evidence_backed_write_count"] == 1
    assert scripted["context_bound_write_count"] == 1
    assert scripted["paid_model_called"] is False
    assert preflight["ready"] is True
    assert preflight["credential_loaded"] is False
    assert preflight["paid_model_called"] is False
    assert preflight["network_called"] is False
    assert Decimal(preflight["planning_execution_reserved_cost_cny"]) <= Decimal(
        preflight["proposal_run_cap_cny"]
    )


def test_cookiecutter3_paid_result_preserves_the_failed_run_and_budget_boundary():
    pilot = RealModelPilotManifest.model_validate(_yaml(PAID_PILOT_PATH))
    provider = load_provider_config(PAID_PROVIDER_PATH)
    result = _yaml(PAID_RESULT_PATH)
    preflight = result["preflight"]
    run = result["run"]
    quality = result["observed_quality"]
    trace = result["trace"]

    assert pilot.provider_policy_id == provider.policy_id
    assert pilot.max_paid_cost == provider.run_budget.max_cost == Decimal("0.120")
    assert provider.campaign.max_cost == Decimal("0.120")
    assert provider.request.max_attempts == 1
    assert provider.fallback_enabled is False

    assert result["scope"]["included_in_original_smoke_aggregate"] is False
    assert result["scope"]["retry_performed"] is False
    assert result["scope"]["fallback_performed"] is False
    assert preflight["ready"] is True
    assert preflight["paid_model_called"] is False
    assert run["status"] == "FAILED"
    assert run["failure_reason"] == "campaign_cost_limit"
    assert run["model_calls"] == 8
    assert run["tool_calls"] == 9
    assert run["settled_cost_cny"] == "0.0659172"
    assert run["campaign_reserved_cost_cny"] == "0"
    assert run["campaign_unknown_cost_cny"] == "0"
    assert Decimal(run["budget_stop"]["required_cost_cny"]) > Decimal(
        run["budget_stop"]["available_cost_cny"]
    )
    assert quality["correct_location_reached"] is True
    assert quality["edit_intent_observed"] is False
    assert quality["valid_edit_produced"] is False
    assert quality["protected_validation_passed"] is None
    assert quality["write_tool_calls"] == 0
    assert quality["protected_check_calls"] == 0
    assert trace["event_count"] == 85
    assert trace["terminal_pack_verified"] is True
    assert trace["open_effects_present"] is False
    assert trace["unknown_effects_present"] is False


def test_cookiecutter3_post_fix_candidate_is_ready_but_still_requires_paid_authorization():
    pilot = RealModelPilotManifest.model_validate(_yaml(TARGET_EVIDENCE_PILOT_PATH))
    provider = load_provider_config(TARGET_EVIDENCE_PROVIDER_PATH)
    candidate = _yaml(TARGET_EVIDENCE_CANDIDATE_PATH)
    preflight = candidate["zero_spend_preflight"]

    assert pilot.provider_policy_id == provider.policy_id
    assert pilot.task.model_policy_id == provider.policy_id
    assert pilot.task.repository.base_commit == pilot.source.buggy_commit
    assert pilot.task.constraints.allowed_paths == ("cookiecutter/**",)
    assert pilot.task.constraints.network == "deny"
    assert pilot.max_paid_cost == provider.run_budget.max_cost == Decimal("0.120")
    assert provider.campaign.max_cost == Decimal("0.120")
    assert provider.request.max_attempts == 1
    assert provider.fallback_enabled is False

    assert candidate["scope"]["paid_execution_authorized"] is False
    assert candidate["scope"]["external_model_called"] is False
    assert candidate["change_under_test"]["historical_valid_edit"] is False
    assert preflight["ready"] is True
    assert preflight["initial_failure_observed"] is True
    assert preflight["source_unchanged"] is True
    assert preflight["solution_isolation_passed"] is True
    assert preflight["credential_loaded"] is False
    assert preflight["paid_model_called"] is False
    assert preflight["network_called"] is False
    assert Decimal(preflight["planning_execution_reserved_cost_cny"]) <= Decimal("0.120")


def test_cookiecutter3_target_evidence_result_preserves_the_negative_continuity_evidence():
    pilot = RealModelPilotManifest.model_validate(_yaml(TARGET_EVIDENCE_PILOT_PATH))
    provider = load_provider_config(TARGET_EVIDENCE_PROVIDER_PATH)
    result = _yaml(TARGET_EVIDENCE_RESULT_PATH)
    run = result["run"]
    quality = result["observed_quality"]
    continuity = result["target_evidence_continuity"]
    trace = result["trace"]

    assert pilot.provider_policy_id == provider.policy_id == run["provider_policy_id"]
    assert provider.run_budget.max_cost == provider.campaign.max_cost == Decimal("0.120")
    assert provider.request.max_attempts == 1
    assert provider.fallback_enabled is False

    assert result["scope"]["unique_paid_run_performed"] is True
    assert result["scope"]["included_in_original_smoke_aggregate"] is False
    assert result["scope"]["retry_performed"] is False
    assert result["scope"]["fallback_performed"] is False
    assert run["status"] == "FAILED"
    assert run["failure_reason"] == "campaign_cost_limit"
    assert run["model_calls"] == 7
    assert run["tool_calls"] == 9
    assert run["settled_cost_cny"] == "0.0584538"
    assert run["campaign_reserved_cost_cny"] == "0"
    assert run["campaign_unknown_cost_cny"] == "0"
    assert Decimal(run["budget_stop"]["required_cost_cny"]) > Decimal(
        run["budget_stop"]["available_cost_cny"]
    )
    assert quality["correct_location_reached_by_tools"] is True
    assert quality["exact_target_call_visible_in_tool_evidence"] is True
    assert quality["exact_target_call_visible_in_post_evidence_model_context"] is False
    assert quality["valid_edit_produced"] is False
    assert quality["protected_validation_passed"] is None
    assert quality["write_tool_calls"] == 0
    assert quality["protected_check_calls"] == 0
    assert continuity["change_under_test_preserved_marker"] is True
    assert continuity["change_under_test_preserved_exact_target"] is False
    assert len(continuity["compacted_projection_checks"]) == 5
    assert not any(
        check["exact_target_visible"] for check in continuity["compacted_projection_checks"]
    )
    assert trace["event_count"] == 80
    assert trace["terminal_pack_verified"] is True
    assert trace["open_effects_present"] is False
    assert trace["unknown_effects_present"] is False
    assert result["aggregate_after_run"]["historical_real_model_runs"] == 16
    assert result["aggregate_after_run"]["valid_edit_and_protected_validation_closures"] == 0
