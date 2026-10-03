import hashlib
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from horizon.adapters.model.config import load_provider_config
from horizon.application.pilot import (
    PilotPreflightService,
    compute_harness_source_digest,
    load_pilot_preflight_report,
    verify_pilot_launch_binding,
)
from horizon.domain.errors import Conflict, PolicyDenied
from horizon.domain.model import CampaignSummary
from horizon.domain.pilot import RealModelPilotManifest
from horizon.domain.task import TaskSpec
from horizon.domain.tools import AcceptanceResult

ROOT = Path(__file__).resolve().parents[2]
PILOT_MANIFEST = (
    ROOT / "benchmarks" / "run_ab" / "full" / "youtube-dl-3-unescape-html" / "real-model-pilot.yaml"
)
PILOT_CONFIG = ROOT / "config" / "providers" / "siliconflow-pilot.yaml"
RETRY_MANIFEST = (
    ROOT
    / "benchmarks"
    / "run_ab"
    / "full"
    / "youtube-dl-3-unescape-html"
    / "real-model-pilot-retry.yaml"
)
RETRY_CONFIG = ROOT / "config" / "providers" / "siliconflow-pilot-retry.yaml"
CONTINUATION_MANIFEST = (
    ROOT
    / "benchmarks"
    / "run_ab"
    / "full"
    / "youtube-dl-3-unescape-html"
    / "real-model-pilot-retry-continuation.yaml"
)
CONTINUATION_CONFIG = ROOT / "config" / "providers" / "siliconflow-pilot-retry-continuation.yaml"
BUDGETED_CONTINUATION_MANIFEST = (
    ROOT
    / "benchmarks"
    / "run_ab"
    / "full"
    / "youtube-dl-3-unescape-html"
    / "real-model-pilot-budgeted-continuation.yaml"
)
BUDGETED_CONTINUATION_CONFIG = (
    ROOT / "config" / "providers" / "siliconflow-pilot-budgeted-continuation.yaml"
)


def load_manifest() -> RealModelPilotManifest:
    return RealModelPilotManifest.model_validate(
        yaml.safe_load(PILOT_MANIFEST.read_text(encoding="utf-8"))
    )


def test_checked_in_pilot_is_bounded_and_solution_blind():
    manifest = load_manifest()
    provider = load_provider_config(PILOT_CONFIG)

    assert manifest.task.budgets.max_model_calls == 6
    assert manifest.max_paid_cost == Decimal("0.25")
    assert manifest.task.constraints.network == "deny"
    assert manifest.task.constraints.allowed_paths == ("youtube_dl/**",)
    assert manifest.provider_policy_id == provider.policy_id
    assert provider.run_budget.max_cost == Decimal("0.25")
    assert provider.request.max_attempts == 1
    assert provider.fallback_enabled is False


def test_checked_in_retry_preserves_the_user_cumulative_cost_cap():
    manifest = RealModelPilotManifest.model_validate(
        yaml.safe_load(RETRY_MANIFEST.read_text(encoding="utf-8"))
    )
    provider = load_provider_config(RETRY_CONFIG)

    assert manifest.provider_policy_id == provider.policy_id
    assert manifest.max_paid_cost == Decimal("0.18")
    assert provider.run_budget.max_cost == Decimal("0.18")
    assert provider.campaign.campaign_id == "siliconflow-pilot-retry-2026-10"
    assert provider.campaign.max_cost == Decimal("0.18")
    assert provider.campaign.max_cost_per_call == Decimal("0.18")
    assert Decimal("0.0652398") + provider.run_budget.max_cost <= Decimal("0.25")
    assert provider.request.max_attempts == 1
    assert provider.fallback_enabled is False


def test_retry_continuation_fits_the_existing_campaign_headroom():
    manifest = RealModelPilotManifest.model_validate(
        yaml.safe_load(CONTINUATION_MANIFEST.read_text(encoding="utf-8"))
    )
    provider = load_provider_config(CONTINUATION_CONFIG)

    assert manifest.provider_policy_id == provider.policy_id
    assert manifest.max_paid_cost == Decimal("0.14")
    assert provider.run_budget.max_cost == Decimal("0.14")
    assert provider.campaign.campaign_id == "siliconflow-pilot-retry-2026-10"
    assert provider.campaign.max_cost == Decimal("0.18")
    assert Decimal("0.032979") + provider.run_budget.max_cost <= provider.campaign.max_cost
    assert Decimal("0.0652398") + provider.campaign.max_cost <= Decimal("0.25")
    assert provider.request.max_attempts == 1
    assert provider.fallback_enabled is False


