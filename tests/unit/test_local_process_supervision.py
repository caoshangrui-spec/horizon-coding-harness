import subprocess
import sys
from pathlib import Path

import pytest

from horizon.application.local_process_supervision import (
    LocalAgentWorkerLauncher,
    LocalProcessAgentSupervisor,
)
from horizon.application.services import LeaseToken
from horizon.application.supervision import ReapedWorkerBoundary


def test_local_worker_command_binds_exact_launch_identity_without_credentials(tmp_path):
    boundary = ReapedWorkerBoundary(
        run_id="run_process_1",
        lease=LeaseToken(lease_id="lease_process_1", worker_id="worker-process-1", epoch=7),
        launch_event_seq=42,
        agent_session_artifact_ref="sha256:session",
        next_iteration=3,
    )
    launcher = LocalAgentWorkerLauncher(
        store_path=tmp_path / "control.sqlite3",
        config_path=tmp_path / "provider.yaml",
        provider_config_hash="a" * 64,
        dotenv_path=tmp_path / ".env",
        artifacts_path=tmp_path / "artifacts",
        workspace_path=tmp_path / "staging" / "workspace",
        campaign_path=tmp_path / "campaign.sqlite3",
        staging_path=tmp_path / "staging",
        image="local-test-image:latest",
        retrieval_path=tmp_path / "retrieval.sqlite3",
        executable="python-test",
        python_path=(str(tmp_path / "src"),),
    )

    command = launcher.command(boundary, 2)

    assert command[:3] == (
        "python-test",
        "-m",
        "horizon.application._local_agent_worker",
    )
    assert command[4:12] == (
        "run_process_1",
        "lease_process_1",
        "worker-process-1",
        "7",
        "42",
        boundary.launch_id,
        "sha256:session",
        "3",
    )
    assert command[-2:] == (str((tmp_path / "retrieval.sqlite3").resolve()), "2")
    assert "credential" not in " ".join(command).lower()
    assert "api_key" not in " ".join(command).lower()


def test_local_worker_command_represents_missing_initial_session(tmp_path):
    boundary = ReapedWorkerBoundary(
        run_id="run_process_initial",
        lease=LeaseToken(lease_id="lease_initial", worker_id="worker-initial", epoch=1),
        launch_event_seq=5,
    )
    launcher = LocalAgentWorkerLauncher(
        store_path=Path(tmp_path / "control.sqlite3"),
        config_path=Path(tmp_path / "provider.yaml"),
        provider_config_hash="b" * 64,
        dotenv_path=Path(tmp_path / ".env"),
        artifacts_path=Path(tmp_path / "artifacts"),
        workspace_path=Path(tmp_path / "workspace"),
        campaign_path=Path(tmp_path / "campaign.sqlite3"),
        staging_path=Path(tmp_path / "staging"),
        image="local-test-image:latest",
        retrieval_path=Path(tmp_path / "retrieval.sqlite3"),
        executable="python-test",
    )

    command = launcher.command(boundary, 1)

    assert command[10:12] == ("-", "0")


def test_local_worker_without_parent_gate_exits_before_opening_store(tmp_path):
    store_path = tmp_path / "must-not-be-created.sqlite3"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "horizon.application._local_agent_worker",
            str(store_path),
            "run_without_gate",
            "lease_without_gate",
            "worker-without-gate",
            "1",
            "1",
            "launch_" + "a" * 32,
            "-",
            "0",
            str(tmp_path / "missing-provider.yaml"),
            "a" * 64,
            str(tmp_path / ".env"),
            str(tmp_path / "artifacts"),
            str(tmp_path / "workspace"),
            str(tmp_path / "campaign.sqlite3"),
            str(tmp_path / "staging"),
            "local-test-image:latest",
            str(tmp_path / "retrieval.sqlite3"),
            "1",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )

    assert process.returncode != 0
    assert b"start gate was not authorized" in process.stderr
    assert not store_path.exists()


@pytest.mark.parametrize("timeout", [0, -1, 86_401, True])
def test_local_process_supervisor_rejects_invalid_worker_timeout(timeout):
    with pytest.raises(ValueError, match="Worker timeout"):
        LocalProcessAgentSupervisor._validate_timeouts(timeout, 5.0)
