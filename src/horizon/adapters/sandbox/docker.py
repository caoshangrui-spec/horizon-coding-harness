from __future__ import annotations

import hashlib
import json
import os
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
    state: Literal["missing", "created", "running", "stopped"]
    image_id: str


class DockerSandbox:
    """Low-level executor for disposable, labeled validation containers."""

    _RECOVERY_SCHEMA = "v1"
    _RECOVERY_LOG_DRIVER = "json-file"
    _RECOVERY_LOG_MAX_SIZE = "8m"
    _RECOVERY_LOG_MAX_FILE = "2"
    _MAX_EXACT_RECOVERY_OUTPUT_BYTES = 65536

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

    def _logs(self, name: str, timeout: int = 15) -> bytes:
        try:
            result = subprocess.run(
                [self.docker, "logs", name],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxError("Stopped sandbox logs are unavailable") from exc
        if result.returncode != 0:
            raise SandboxError("Stopped sandbox logs could not be read")
        return result.stdout

    def _launch_attached_start(self, name: str) -> subprocess.Popen[bytes]:
        """Start one previously created container and attach to its combined output."""

        try:
            return subprocess.Popen(
                [self.docker, "start", "--attach", name],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            raise SandboxError("Docker start process could not launch") from exc

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

    @staticmethod
    def _request_digest(workspace: Path, request: CommandRequest) -> str:
        payload = {
            "argv": request.argv,
            "output_limit_bytes": request.output_limit_bytes,
            "timeout_seconds": request.timeout_seconds,
            "workspace": os.path.normcase(str(workspace)),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _inspect_attempt(self, attempt_id: str) -> tuple[SandboxAttemptStatus, dict | None]:
        owner, name = self._attempt_identity(attempt_id)
        result = self._command(["inspect", name])
        if result.returncode != 0:
            detail = f"{result.stdout}\n{result.stderr}".casefold()
            if "no such object" not in detail and "no such container" not in detail:
                raise SandboxError("Docker attempt inspection failed; status is unknown")
            return (
                SandboxAttemptStatus(
                    attempt_id=attempt_id,
                    container_name=name,
                    state="missing",
                    image_id=self.image_id,
                ),
                None,
            )
        try:
            documents = json.loads(result.stdout)
            document = documents[0]
            labels = document["Config"]["Labels"] or {}
            engine_state = document["State"]["Status"]
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
            or not isinstance(engine_state, str)
            or not isinstance(running, bool)
        ):
            raise SandboxError("Docker attempt identity does not match the persisted operation")
        if not running and engine_state == "created":
            attempt_state = "created"
        elif running and engine_state == "running":
            attempt_state = "running"
        elif not running and engine_state in {"exited", "dead"}:
            attempt_state = "stopped"
        else:
            raise SandboxError("Docker attempt state is transitional or unsupported")
        return (
            SandboxAttemptStatus(
                attempt_id=attempt_id,
                container_name=name,
                state=attempt_state,
                image_id=self.image_id,
            ),
            document,
        )

    def attempt_status(self, attempt_id: str) -> SandboxAttemptStatus:
        status, _ = self._inspect_attempt(attempt_id)
        return status

    def recover_stopped_attempt(
        self,
        workspace: Path,
        request: CommandRequest,
        attempt_id: str,
    ) -> CommandResult:
        """Recover an exact, naturally completed result without removing its container."""

        reject_link(workspace)
        workspace = workspace.resolve(strict=True)
        if workspace.parent != self.staging_root or not workspace.is_dir():
            raise PolicyDenied("Only a direct disposable child of staging_root may be recovered")
        if request.output_limit_bytes > self._MAX_EXACT_RECOVERY_OUTPUT_BYTES:
            raise SandboxError(
                "Requested output limit exceeds exact stopped-attempt recovery scope"
            )

        status, document = self._inspect_attempt(attempt_id)
        if status.state != "stopped" or document is None:
            raise SandboxError("Only a verified stopped sandbox attempt has a recoverable result")
        try:
            labels = document["Config"]["Labels"] or {}
            state = document["State"]
            config = document["Config"]
            host = document["HostConfig"]
            log_config = host["LogConfig"]
            mounts = document["Mounts"]
            container_id = document["Id"]
            exit_code = state["ExitCode"]
        except (KeyError, StopIteration, TypeError) as exc:
            raise SandboxError("Stopped sandbox result metadata is incomplete") from exc

        declared_volumes = config.get("Volumes") or {}
        configured_mounts = host.get("Mounts")
        configured_binds = host.get("Binds")
        if (
            not isinstance(mounts, list)
            or not all(isinstance(item, dict) for item in mounts)
            or not all(
                isinstance(item.get("Destination"), str) and item["Destination"] for item in mounts
            )
            or not isinstance(declared_volumes, dict)
            or not all(isinstance(path, str) and path for path in declared_volumes)
            or not isinstance(configured_mounts, list)
            or len(configured_mounts) != 1
            or not isinstance(configured_mounts[0], dict)
        ):
            raise SandboxError("Stopped sandbox result metadata is incomplete")
        workspace_mounts = [item for item in mounts if item.get("Destination") == "/workspace"]
        image_volume_mounts = [item for item in mounts if item.get("Destination") != "/workspace"]
        configured_workspace_mount = configured_mounts[0]
        exact_mount_contract = (
            configured_binds in (None, [])
            and configured_workspace_mount.get("Type") == "bind"
            and configured_workspace_mount.get("Source") == str(workspace)
            and configured_workspace_mount.get("Target") == "/workspace"
            and configured_workspace_mount.get("ReadOnly") in {None, False}
            and len(workspace_mounts) == 1
            and workspace_mounts[0].get("Type") == "bind"
            and workspace_mounts[0].get("Source") == str(workspace)
            and workspace_mounts[0].get("RW") is True
            and {item.get("Destination") for item in image_volume_mounts} == set(declared_volumes)
            and all(
                item.get("Type") == "volume" and item.get("RW") is True
                for item in image_volume_mounts
            )
        )

        expected_request = self._request_digest(workspace, request)
        exact_log_config = {
            "max-file": self._RECOVERY_LOG_MAX_FILE,
            "max-size": self._RECOVERY_LOG_MAX_SIZE,
        }
        security_options = set(host.get("SecurityOpt") or ())
        if (
            labels.get("horizon.recovery") != self._RECOVERY_SCHEMA
            or labels.get("horizon.request") != expected_request
            or state.get("Status") != "exited"
            or state.get("OOMKilled") is not False
            or state.get("Error") not in {"", None}
            or not isinstance(exit_code, int)
            or isinstance(exit_code, bool)
            or not 0 <= exit_code < 128
            or config.get("Entrypoint") != [request.argv[0]]
            or config.get("Cmd") != list(request.argv[1:])
            or config.get("User") != "65534:65534"
            or config.get("WorkingDir") != "/workspace"
            or host.get("NetworkMode") != "none"
            or host.get("ReadonlyRootfs") is not True
            or host.get("Privileged") is not False
            or {str(item).upper() for item in (host.get("CapDrop") or ())} != {"ALL"}
            or not security_options.intersection({"no-new-privileges", "no-new-privileges:true"})
            or host.get("PidsLimit") != 64
            or host.get("Memory") != 128 * 1024 * 1024
            or host.get("NanoCpus") != 1_000_000_000
            or host.get("Init") is not True
            or log_config.get("Type") != self._RECOVERY_LOG_DRIVER
            or log_config.get("Config") != exact_log_config
            or not exact_mount_contract
            or not isinstance(container_id, str)
            or not container_id
        ):
            raise SandboxError("Stopped sandbox result is not an exact recoverable attempt")

        logs = self._logs(status.container_name)
        if len(logs) > request.output_limit_bytes:
            raise SandboxError(
                "Stopped sandbox output exceeds the exact recovery limit; discard is required"
            )

        final_status, final_document = self._inspect_attempt(attempt_id)
        if (
            final_status.state != "stopped"
            or final_document is None
            or final_document.get("Id") != container_id
            or final_document.get("State", {}).get("ExitCode") != exit_code
        ):
            raise SandboxError("Stopped sandbox changed while its result was being recovered")
        return CommandResult(
            exit_code=exit_code,
            timed_out=False,
            output=logs.decode("utf-8", errors="replace"),
            total_output_bytes=len(logs),
            output_sha256=hashlib.sha256(logs).hexdigest(),
            output_truncated=False,
            image_id=self.image_id,
            container_name=status.container_name,
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

    def remove_attempt(
        self,
        attempt_id: str,
        *,
        expected_state: Literal["created", "stopped"] | None = None,
    ) -> SandboxAttemptStatus:
        status = self.attempt_status(attempt_id)
        if expected_state is not None and status.state != expected_state:
            raise SandboxError("Sandbox attempt state changed before removal")
        if status.state not in {"created", "stopped"}:
            raise SandboxError("Only a verified inactive sandbox attempt may be removed")
        removed = self._command(["rm", "--volumes", status.container_name])
        if removed.returncode != 0:
            raise SandboxError("Inactive sandbox attempt could not be removed")
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
                "--label",
                f"horizon.recovery={self._RECOVERY_SCHEMA}",
                "--label",
                f"horizon.request={self._request_digest(workspace, request)}",
            ]
        create_args = [
            "create",
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
            self._RECOVERY_LOG_DRIVER,
            "--log-opt",
            f"max-size={self._RECOVERY_LOG_MAX_SIZE}",
            "--log-opt",
            f"max-file={self._RECOVERY_LOG_MAX_FILE}",
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
        process: subprocess.Popen[bytes] | None = None
        reader: threading.Thread | None = None
        retained = bytearray()
        total = 0
        output_hash = hashlib.sha256()
        read_errors: list[Exception] = []

        def drain():
            nonlocal total
            assert process is not None
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

        timed_out = False
        try:
            created = self._command(create_args)
            if created.returncode != 0:
                raise SandboxError("Docker container could not be created")
            if attempt_id is not None:
                if self.attempt_status(attempt_id).state != "created":
                    raise SandboxError("New sandbox attempt did not remain in created state")
            elif not self._owned(name, owner):
                raise SandboxError("New sandbox container identity is unconfirmed")

            # Keeping create and start as two explicit operations makes the never-started
            # recovery state observable after a controller crash. A durable attempt remains
            # labeled in Docker until its tool receipt is settled or explicitly reconciled.
            process = self._launch_attached_start(name)
            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
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
            completed = self._command(
                ["inspect", name, "--format", "{{.State.Status}}|{{.State.Running}}"]
            )
            if completed.returncode != 0 or completed.stdout.strip() != "exited|false":
                raise SandboxError(
                    "Docker start did not produce a completed container; result is incomplete"
                )
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
                # Durable attempts remain stopped until their tool receipt is committed. This
                # leaves the container metadata and bounded logs available if the controller
                # exits after command completion but before settlement.
                if self._owned(name, owner):
                    state = self._command(["inspect", name, "--format", "{{.State.Running}}"])
                    if state.returncode != 0:
                        raise SandboxError("Container state is unknown; reconciliation required")
                    if state.stdout.strip() == "true":
                        self._stop_owned(name, owner)
                    if attempt_id is None:
                        removed = self._command(["rm", "--volumes", name])
                        if removed.returncode != 0:
                            raise SandboxError(f"Owned test container could not be removed: {name}")
            finally:
                if process is not None and process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                if reader is not None:
                    reader.join(timeout=5)
