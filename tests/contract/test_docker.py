import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from horizon.adapters.sandbox.docker import CommandRequest, DockerSandbox
from horizon.application.docker_created_recovery import (
    DockerCreatedRecoveryRunner,
    verify_docker_created_recovery_pack,
)
from horizon.domain.common import canonical_json
from horizon.domain.errors import PolicyDenied

pytestmark = pytest.mark.docker


@pytest.fixture
def sandbox(tmp_path):
    image = os.environ.get("HORIZON_TEST_DOCKER_IMAGE")
    if not image:
        pytest.skip("Set HORIZON_TEST_DOCKER_IMAGE to an existing shell-capable Linux image")
    root = tmp_path / "staging"
    root.mkdir()
    return DockerSandbox(root, image)


def stage(sandbox):
    root = sandbox.staging_root / "candidate"
    root.mkdir()
    root.chmod(0o777)
    return root


def test_real_container_restrictions_and_disposable_write(sandbox):
    workspace = stage(sandbox)
    command = (
        'test "$(id -u)" = 65534 && '
        "test ! -e /var/run/docker.sock && "
        "test ! -e /controller && "
        "! touch /root-write 2>/dev/null && "
        'printf "isolated change" > /workspace/result.txt && '
        'printf "horizon-ok"'
    )
    result = sandbox.execute(
        workspace,
        CommandRequest(argv=("/bin/sh", "-c", command)),
        attempt_id="tool_contract_check_1",
    )
    assert result.exit_code == 0, result.output
    assert result.output == "horizon-ok"
    assert (workspace / "result.txt").read_text() == "isolated change"
    assert result.image_id.startswith("sha256:")
    assert result.container_name.startswith("horizon-check-")
    assert sandbox.attempt_status("tool_contract_check_1").state == "stopped"
    assert sandbox.remove_attempt("tool_contract_check_1").state == "missing"
    assert sandbox.attempt_status("tool_contract_check_1").state == "missing"
    absent = subprocess.run(["docker", "inspect", result.container_name], capture_output=True)
    assert absent.returncode != 0

    attempt_id = "tool_contract_orphan_1"
    _, name = sandbox._attempt_identity(attempt_id)
    helper = Path(__file__).parents[1] / "fault_injection" / "_docker_check_worker.py"
    worker = subprocess.Popen(
        [
            sys.executable,
            str(helper),
            str(sandbox.staging_root),
            str(workspace),
            os.environ["HORIZON_TEST_DOCKER_IMAGE"],
            attempt_id,
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if sandbox.attempt_status(attempt_id).state == "running":
                break
            if worker.poll() is not None:
                stdout, stderr = worker.communicate()
                raise AssertionError(f"check worker exited early: {stdout}\n{stderr}")
            time.sleep(0.1)
        else:
            raise AssertionError("labeled check container did not start")

        worker.kill()
        worker.wait(timeout=10)
        assert worker.returncode != 0
        assert sandbox.attempt_status(attempt_id).state == "running"
        assert sandbox.stop_attempt(attempt_id).state == "stopped"
        assert sandbox.remove_attempt(attempt_id, expected_state="stopped").state == "missing"
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=10)
        subprocess.run(
            ["docker", "rm", "--force", "--volumes", name],
            capture_output=True,
            check=False,
        )


def test_created_attempt_survives_executor_crash_and_is_removed_without_starting(sandbox):
    workspace = stage(sandbox)
    attempt_id = "tool_contract_created_only"
    _, name = sandbox._attempt_identity(attempt_id)
    marker = workspace / "never-started.txt"
    helper = Path(__file__).parents[1] / "fault_injection" / "_docker_check_worker.py"
    try:
        worker = subprocess.run(
            [
                sys.executable,
                str(helper),
                str(sandbox.staging_root),
                str(workspace),
                os.environ["HORIZON_TEST_DOCKER_IMAGE"],
                attempt_id,
                "created-before-start",
            ],
            capture_output=True,
            timeout=30,
        )
        assert worker.returncode == 34, worker.stderr.decode(errors="replace")
        assert sandbox.attempt_status(attempt_id).state == "created"
        assert not marker.exists()

        assert sandbox.remove_attempt(attempt_id, expected_state="created").state == "missing"
        assert not marker.exists()
    finally:
        subprocess.run(
            ["docker", "rm", "--force", "--volumes", name],
            capture_output=True,
            check=False,
        )


def test_created_attempt_recovery_exports_offline_verifiable_evidence(tmp_path, sandbox):
    image = os.environ["HORIZON_TEST_DOCKER_IMAGE"]
    result = DockerCreatedRecoveryRunner().run(
        tmp_path / "docker-created-recovery",
        image,
    )

    evidence = result.evidence_pack.evidence
    assert evidence.worker_exit_code == 34
    assert evidence.state_before_recovery == "created"
    assert evidence.state_after_recovery == "missing"
    assert evidence.recovery_disposition == "discard_check"
    assert evidence.tool_status == "cancelled"
    assert evidence.workspace_revision_before == evidence.workspace_revision_after
    assert evidence.trace_replay_verified is True
    assert evidence.safe_to_resume is True
    assert evidence.paid_model_called is False
    assert evidence.network_called is False
    assert verify_docker_created_recovery_pack(result.evidence_pack_path) == result.evidence_pack

    # Rehashing a forged recovery narrative must not bypass semantic verification.
    artifact_path = result.evidence_dir / "recovery-artifact.json"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["effect"] = "forged generic success"
    forged = canonical_json(artifact).encode("utf-8")
    artifact_path.write_bytes(forged)
    pack = json.loads(result.evidence_pack_path.read_text(encoding="utf-8"))
    artifact_record = next(item for item in pack["files"] if item["role"] == "recovery_artifact")
    artifact_record["bytes"] = len(forged)
    artifact_record["sha256"] = hashlib.sha256(forged).hexdigest()
    result.evidence_pack_path.write_text(
        canonical_json(pack) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(ValueError, match="artifacts do not match the Trace receipt"):
        verify_docker_created_recovery_pack(result.evidence_pack_path)


def test_missing_attempt_cannot_prove_non_execution_after_external_removal(sandbox):
    workspace = stage(sandbox)
    helper = Path(__file__).parents[1] / "fault_injection" / "_docker_check_worker.py"
    cases = (
        ("before-create", "tool_contract_before_create", 31, False),
        ("after-external-removal", "tool_contract_external_removal", 32, True),
    )

    for mode, attempt_id, exit_code, marker_expected in cases:
        _, name = sandbox._attempt_identity(attempt_id)
        marker = workspace / "after-external-removal.txt"
        marker.unlink(missing_ok=True)
        try:
            worker = subprocess.run(
                [
                    sys.executable,
                    str(helper),
                    str(sandbox.staging_root),
                    str(workspace),
                    os.environ["HORIZON_TEST_DOCKER_IMAGE"],
                    attempt_id,
                    mode,
                ],
                capture_output=True,
                timeout=30,
            )
            assert worker.returncode == exit_code, worker.stderr.decode(errors="replace")
            assert marker.exists() is marker_expected
            # External or legacy cleanup can erase the attempt even after execution. Missing
            # therefore remains insufficient proof that the check never ran.
            assert sandbox.attempt_status(attempt_id).state == "missing"
        finally:
            subprocess.run(
                ["docker", "rm", "--force", "--volumes", name],
                capture_output=True,
                check=False,
            )


def test_stopped_attempt_exact_result_is_recoverable_before_cleanup(sandbox):
    workspace = stage(sandbox)
    attempt_id = "tool_contract_stopped_result"
    _, name = sandbox._attempt_identity(attempt_id)
    helper = Path(__file__).parents[1] / "fault_injection" / "_docker_check_worker.py"
    request = CommandRequest(
        argv=("/bin/sh", "-c", "printf 'recoverable failure\\n'; exit 1"),
        timeout_seconds=120,
    )
    try:
        worker = subprocess.run(
            [
                sys.executable,
                str(helper),
                str(sandbox.staging_root),
                str(workspace),
                os.environ["HORIZON_TEST_DOCKER_IMAGE"],
                attempt_id,
                "stopped-before-cleanup",
            ],
            capture_output=True,
            timeout=30,
        )
        assert worker.returncode == 33, worker.stderr.decode(errors="replace")
        assert sandbox.attempt_status(attempt_id).state == "stopped"
        recovered = sandbox.recover_stopped_attempt(workspace, request, attempt_id)
        assert recovered.exit_code == 1
        assert recovered.timed_out is False
        assert recovered.output == "recoverable failure\n"
        assert recovered.output_truncated is False
        assert recovered.output_sha256 == hashlib.sha256(b"recoverable failure\n").hexdigest()
        assert sandbox.remove_attempt(attempt_id).state == "missing"
    finally:
        subprocess.run(
            ["docker", "rm", "--force", "--volumes", name],
            capture_output=True,
            check=False,
        )


def test_real_timeout_kills_container_and_child(sandbox):
    result = sandbox.execute(
        stage(sandbox),
        CommandRequest(
            argv=("/bin/sh", "-c", "sleep 30 & wait"),
            timeout_seconds=2,
        ),
    )
    assert result.timed_out
    assert result.exit_code != 0
    absent = subprocess.run(["docker", "inspect", result.container_name], capture_output=True)
    assert absent.returncode != 0


def test_real_output_is_bounded_but_fully_drained(sandbox):
    result = sandbox.execute(
        stage(sandbox),
        CommandRequest(
            argv=("/bin/sh", "-c", "head -c 100000 /dev/zero"),
            output_limit_bytes=128,
        ),
    )
    assert result.exit_code == 0
    assert result.total_output_bytes == 100000
    assert len(result.output) == 128
    assert result.output_truncated


def test_authoritative_or_external_root_is_not_mounted(sandbox, tmp_path):
    with pytest.raises(PolicyDenied):
        sandbox.execute(tmp_path, CommandRequest(argv=("/bin/true",)))
