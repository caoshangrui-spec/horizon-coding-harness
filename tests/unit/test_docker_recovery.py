from __future__ import annotations

from pathlib import Path

import pytest

from horizon.adapters.sandbox.docker import (
    CommandRequest,
    DockerSandbox,
    SandboxAttemptStatus,
    SandboxError,
)


def stopped_sandbox(tmp_path: Path, request: CommandRequest):
    staging = tmp_path / "staging"
    workspace = staging / "candidate"
    workspace.mkdir(parents=True)
    sandbox = object.__new__(DockerSandbox)
    sandbox.staging_root = staging.resolve()
    sandbox.docker = "docker"
    sandbox.image_id = "sha256:" + "a" * 64
    attempt_id = "tool_exact_recovery"
    _, name = sandbox._attempt_identity(attempt_id)
    status = SandboxAttemptStatus(
        attempt_id=attempt_id,
        container_name=name,
        state="stopped",
        image_id=sandbox.image_id,
    )
    document = {
        "Id": "b" * 64,
        "Image": sandbox.image_id,
        "Config": {
            "Labels": {
                "horizon.recovery": sandbox._RECOVERY_SCHEMA,
                "horizon.request": sandbox._request_digest(workspace.resolve(), request),
            },
            "Entrypoint": [request.argv[0]],
            "Cmd": list(request.argv[1:]),
            "User": "65534:65534",
            "WorkingDir": "/workspace",
        },
        "State": {
            "Running": False,
            "Status": "exited",
            "ExitCode": 1,
            "OOMKilled": False,
            "Error": "",
        },
        "HostConfig": {
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "Privileged": False,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "PidsLimit": 64,
            "Memory": 128 * 1024 * 1024,
            "NanoCpus": 1_000_000_000,
            "Init": True,
            "LogConfig": {
                "Type": sandbox._RECOVERY_LOG_DRIVER,
                "Config": {
                    "max-file": sandbox._RECOVERY_LOG_MAX_FILE,
                    "max-size": sandbox._RECOVERY_LOG_MAX_SIZE,
                },
            },
        },
        "Mounts": [
            {
                "Type": "bind",
                "Source": str(workspace.resolve()),
                "Destination": "/workspace",
                "RW": True,
            }
        ],
    }
    sandbox._inspect_attempt = lambda _attempt_id: (status, document)
    sandbox._logs = lambda _name: b"exact failure\n"
    return sandbox, workspace, attempt_id, document


def test_recovers_exact_naturally_stopped_result(tmp_path):
    request = CommandRequest(argv=("sh", "tests/check.sh"), timeout_seconds=30)
    sandbox, workspace, attempt_id, _ = stopped_sandbox(tmp_path, request)

    result = sandbox.recover_stopped_attempt(workspace, request, attempt_id)

    assert result.exit_code == 1
    assert result.timed_out is False
    assert result.output == "exact failure\n"
    assert result.total_output_bytes == len(b"exact failure\n")
    assert result.output_truncated is False


@pytest.mark.parametrize(
    "unsafe_state",
    ["signal", "oom", "request_mismatch", "isolation_mismatch", "large_output"],
)
def test_recovery_rejects_ambiguous_or_inexact_stopped_result(tmp_path, unsafe_state):
    request = CommandRequest(argv=("sh", "tests/check.sh"), timeout_seconds=30)
    sandbox, workspace, attempt_id, document = stopped_sandbox(tmp_path, request)
    if unsafe_state == "signal":
        document["State"]["ExitCode"] = 137
    elif unsafe_state == "oom":
        document["State"]["OOMKilled"] = True
    elif unsafe_state == "request_mismatch":
        request = CommandRequest(argv=("sh", "tests/other.sh"), timeout_seconds=30)
    elif unsafe_state == "isolation_mismatch":
        document["HostConfig"]["NetworkMode"] = "default"
    else:
        sandbox._logs = lambda _name: b"x" * (request.output_limit_bytes + 1)

    with pytest.raises(SandboxError):
        sandbox.recover_stopped_attempt(workspace, request, attempt_id)
