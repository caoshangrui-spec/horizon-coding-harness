import json
import subprocess
from io import BytesIO

import pytest

from horizon.adapters.sandbox.docker import (
    CommandRequest,
    DockerSandbox,
    SandboxAttemptStatus,
    SandboxError,
)
from horizon.domain.errors import PolicyDenied


def fake_sandbox(
    *,
    attempt_id: str,
    attempt_state: str = "running",
    owner_override: str | None = None,
):
    sandbox = object.__new__(DockerSandbox)
    sandbox.image_id = "sha256:" + "a" * 64
    sandbox.docker = "docker"
    owner, name = sandbox._attempt_identity(attempt_id)
    state = {"exists": True, "status": attempt_state}
    commands = []

    def command(args, timeout=15):
        commands.append((tuple(args), timeout))
        if args[0] == "inspect":
            if not state["exists"]:
                return subprocess.CompletedProcess(
                    args,
                    1,
                    stdout="",
                    stderr=f"Error: No such object: {name}",
                )
            if "--format" in args:
                template = args[-1]
                if "horizon.owner" in template:
                    output = owner_override or owner
                else:
                    output = str(state["status"] == "running").lower()
                return subprocess.CompletedProcess(args, 0, stdout=output + "\n", stderr="")
            document = [
                {
                    "Config": {
                        "Labels": {
                            "horizon.owner": owner_override or owner,
                            "horizon.attempt": attempt_id,
                            "horizon.image": sandbox.image_id,
                        }
                    },
                    "State": {
                        "Running": state["status"] == "running",
                        "Status": state["status"],
                    },
                    "Image": sandbox.image_id,
                }
            ]
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps(document),
                stderr="",
            )
        if args[0] == "kill":
            state["status"] = "exited"
            return subprocess.CompletedProcess(args, 0, stdout=name + "\n", stderr="")
        if args[0] == "rm":
            state["exists"] = False
            return subprocess.CompletedProcess(args, 0, stdout=name + "\n", stderr="")
        raise AssertionError(f"Unexpected Docker command: {args}")

    sandbox._command = command
    return sandbox, state, commands


def test_labeled_attempt_can_be_verified_stopped_and_removed():
    sandbox, state, commands = fake_sandbox(attempt_id="tool_check_123")

    assert sandbox.attempt_status("tool_check_123").state == "running"
    assert sandbox.stop_attempt("tool_check_123").state == "stopped"
    assert sandbox.remove_attempt("tool_check_123").state == "missing"

    assert state == {"exists": False, "status": "exited"}
    assert any(command[0][0] == "kill" for command in commands)
    assert any(command[0][0] == "rm" for command in commands)


def test_labeled_created_attempt_can_be_removed_without_kill():
    sandbox, state, commands = fake_sandbox(
        attempt_id="tool_check_created",
        attempt_state="created",
    )

    assert sandbox.attempt_status("tool_check_created").state == "created"
    assert sandbox.remove_attempt("tool_check_created", expected_state="created").state == "missing"

    assert state == {"exists": False, "status": "created"}
    assert not any(command[0][0] == "kill" for command in commands)
    assert any(command[0][0] == "rm" for command in commands)


def test_attempt_removal_rejects_state_change_from_expected_created():
    sandbox, state, commands = fake_sandbox(
        attempt_id="tool_check_changed",
        attempt_state="exited",
    )

    with pytest.raises(SandboxError, match="state changed"):
        sandbox.remove_attempt("tool_check_changed", expected_state="created")

    assert state["exists"] is True
    assert not any(command[0][0] == "rm" for command in commands)


@pytest.mark.parametrize("attempt_state", ["paused", "restarting", "removing"])
def test_labeled_attempt_rejects_transitional_engine_state(attempt_state):
    sandbox, _, _ = fake_sandbox(
        attempt_id="tool_check_transitional",
        attempt_state=attempt_state,
    )

    with pytest.raises(SandboxError, match="transitional or unsupported"):
        sandbox.attempt_status("tool_check_transitional")


def test_labeled_attempt_rejects_mismatched_owner_and_invalid_id():
    sandbox, _, _ = fake_sandbox(
        attempt_id="tool_check_123",
        owner_override="untrusted-owner",
    )

    with pytest.raises(SandboxError, match="identity"):
        sandbox.attempt_status("tool_check_123")
    with pytest.raises(PolicyDenied, match="attempt ID"):
        sandbox.attempt_status("not/valid")


