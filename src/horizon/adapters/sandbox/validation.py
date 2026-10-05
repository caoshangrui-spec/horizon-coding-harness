from __future__ import annotations

import shlex
from pathlib import Path

from horizon.adapters.sandbox.docker import CommandRequest, CommandResult, DockerSandbox
from horizon.domain.task import AcceptanceCheck
from horizon.domain.tools import AcceptanceResult


class DockerAcceptanceExecutor:
    def __init__(self, sandbox: DockerSandbox):
        self.sandbox = sandbox

    @staticmethod
    def _request(check: AcceptanceCheck) -> CommandRequest:
        argv = tuple(shlex.split(check.command, posix=True))
        if not argv:
            raise ValueError("Acceptance command cannot be empty")
        return CommandRequest(
            argv=argv,
            timeout_seconds=check.timeout_seconds,
        )

    @staticmethod
    def _result(check: AcceptanceCheck, result: CommandResult) -> AcceptanceResult:
        return AcceptanceResult(
            check_id=check.id,
            passed=result.exit_code == 0 and not result.timed_out,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            output=result.output,
            output_hash=result.output_sha256,
            output_truncated=result.output_truncated,
        )

    def _execute(
        self,
        workspace: Path,
        check: AcceptanceCheck,
        *,
        attempt_id: str | None = None,
    ) -> AcceptanceResult:
        result = self.sandbox.execute(
            workspace,
            self._request(check),
            attempt_id=attempt_id,
        )
        return self._result(check, result)

    def execute(self, workspace: Path, check: AcceptanceCheck) -> AcceptanceResult:
        return self._execute(workspace, check)

    def execute_attempt(
        self,
        workspace: Path,
        check: AcceptanceCheck,
        attempt_id: str,
    ) -> AcceptanceResult:
        return self._execute(workspace, check, attempt_id=attempt_id)

    def recover_attempt(
        self,
        workspace: Path,
        check: AcceptanceCheck,
        attempt_id: str,
    ) -> AcceptanceResult:
        result = self.sandbox.recover_stopped_attempt(
            workspace,
            self._request(check),
            attempt_id,
        )
        return self._result(check, result)
