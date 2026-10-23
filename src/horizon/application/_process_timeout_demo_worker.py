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
    if len(values) != 7:
        raise SystemExit(
            "usage: _process_timeout_demo_worker STORE RUN LEASE WORKER EPOCH EVENT_SEQ READY"
        )
    store_path, run_id, lease_id, worker_id, raw_epoch, raw_seq, raw_ready_path = values
    epoch = int(raw_epoch)
    launch_event_seq = int(raw_seq)
    if epoch < 1 or launch_event_seq < 1:
        raise ValueError("Worker launch identity must contain positive epoch and event sequence")

    store = SQLiteEventStore(Path(store_path), read_only=True)
    run = store.get(run_id)
    if (
        run.status != RunStatus.RUNNING
        or run.plan is None
        or run.lease_id != lease_id
        or run.worker_id != worker_id
        or run.lease_epoch != epoch
        or run.seq != launch_event_seq
        or run.reservations
        or run.cancel_requested
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
