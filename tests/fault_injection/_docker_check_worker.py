from __future__ import annotations

import sys
from pathlib import Path

from horizon.adapters.sandbox.docker import CommandRequest, DockerSandbox


def main() -> None:
    staging_root = Path(sys.argv[1])
    workspace = Path(sys.argv[2])
    image = sys.argv[3]
    attempt_id = sys.argv[4]
    DockerSandbox(staging_root, image).execute(
        workspace,
        CommandRequest(
            argv=("/bin/sh", "-c", "sleep 60"),
            timeout_seconds=120,
        ),
        attempt_id=attempt_id,
    )


if __name__ == "__main__":
    main()
