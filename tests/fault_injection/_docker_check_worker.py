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
    if mode == "created-before-start":

        def crash_before_start(_name: str):
            os._exit(34)

        sandbox._launch_attached_start = crash_before_start
    command = "sleep 60"
    if mode == "created-before-start":
        command = "touch /workspace/never-started.txt"
    elif mode == "after-external-removal":
        command = "printf executed > /workspace/after-external-removal.txt"
    elif mode == "stopped-before-cleanup":
        command = "printf 'recoverable failure\\n'; exit 1"
    result = sandbox.execute(
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
        if result.exit_code != 1 or result.output != "recoverable failure\n":
            os._exit(35)
        os._exit(33)


if __name__ == "__main__":
    main()