def test_budgeted_continuation_fits_remaining_campaign_and_user_caps():
    manifest = RealModelPilotManifest.model_validate(
        yaml.safe_load(BUDGETED_CONTINUATION_MANIFEST.read_text(encoding="utf-8"))
    )
    provider = load_provider_config(BUDGETED_CONTINUATION_CONFIG)

    assert manifest.provider_policy_id == provider.policy_id
    assert manifest.max_paid_cost == Decimal("0.11")
    assert provider.run_budget.max_cost == Decimal("0.11")
    assert provider.request.max_context_chars == 7_000
    assert provider.request.preserve_recent_context_units == 2
    assert provider.campaign.campaign_id == "siliconflow-pilot-retry-2026-10"
    assert provider.campaign.max_cost == Decimal("0.18")
    assert Decimal("0.0614814") + provider.run_budget.max_cost <= provider.campaign.max_cost
    assert Decimal("0.0652398") + Decimal("0.0614814") + provider.run_budget.max_cost <= Decimal(
        "0.25"
    )
    assert Decimal("0.0652398") + provider.campaign.max_cost <= Decimal("0.25")
    assert provider.request.max_attempts == 1
    assert provider.fallback_enabled is False


def test_harness_source_digest_is_ordered_and_content_sensitive(tmp_path):
    root = tmp_path / "horizon"
    (root / "nested").mkdir(parents=True)
    (root / "z.py").write_text("Z = 1\n", encoding="utf-8")
    (root / "nested/a.py").write_text("A = 1\n", encoding="utf-8")

    original = compute_harness_source_digest(root)
    assert compute_harness_source_digest(root) == original

    (root / "nested/a.py").write_text("A = 2\n", encoding="utf-8")
    assert compute_harness_source_digest(root) != original


def test_pilot_manifest_rejects_private_solution_in_model_visible_task():
    raw = yaml.safe_load(PILOT_MANIFEST.read_text(encoding="utf-8"))
    raw["task"]["objective"] += f" Use fixed commit {raw['source']['fixed_commit']}."

    with pytest.raises(ValidationError, match="private solution"):
        RealModelPilotManifest.model_validate(raw)


class FailingAcceptance:
    def execute(self, workspace, check):
        assert (workspace / "module.py").read_text(encoding="utf-8") == "VALUE = 'buggy'\n"
        output = "expected fixed behavior"
        return AcceptanceResult(
            check_id=check.id,
            passed=False,
            exit_code=1,
            timed_out=False,
            output=output,
            output_hash=hashlib.sha256(output.encode()).hexdigest(),
        )


