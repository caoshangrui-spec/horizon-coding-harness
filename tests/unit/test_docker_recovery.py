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
            "Volumes": None,
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
            "Binds": None,
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": str(workspace.resolve()),
                    "Target": "/workspace",
                }
            ],
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


def test_recovers_exact_result_with_only_image_declared_anonymous_volume(tmp_path):
    request = CommandRequest(argv=("sh", "tests/check.sh"), timeout_seconds=30)
    sandbox, workspace, attempt_id, document = stopped_sandbox(tmp_path, request)
    document["Config"]["Volumes"] = {"/data": {}}
    document["Mounts"].insert(
        0,
        {
            "Type": "volume",
            "Source": "/var/lib/docker/volumes/anonymous/_data",
            "Destination": "/data",
            "RW": True,
        },
    )

    result = sandbox.recover_stopped_attempt(workspace, request, attempt_id)

    assert result.exit_code == 1


@pytest.mark.parametrize(
    "unsafe_state",
    [
        "created",
        "signal",
        "oom",
        "request_mismatch",
        "isolation_mismatch",
        "extra_bind_mount",
        "large_output",
    ],
)
def test_recovery_rejects_ambiguous_or_inexact_stopped_result(tmp_path, unsafe_state):
    request = CommandRequest(argv=("sh", "tests/check.sh"), timeout_seconds=30)
    sandbox, workspace, attempt_id, document = stopped_sandbox(tmp_path, request)
    if unsafe_state == "created":
        document["State"]["Status"] = "created"
        document["State"]["ExitCode"] = 0
        sandbox._inspect_attempt = lambda _attempt_id: (
            SandboxAttemptStatus(
                attempt_id=attempt_id,
                container_name=sandbox._attempt_identity(attempt_id)[1],
                state="created",
                image_id=sandbox.image_id,
            ),
            document,
        )
    elif unsafe_state == "signal":
        document["State"]["ExitCode"] = 137
    elif unsafe_state == "oom":
        document["State"]["OOMKilled"] = True
    elif unsafe_state == "request_mismatch":
        request = CommandRequest(argv=("sh", "tests/other.sh"), timeout_seconds=30)
    elif unsafe_state == "isolation_mismatch":
        document["HostConfig"]["NetworkMode"] = "default"
    elif unsafe_state == "extra_bind_mount":
        document["HostConfig"]["Mounts"].append(
            {"Type": "bind", "Source": str(tmp_path), "Target": "/host"}
        )
        document["Mounts"].append(
            {
                "Type": "bind",
                "Source": str(tmp_path),
                "Destination": "/host",
                "RW": True,
            }
        )
    else:
        sandbox._logs = lambda _name: b"x" * (request.output_limit_bytes + 1)

    with pytest.raises(SandboxError):
        sandbox.recover_stopped_attempt(workspace, request, attempt_id)
