from __future__ import annotations

import os
import sys
from pathlib import Path

from horizon.adapters.sandbox.docker import CommandRequest, DockerSandbox


def main() -> None:
    staging_root = Path(sys.argv[1])
    workspace = Path(sys.argv[2])
    image = sys.argv[3]
    attempt_id = sys.argv[4]
    mode = sys.argv[5] if len(sys.argv) > 5 else "running"
    sandbox = DockerSandbox(staging_root, image)
    if mode == "before-create":
        os._exit(31)
    command = "sleep 60"
    if mode == "after-external-removal":
        command = "printf executed > /workspace/after-external-removal.txt"
    elif mode == "stopped-before-cleanup":
        command = "printf 'recoverable failure\\n'; exit 1"
    sandbox.execute(
        workspace,
        CommandRequest(
            argv=("/bin/sh", "-c", command),
            timeout_seconds=120,
        ),
        attempt_id=attempt_id,
    )
    if mode == "after-external-removal":
        sandbox.remove_attempt(attempt_id)
        os._exit(32)
    if mode == "stopped-before-cleanup":
        os._exit(33)


if __name__ == "__main__":
    main()