def init_source(path: Path) -> str:
    path.mkdir()
    (path / "module.py").write_text("VALUE = 'buggy'\n", encoding="utf-8")
    commands = (
        ("init",),
        ("config", "user.email", "pilot@example.invalid"),
        ("config", "user.name", "Horizon Pilot"),
        ("add", "module.py"),
        ("commit", "-m", "buggy fixture"),
    )
    for arguments in commands:
        result = subprocess.run(
            ["git", "-C", str(path), *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_preflight_persists_bound_task_and_launch_receipt(tmp_path):
    source = tmp_path / "source"
    head = init_source(source)
    provider = load_provider_config(PILOT_CONFIG)
    task = TaskSpec.model_validate(
        {
            "task_id": "real-pilot-fixture",
            "title": "Repair fixture behavior",
            "objective": "Make the documented behavior pass with a narrow source change",
            "repository": {
                "source": "local",
                "path": "/pilot-source",
                "base_commit": head,
            },
            "constraints": {
                "allowed_paths": ["module.py"],
                "network": "deny",
            },
            "acceptance": [
                {
                    "id": "behavior",
                    "command": "python -c pass",
                    "required": True,
                }
            ],
            "budgets": {
                "max_steps": 8,
                "max_model_calls": 6,
                "max_tool_calls": 8,
                "max_wall_time_seconds": 60,
                "max_cost_usd": "0.25",
            },
            "task_kind": "bugfix",
            "execution_mode": "workspace_write",
            "authority_scope": "workspace_write",
            "model_policy_id": provider.policy_id,
            "memory_scope": "real-pilot-fixture",
        }
    )
    fixed = "b" * 40
    manifest = RealModelPilotManifest.model_validate(
        {
            "pilot_id": "real-pilot-fixture",
            "source_path": "source",
            "source": {
                "benchmark": "FixtureBench",
                "project": "fixture",
                "bug_id": "1",
                "repository_url": "https://github.com/example/fixture",
                "buggy_commit": head,
                "fixed_commit": fixed,
                "fix_url": f"https://github.com/example/fixture/commit/{fixed}",
                "upstream_test": "tests/test_behavior.py::test_behavior",
                "license_spdx": "MIT",
                "license_url": f"https://github.com/example/fixture/blob/{head}/LICENSE",
                "reduction": "full_checkout",
                "reduction_note": "A complete one-file Git fixture for preflight contract tests.",
            },
            "task": task.model_dump(mode="json"),
            "provider_policy_id": provider.policy_id,
            "currency": "CNY",
            "max_paid_cost": "0.25",
            "expected_initial_failed_checks": ["behavior"],
            "solution_isolation": {
                "forbidden_task_literals": ["VALUE = 'fixed'"],
            },
        }
    )
    campaign = CampaignSummary(
        campaign_id=provider.campaign.campaign_id,
        currency="CNY",
        max_cost="3.00",
        settled_cost="0.02",
        reserved_cost="0",
        unknown_cost="0",
        occupied_cost="0.02",
        remaining_cost="2.98",
    )
    image_id = f"sha256:{'c' * 64}"

    result = PilotPreflightService(
        FailingAcceptance(),
        validation_backend_ref=image_id,
    ).evaluate(
        manifest,
        source=source,
        state_dir=tmp_path / "state",
        provider=provider,
        campaign=campaign,
    )

    assert result.report.ready is True
    assert result.report.initial_failure_matches is True
    assert result.report.credential_loaded is False
    assert result.report.paid_model_called is False
    assert result.report.network_called is False
    assert result.report.repository_code_executed is True
    assert result.report.validation_backend_ref == image_id
    assert result.report.harness_source_digest == compute_harness_source_digest()
    prepared = TaskSpec.model_validate(
        yaml.safe_load(result.prepared_task_path.read_text(encoding="utf-8"))
    )
    assert Path(prepared.repository.path) == source.resolve()
    loaded, reference = load_pilot_preflight_report(result.report_path)
    assert loaded == result.report
    assert reference == result.report_ref

    verify_pilot_launch_binding(
        loaded,
        task=prepared,
        provider=provider,
        source_git_head=head,
        source_snapshot_revision=loaded.source_snapshot_revision,
        validation_backend_ref=image_id,
    )
    changed = prepared.model_copy(update={"title": "Changed after preflight"})
    with pytest.raises(Conflict, match="TaskSpec changed"):
        verify_pilot_launch_binding(
            loaded,
            task=changed,
            provider=provider,
            source_git_head=head,
            source_snapshot_revision=loaded.source_snapshot_revision,
            validation_backend_ref=image_id,
        )
    with pytest.raises(Conflict, match="Harness source changed"):
        verify_pilot_launch_binding(
            loaded,
            task=prepared,
            provider=provider,
            source_git_head=head,
            source_snapshot_revision=loaded.source_snapshot_revision,
            validation_backend_ref=image_id,
            current_harness_source_digest="d" * 64,
        )
    legacy = loaded.model_copy(update={"harness_source_digest": None})
    with pytest.raises(PolicyDenied, match="predates Harness source binding"):
        verify_pilot_launch_binding(
            legacy,
            task=prepared,
            provider=provider,
            source_git_head=head,
            source_snapshot_revision=loaded.source_snapshot_revision,
            validation_backend_ref=image_id,
        )
