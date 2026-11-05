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
SUITE_V11_PATH = ROOT / "benchmarks" / "run_ab" / "bugsinpy-full-checkout-pilot-v11.yaml"
SUITE_V12_PATH = ROOT / "benchmarks" / "run_ab" / "bugsinpy-full-checkout-pilot-v12.yaml"
RESULT_PATH = ROOT / "benchmarks" / "real-model-first-closure-cookiecutter3-candidate-v1.yaml"
PROVIDER_PATH = ROOT / "config" / "providers" / "siliconflow-smoke-candidate-preflight-v1.yaml"


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
