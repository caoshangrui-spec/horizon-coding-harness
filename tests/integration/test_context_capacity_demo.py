import hashlib
import json

import pytest
from typer.testing import CliRunner

from horizon.application.context_capacity_demo import (
    ContextCapacityDemoRunner,
    verify_context_capacity_evidence_pack,
)
from horizon.domain.common import canonical_json
from horizon.interfaces.cli.app import app


def test_context_capacity_demo_pauses_before_dispatch_and_exports_offline_evidence(tmp_path):
    output = tmp_path / "context-capacity-evidence"

    result = ContextCapacityDemoRunner().run(output)

    evidence = result.evidence_pack.evidence
    assert evidence.terminal_status == "SUCCEEDED"
    assert evidence.stop_reason == "protected_prefix_exceeds_limit"
    assert evidence.required_context_chars > evidence.configured_context_chars
    assert evidence.blocked_message_count == evidence.resumed_message_count
    assert evidence.recovery_lease_epoch == evidence.blocked_lease_epoch + 1
    assert evidence.request_event_seq < evidence.blocked_lease_release_event_seq
    assert evidence.blocked_lease_release_event_seq < evidence.recovery_lease_event_seq
    assert evidence.recovery_lease_event_seq < evidence.decision_event_seq
    assert evidence.session_resumed_event_seq < evidence.first_model_reservation_event_seq
    assert evidence.provider_dispatch_before_resume is False
    assert evidence.transcript_preserved is True
    assert evidence.required_validation_passed is True
    assert evidence.trace_replay_verified is True
    assert evidence.final_model_call_count == 2
    assert evidence.final_tool_call_count == 3
    assert evidence.no_unknown_effects is True
    assert evidence.no_open_effects is True
    assert evidence.paid_model_called is False
    assert evidence.network_called is False
    assert evidence.docker_called is False
    assert evidence.repository_code_executed is False
    assert evidence.external_cost_cny == 0
    assert not tuple(output.rglob("*.sqlite3"))
    assert verify_context_capacity_evidence_pack(result.evidence_pack_path) == (
        result.evidence_pack
    )


def test_context_capacity_cli_runs_and_verifies_without_runtime_state(tmp_path):
    runner = CliRunner()
    output = tmp_path / "context-capacity-cli"

    completed = runner.invoke(
        app,
        ["demo", "context-capacity-recovery", "--output", str(output)],
    )

    assert completed.exit_code == 0, completed.output
    payload = json.loads(completed.stdout)
    assert payload["verified"] is True
    assert payload["status"] == "SUCCEEDED"
    assert payload["stop_reason"] == "protected_prefix_exceeds_limit"
    assert payload["required_context_chars"] > payload["configured_context_chars"]
    assert payload["provider_dispatch_before_resume"] is False
    assert payload["transcript_preserved"] is True
    assert payload["required_validation_passed"] is True
    assert payload["paid_model_called"] is False
    assert payload["external_cost_cny"] == 0

    pack_path = output / "evidence-pack.json"
    verified = runner.invoke(
        app,
        ["demo", "verify-context-capacity-recovery", str(pack_path)],
    )

    assert verified.exit_code == 0, verified.output
    verification = json.loads(verified.stdout)
    assert verification["verified"] is True
    assert verification["run_id"] == payload["run_id"]
    assert verification["status"] == "SUCCEEDED"
    assert verification["provider_dispatch_before_resume"] is False
    assert verification["verification_mode"] == ("offline_recursive_trace_and_artifact_replay")

    repeated = runner.invoke(
        app,
        ["demo", "context-capacity-recovery", "--output", str(output)],
    )
    assert repeated.exit_code == 2
    assert "FileExistsError" in repeated.stderr


def test_context_capacity_verifier_rejects_rehashed_transcript_tampering(tmp_path):
    result = ContextCapacityDemoRunner().run(tmp_path / "tampered-context-capacity")
    resumed = json.loads(result.resumed_session_path.read_text(encoding="utf-8"))
    resumed["messages"][1]["content"] += " tampered"
    content = canonical_json(resumed).encode("utf-8")
    result.resumed_session_path.write_bytes(content)

    pack = json.loads(result.evidence_pack_path.read_text(encoding="utf-8"))
    record = next(item for item in pack["files"] if item["role"] == "resumed_session")
    record["bytes"] = len(content)
    record["sha256"] = hashlib.sha256(content).hexdigest()
    result.evidence_pack_path.write_text(
        canonical_json(pack) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ValueError, match="Resumed Agent session"):
        verify_context_capacity_evidence_pack(result.evidence_pack_path)
