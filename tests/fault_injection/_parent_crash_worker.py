"""Gate-controlled child kept alive after its local Process Supervisor is killed."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 5:
        raise SystemExit("usage: _parent_crash_worker LAUNCH_ID READY_PATH STOP_PATH STOPPED_PATH")
    launch_id, ready_arg, stop_arg, stopped_arg = sys.argv[1:]
    gate = sys.stdin.buffer.readline().decode("utf-8", errors="strict").rstrip("\r\n")
    if gate != launch_id:
        raise SystemExit("parent-crash Worker received an invalid start gate")

    ready_path = Path(ready_arg)
    stop_path = Path(stop_arg)
    stopped_path = Path(stopped_arg)
    ready_path.write_text(
        json.dumps({"process_id": os.getpid(), "launch_id": launch_id}),
        encoding="utf-8",
    )
    while not stop_path.exists():
        time.sleep(0.01)
    stopped_path.write_text(str(os.getpid()), encoding="utf-8")


if __name__ == "__main__":
    main()
