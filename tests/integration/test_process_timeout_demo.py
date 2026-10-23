import hashlib
import json

import pytest
from typer.testing import CliRunner

from horizon.application.process_timeout_demo import (
    ProcessTimeoutDemoRunner,
    verify_process_timeout_evidence_pack,
)
from horizon.domain.common import canonical_json
from horizon.interfaces.cli.app import app


def test_process_timeout_demo_runs_real_child_and_exports_offline_evidence(tmp_path):
    output = tmp_path / "process-timeout-evidence"

    result = ProcessTimeoutDemoRunner().run(output, worker_timeout_seconds=0.05)

    evidence = result.evidence_pack.evidence
    assert evidence.terminal_status == "CANCELLED"
    assert evidence.interruption_reason == "worker_timeout"
    assert evidence.process_id > 0
    assert evidence.lease_epoch == 1
    assert evidence.launch_event_seq < evidence.cancellation_fence_event_seq
    assert evidence.cancellation_fence_event_seq < evidence.worker_stopped_event_seq
    assert evidence.worker_stopped_event_seq < evidence.lease_released_event_seq
    assert evidence.lease_released_event_seq < evidence.terminal_cancellation_event_seq
    assert evidence.terminal_cancellation_event_seq == evidence.event_count
    assert evidence.no_unknown_effects is True
    assert evidence.no_open_effects is True
    assert evidence.paid_model_called is False
    assert evidence.tool_called is False
    assert evidence.network_called is False
    assert evidence.docker_called is False
    assert evidence.repository_code_executed is False
    assert evidence.external_cost_cny == 0
    assert not tuple(output.rglob("*.sqlite3"))
    assert verify_process_timeout_evidence_pack(result.evidence_pack_path) == result.evidence_pack


def test_process_timeout_cli_runs_and_then_verifies_without_runtime_state(tmp_path):
    runner = CliRunner()
    output = tmp_path / "process-timeout-cli"

    completed = runner.invoke(
        app,
        [
            "demo",
            "process-timeout",
            "--output",
            str(output),
            "--worker-timeout-seconds",
            "0.05",
        ],
    )

    assert completed.exit_code == 0, completed.output
    payload = json.loads(completed.stdout)
    assert payload["verified"] is True
    assert payload["status"] == "CANCELLED"
    assert payload["interruption_reason"] == "worker_timeout"
    assert payload["worker_timeout_seconds"] == 0.05
    assert payload["paid_model_called"] is False
    assert payload["tool_called"] is False
    assert payload["network_called"] is False
    assert payload["docker_called"] is False
    assert payload["repository_code_executed"] is False
    assert payload["external_cost_cny"] == 0

    pack_path = output / "evidence-pack.json"
    verified = runner.invoke(app, ["demo", "verify-process-timeout", str(pack_path)])

    assert verified.exit_code == 0, verified.output
    verification = json.loads(verified.stdout)
    assert verification["verified"] is True
    assert verification["run_id"] == payload["run_id"]
    assert verification["status"] == "CANCELLED"
    assert verification["verification_mode"] == "offline_recursive_trace_replay"

    repeated = runner.invoke(
        app,
        ["demo", "process-timeout", "--output", str(output)],
    )
    assert repeated.exit_code == 2
    assert "FileExistsError" in repeated.stderr


def test_process_timeout_verifier_rejects_rehashed_process_identity_tampering(tmp_path):
    result = ProcessTimeoutDemoRunner().run(
        tmp_path / "tampered-process-timeout",
        worker_timeout_seconds=0.05,
    )
    observation = json.loads(result.supervision_observation_path.read_text(encoding="utf-8"))
    observation["process_id"] += 1
    content = (canonical_json(observation) + "\n").encode("utf-8")
    result.supervision_observation_path.write_bytes(content)

    pack = json.loads(result.evidence_pack_path.read_text(encoding="utf-8"))
    record = next(item for item in pack["files"] if item["role"] == "supervision_observation")
    record["bytes"] = len(content)
    record["sha256"] = hashlib.sha256(content).hexdigest()
    result.evidence_pack_path.write_text(
        canonical_json(pack) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ValueError, match="does not match the cancellation Trace"):
        verify_process_timeout_evidence_pack(result.evidence_pack_path)