class _AttachedProcess:
    def __init__(self, output: bytes, returncode: int):
        self.stdout = BytesIO(output)
        self.returncode = returncode

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        raise AssertionError("A completed fake Docker client must not be killed")


def executable_sandbox(tmp_path):
    staging = tmp_path / "staging"
    workspace = staging / "candidate"
    workspace.mkdir(parents=True)
    sandbox = object.__new__(DockerSandbox)
    sandbox.staging_root = staging.resolve()
    sandbox.docker = "docker"
    sandbox.image_id = "sha256:" + "a" * 64
    state = {"exists": False, "status": "missing"}
    commands = []

    def command(args, timeout=15):
        commands.append((tuple(args), timeout))
        if args[0] == "create":
            assert state["exists"] is False
            state.update(exists=True, status="created")
            return subprocess.CompletedProcess(args, 0, stdout="container-id\n", stderr="")
        if args[0] == "inspect" and "--format" in args:
            template = args[-1]
            if ".State.Status" in template:
                output = f"{state['status']}|{str(state['status'] == 'running').lower()}"
            else:
                output = str(state["status"] == "running").lower()
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=output + "\n",
                stderr="",
            )
        if args[0] == "rm":
            state.update(exists=False, status="missing")
            return subprocess.CompletedProcess(args, 0, stdout="container-name\n", stderr="")
        raise AssertionError(f"Unexpected Docker command: {args}")

    def attempt_status(attempt_id):
        status = state["status"]
        if status == "exited":
            status = "stopped"
        return SandboxAttemptStatus(
            attempt_id=attempt_id,
            container_name=sandbox._attempt_identity(attempt_id)[1],
            state=status,
            image_id=sandbox.image_id,
        )

    sandbox._command = command
    sandbox._owned = lambda _name, _owner: state["exists"]
    sandbox.attempt_status = attempt_status
    return sandbox, workspace, state, commands


def test_execute_uses_explicit_create_then_attached_start(tmp_path):
    sandbox, workspace, state, commands = executable_sandbox(tmp_path)
    starts = []

    def start(name):
        assert state == {"exists": True, "status": "created"}
        starts.append(name)
        state["status"] = "exited"
        return _AttachedProcess(b"bounded output", 3)

    sandbox._launch_attached_start = start
    result = sandbox.execute(
        workspace,
        CommandRequest(argv=("/bin/sh", "-c", "exit 3")),
        attempt_id="tool_create_start",
    )

    assert result.exit_code == 3
    assert result.output == "bounded output"
    assert len(starts) == 1
    assert commands[0][0][0] == "create"
    assert "run" not in commands[0][0]
    assert state == {"exists": True, "status": "exited"}


@pytest.mark.parametrize(
    ("attempt_id", "expected_exists"),
    [("tool_create_start_crash", True), (None, False)],
)
def test_start_launch_failure_retains_only_durable_created_attempt(
    tmp_path,
    attempt_id,
    expected_exists,
):
    sandbox, workspace, state, commands = executable_sandbox(tmp_path)

    def fail_start(_name):
        raise SandboxError("simulated controller failure before Docker start")

    sandbox._launch_attached_start = fail_start
    with pytest.raises(SandboxError, match="before Docker start"):
        sandbox.execute(
            workspace,
            CommandRequest(argv=("/bin/true",)),
            attempt_id=attempt_id,
        )

    assert state["exists"] is expected_exists
    assert state["status"] == ("created" if expected_exists else "missing")
    assert any(command[0][0] == "create" for command in commands)
    assert any(command[0][0] == "rm" for command in commands) is (not expected_exists)


def test_start_client_error_is_not_misreported_as_a_completed_command(tmp_path):
    sandbox, workspace, state, _ = executable_sandbox(tmp_path)

    def start_without_starting(_name):
        return _AttachedProcess(b"docker start failed", 1)

    sandbox._launch_attached_start = start_without_starting
    with pytest.raises(SandboxError, match="did not produce a completed container"):
        sandbox.execute(
            workspace,
            CommandRequest(argv=("/bin/true",)),
            attempt_id="tool_start_client_error",
        )

    assert state == {"exists": True, "status": "created"}
