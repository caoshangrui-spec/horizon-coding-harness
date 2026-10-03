from __future__ import annotations

import hashlib
import json
import re
import subprocess
import threading
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field

from horizon.adapters.persistence.artifacts import reject_link
from horizon.domain.common import Contract
from horizon.domain.errors import HorizonError, PolicyDenied


class SandboxError(HorizonError):
    pass


class CommandRequest(Contract):
    argv: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: int = Field(default=30, ge=1, le=1200)
    output_limit_bytes: int = Field(default=65536, ge=1, le=10 * 1024 * 1024)


class CommandResult(Contract):
    exit_code: int
    timed_out: bool
    output: str
    total_output_bytes: int
    output_sha256: str
    output_truncated: bool
    image_id: str
    container_name: str


class SandboxAttemptStatus(Contract):
    attempt_id: str
    container_name: str
    state: Literal["missing", "running", "stopped"]
    image_id: str


class DockerSandbox:
    """Low-level executor for disposable, labeled validation containers."""

    def __init__(self, staging_root: Path, image: str, docker: str = "docker"):
        if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9:/.@_-]*", image) is None:
            raise PolicyDenied("Invalid Docker image reference")
        for part in (staging_root.absolute(), *staging_root.absolute().parents):
            reject_link(part)
        self.staging_root = staging_root.resolve(strict=True)
        self.docker = docker
        inspected = self._command(["image", "inspect", image, "--format", "{{.Id}}"])
        if (
            inspected.returncode != 0
            or re.fullmatch(r"sha256:[a-f0-9]{64}", inspected.stdout.strip()) is None
        ):
            raise SandboxError(
                "Image unavailable locally or Docker inaccessible; no image was pulled"
            )
        self.image_id = inspected.stdout.strip()

    def _command(self, args: list[str], timeout: int = 15):
        try:
            return subprocess.run(
                [self.docker, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxError("Docker control operation failed or timed out") from exc

    def _owned(self, name: str, owner: str) -> bool:
        result = self._command(
            [
                "inspect",
                name,
                "--format",
                '{{index .Config.Labels "horizon.owner"}}',
            ]
        )
        return result.returncode == 0 and result.stdout.strip() == owner

    def _stop_owned(self, name: str, owner: str) -> None:
        if not self._owned(name, owner):
            raise SandboxError("Cannot verify container ownership; stop status is unknown")
        self._command(["kill", name])
        result = self._command(["inspect", name, "--format", "{{.State.Running}}"])
        if result.returncode != 0 or result.stdout.strip() != "false":
            raise SandboxError("Container termination is unconfirmed; do not retry the operation")

    @staticmethod
    def _attempt_identity(attempt_id: str) -> tuple[str, str]:
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", attempt_id) is None:
            raise PolicyDenied("Invalid sandbox attempt ID")
        owner = hashlib.sha256(f"horizon-check-v1:{attempt_id}".encode()).hexdigest()
        return owner, f"horizon-check-{owner[:32]}"

    def attempt_status(self, attempt_id: str) -> SandboxAttemptStatus:
        owner, name = self._attempt_identity(attempt_id)
        result = self._command(["inspect", name])
        if result.returncode != 0:
            detail = f"{result.stdout}\n{result.stderr}".casefold()
            if "no such object" not in detail and "no such container" not in detail:
                raise SandboxError("Docker attempt inspection failed; status is unknown")
            return SandboxAttemptStatus(
                attempt_id=attempt_id,
                container_name=name,
                state="missing",
                image_id=self.image_id,
            )
        try:
            documents = json.loads(result.stdout)
            document = documents[0]
            labels = document["Config"]["Labels"] or {}
            running = document["State"]["Running"]
            image_id = document["Image"]
        except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise SandboxError("Docker attempt metadata is malformed; status is unknown") from exc
        if (
            len(documents) != 1
            or labels.get("horizon.owner") != owner
            or labels.get("horizon.attempt") != attempt_id
            or labels.get("horizon.image") != self.image_id
            or image_id != self.image_id
            or not isinstance(running, bool)
        ):
            raise SandboxError("Docker attempt identity does not match the persisted operation")
        return SandboxAttemptStatus(
            attempt_id=attempt_id,
            container_name=name,
            state="running" if running else "stopped",
            image_id=self.image_id,
        )

    def stop_attempt(self, attempt_id: str) -> SandboxAttemptStatus:
        owner, name = self._attempt_identity(attempt_id)
        status = self.attempt_status(attempt_id)
        if status.state == "missing":
            raise SandboxError("Cannot stop a missing sandbox attempt")
        if status.state == "running":
            self._stop_owned(name, owner)
            status = self.attempt_status(attempt_id)
        if status.state != "stopped":
            raise SandboxError("Sandbox attempt termination is unconfirmed")
        return status

    def remove_attempt(self, attempt_id: str) -> SandboxAttemptStatus:
        status = self.attempt_status(attempt_id)
        if status.state != "stopped":
            raise SandboxError("Only a verified stopped sandbox attempt may be removed")
        removed = self._command(["rm", "--volumes", status.container_name])
        if removed.returncode != 0:
            raise SandboxError("Stopped sandbox attempt could not be removed")
        final = self.attempt_status(attempt_id)
        if final.state != "missing":
            raise SandboxError("Sandbox attempt removal is unconfirmed")
        return final

    def execute(
        self,
        workspace: Path,
        request: CommandRequest,
        *,
        attempt_id: str | None = None,
    ) -> CommandResult:
        reject_link(workspace)
        workspace = workspace.resolve(strict=True)
        if workspace.parent != self.staging_root or not workspace.is_dir():
            raise PolicyDenied("Only a direct disposable child of staging_root may be mounted")
        if any(char in str(workspace) for char in (",", "\n", "\r")):
            raise PolicyDenied("Workspace path cannot contain Docker mount option delimiters")
        if any("\x00" in argument for argument in request.argv):
            raise PolicyDenied("Command arguments cannot contain NUL")
        if attempt_id is None:
            owner = uuid4().hex
            name = f"horizon-contract-{owner}"
            attempt_labels: list[str] = []
        else:
            owner, name = self._attempt_identity(attempt_id)
            if self.attempt_status(attempt_id).state != "missing":
                raise SandboxError("Sandbox attempt already exists and requires reconciliation")
            attempt_labels = [
                "--label",
                f"horizon.attempt={attempt_id}",
                "--label",
                f"horizon.image={self.image_id}",
            ]
        args = [
            self.docker,
            "run",
            "--pull",
            "never",
            "--name",
            name,
            "--label",
            f"horizon.owner={owner}",
            *attempt_labels,
            "--network",
            "none",
            "--read-only",
            "--log-driver",
            "none",
            "--no-healthcheck",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "65534:65534",
            "--pids-limit",
            "64",
            "--memory",
            "128m",
            "--cpus",
            "1",
            "--init",
            "--workdir",
            "/workspace",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=67108864",
            "--mount",
            f"type=bind,source={workspace},target=/workspace",
            "--entrypoint",
            request.argv[0],
            self.image_id,
            *request.argv[1:],
        ]
        try:
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as exc:
            raise SandboxError("Docker process could not start") from exc
        retained = bytearray()
        total = 0
        output_hash = hashlib.sha256()
        read_errors: list[Exception] = []

        def drain():
            nonlocal total
            assert process.stdout is not None
            try:
                with process.stdout:
                    while chunk := process.stdout.read(8192):
                        total += len(chunk)
                        output_hash.update(chunk)
                        room = request.output_limit_bytes - len(retained)
                        if room > 0:
                            retained.extend(chunk[:room])
            except Exception as exc:
                read_errors.append(exc)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        timed_out = False
        try:
            try:
                process.wait(timeout=request.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._stop_owned(name, owner)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired as exc:
                    raise SandboxError("Container stopped but Docker client did not exit") from exc
            reader.join(timeout=5)
            if reader.is_alive() or read_errors:
                raise SandboxError("Output stream did not close cleanly; result is incomplete")
            if not self._owned(name, owner):
                raise SandboxError("Container was not created or its identity is unconfirmed")
            return CommandResult(
                exit_code=process.returncode,
                timed_out=timed_out,
                output=bytes(retained).decode("utf-8", errors="replace"),
                total_output_bytes=total,
                output_sha256=output_hash.hexdigest(),
                output_truncated=total > len(retained),
                image_id=self.image_id,
                container_name=name,
            )
        finally:
            try:
                # Only our uniquely labeled, confirmed stopped container may be removed.
                if self._owned(name, owner):
                    state = self._command(["inspect", name, "--format", "{{.State.Running}}"])
                    if state.returncode != 0:
                        raise SandboxError("Container state is unknown; reconciliation required")
                    if state.stdout.strip() == "true":
                        self._stop_owned(name, owner)
                    removed = self._command(["rm", "--volumes", name])
                    if removed.returncode != 0:
                        raise SandboxError(f"Owned test container could not be removed: {name}")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                reader.join(timeout=5)
