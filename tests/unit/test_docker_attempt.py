import json
import subprocess

import pytest

from horizon.adapters.sandbox.docker import DockerSandbox, SandboxError
from horizon.domain.errors import PolicyDenied


def fake_sandbox(*, attempt_id: str, running: bool = True, owner_override: str | None = None):
    sandbox = object.__new__(DockerSandbox)
    sandbox.image_id = "sha256:" + "a" * 64
    sandbox.docker = "docker"
    owner, name = sandbox._attempt_identity(attempt_id)
    state = {"exists": True, "running": running}
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
                    output = str(state["running"]).lower()
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
                    "State": {"Running": state["running"]},
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
            state["running"] = False
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

    assert state == {"exists": False, "running": False}
    assert any(command[0][0] == "kill" for command in commands)
    assert any(command[0][0] == "rm" for command in commands)


def test_labeled_attempt_rejects_mismatched_owner_and_invalid_id():
    sandbox, _, _ = fake_sandbox(
        attempt_id="tool_check_123",
        owner_override="untrusted-owner",
    )

    with pytest.raises(SandboxError, match="identity"):
        sandbox.attempt_status("tool_check_123")
    with pytest.raises(PolicyDenied, match="attempt ID"):
        sandbox.attempt_status("not/valid")
