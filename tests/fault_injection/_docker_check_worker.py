from __future__ import annotations

import os
import sys
import time
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
    if mode == "after-cleanup":
        command = "printf executed > /workspace/after-cleanup.txt"
    elif mode == "stopped-before-cleanup":
        command = "printf 'recoverable failure\\n'; exit 1"
        original_owned = sandbox._owned

        def pause_after_exit(name: str, owner: str) -> bool:
            owned = original_owned(name, owner)
            if owned:
                state = sandbox._command(["inspect", name, "--format", "{{.State.Running}}"])
                if state.returncode == 0 and state.stdout.strip() == "false":
                    (workspace / "stopped-before-cleanup.txt").write_text("ready")
                    time.sleep(60)
            return owned

        sandbox._owned = pause_after_exit
    sandbox.execute(
        workspace,
        CommandRequest(
            argv=("/bin/sh", "-c", command),
            timeout_seconds=120,
        ),
        attempt_id=attempt_id,
    )
    if mode == "after-cleanup":
        os._exit(32)


if __name__ == "__main__":
    main()
