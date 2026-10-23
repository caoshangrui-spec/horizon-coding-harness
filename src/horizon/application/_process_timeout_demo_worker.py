from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.domain.common import canonical_json
from horizon.domain.process_timeout_demo import ProcessTimeoutReadyObservation
from horizon.domain.states import RunStatus


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if len(values) != 8:
        raise SystemExit(
            "usage: _process_timeout_demo_worker STORE RUN LEASE WORKER EPOCH EVENT_SEQ "
            "LAUNCH_ID READY"
        )
    (
        store_path,
        run_id,
        lease_id,
        worker_id,
        raw_epoch,
        raw_seq,
        process_launch_id,
        raw_ready_path,
    ) = values
    epoch = int(raw_epoch)
    launch_event_seq = int(raw_seq)
    if epoch < 1 or launch_event_seq < 1:
        raise ValueError("Worker launch identity must contain positive epoch and event sequence")

    gate = sys.stdin.buffer.readline().decode("utf-8", errors="strict").rstrip("\r\n")
    if gate != process_launch_id:
        raise RuntimeError("Demo Worker did not receive its exact durable start authorization")

    store = SQLiteEventStore(Path(store_path), read_only=True)
    run = store.get(run_id)
    process_launch = run.process_worker_launch
    if (
        run.status != RunStatus.RUNNING
        or run.plan is None
        or run.lease_id != lease_id
        or run.worker_id != worker_id
        or run.lease_epoch != epoch
        or run.reservations
        or run.cancel_requested
        or process_launch is None
        or process_launch.get("launch_id") != process_launch_id
        or process_launch.get("lease_id") != lease_id
        or process_launch.get("worker_id") != worker_id
        or process_launch.get("epoch") != epoch
        or process_launch.get("process_id") != os.getpid()
        or process_launch.get("launch_event_seq") != launch_event_seq
        or process_launch.get("start_authorized") is not True
    ):
        raise RuntimeError("Demo Worker launch identity does not match the durable Run boundary")
    events = store.events(run_id)
    launch_event = events[launch_event_seq - 1]
    ready = ProcessTimeoutReadyObservation(
        run_id=run_id,
        lease_id=lease_id,
        worker_id=worker_id,
        epoch=epoch,
        launch_event_seq=launch_event_seq,
        launch_event_hash=launch_event.event_hash,
        process_launch_id=process_launch_id,
        process_id=os.getpid(),
    )

    ready_path = Path(raw_ready_path)
    temporary = ready_path.with_name(f".{ready_path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(canonical_json(ready) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, ready_path)

    while True:
        time.sleep(60)


if __name__ == "__main__":
    raise SystemExit(main())
