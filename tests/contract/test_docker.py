import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from horizon.adapters.sandbox.docker import CommandRequest, DockerSandbox
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


def test_created_attempt_is_identified_and_removed_without_starting(sandbox):
    workspace = stage(sandbox)
    attempt_id = "tool_contract_created_only"
    owner, name = sandbox._attempt_identity(attempt_id)
    marker = workspace / "never-started.txt"
    request = CommandRequest(
        argv=("/bin/sh", "-c", "touch /workspace/never-started.txt"),
        timeout_seconds=30,
    )
    created = subprocess.run(
        [
            "docker",
            "create",
            "--name",
            name,
            "--label",
            f"horizon.owner={owner}",
            "--label",
            f"horizon.attempt={attempt_id}",
            "--label",
            f"horizon.image={sandbox.image_id}",
            "--label",
            f"horizon.recovery={sandbox._RECOVERY_SCHEMA}",
            "--label",
            f"horizon.request={sandbox._request_digest(workspace.resolve(), request)}",
            "--network",
            "none",
            "--read-only",
            "--user",
            "65534:65534",
            "--mount",
            f"type=bind,source={workspace.resolve()},target=/workspace",
            "--entrypoint",
            request.argv[0],
            sandbox.image_id,
            *request.argv[1:],
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        assert created.returncode == 0, created.stderr
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
