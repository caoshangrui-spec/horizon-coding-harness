"""Internal subprocess that exits after Docker create and before Docker start."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Never

from horizon.adapters.sandbox.docker import DockerSandbox
from horizon.adapters.sandbox.validation import DockerAcceptanceExecutor
from horizon.domain.task import AcceptanceCheck

EXIT_AFTER_CREATE = 34


class _ExitBeforeStartSandbox(DockerSandbox):
    def _launch_attached_start(self, name: str) -> Never:
        os._exit(EXIT_AFTER_CREATE)


def main() -> None:
    if len(sys.argv) != 5:
        raise SystemExit("expected: staging_root workspace image attempt_id")
    staging_root = Path(sys.argv[1])
    workspace = Path(sys.argv[2])
    image = sys.argv[3]
    attempt_id = sys.argv[4]
    sandbox = _ExitBeforeStartSandbox(staging_root, image)
    DockerAcceptanceExecutor(sandbox).execute_attempt(
        workspace,
        AcceptanceCheck(
            id="never-started",
            command="/bin/sh -c 'printf executed > /workspace/never-started.txt'",
            timeout_seconds=30,
        ),
        attempt_id,
    )


if __name__ == "__main__":
    main()
