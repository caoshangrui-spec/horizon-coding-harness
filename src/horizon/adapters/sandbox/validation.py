from __future__ import annotations

import shlex
from pathlib import Path

from horizon.adapters.sandbox.docker import CommandRequest, DockerSandbox
from horizon.domain.task import AcceptanceCheck
from horizon.domain.tools import AcceptanceResult


class DockerAcceptanceExecutor:
    def __init__(self, sandbox: DockerSandbox):
        self.sandbox = sandbox

    def _execute(
        self,
        workspace: Path,
        check: AcceptanceCheck,
        *,
        attempt_id: str | None = None,
    ) -> AcceptanceResult:
        argv = tuple(shlex.split(check.command, posix=True))
        if not argv:
            raise ValueError("Acceptance command cannot be empty")
        result = self.sandbox.execute(
            workspace,
            CommandRequest(
                argv=argv,
                timeout_seconds=check.timeout_seconds,
            ),
            attempt_id=attempt_id,
        )
        return AcceptanceResult(
            check_id=check.id,
            passed=result.exit_code == 0 and not result.timed_out,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            output=result.output,
            output_hash=result.output_sha256,
            output_truncated=result.output_truncated,
        )

    def execute(self, workspace: Path, check: AcceptanceCheck) -> AcceptanceResult:
        return self._execute(workspace, check)

    def execute_attempt(
        self,
        workspace: Path,
        check: AcceptanceCheck,
        attempt_id: str,
    ) -> AcceptanceResult:
        return self._execute(workspace, check, attempt_id=attempt_id)
