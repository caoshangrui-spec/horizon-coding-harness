import subprocess
import sys
import textwrap

from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.domain.states import RunStatus


def test_process_exit_after_insert_before_commit_rolls_back(store, task):
    run = store.create(task, "create")
    script = textwrap.dedent("""
        import os, sys
        from datetime import datetime
        from horizon.adapters.persistence.sqlite import SQLiteEventStore
        from horizon.domain.events import NewEvent
        class CrashStore(SQLiteEventStore):
            def _append(self, *args, **kwargs):
                result = super()._append(*args, **kwargs)
                os._exit(17)
        store = CrashStore(sys.argv[1], clock=lambda: datetime.fromisoformat(sys.argv[3]))
        store.command(sys.argv[2], "crash", {}, lambda run: [
            NewEvent(event_type="STATE_CHANGED", payload={"from":"CREATED", "to":"PLANNING"})
        ])
    """)
    result = subprocess.run(
        [sys.executable, "-c", script, str(store.path), run.run_id, store.clock().isoformat()],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 17, result.stderr.decode(errors="replace")
    reopened = SQLiteEventStore(store.path, clock=store.clock)
    state = reopened.get(run.run_id)
    assert state.status == RunStatus.CREATED
    assert state.seq == 1


def test_process_exit_after_commit_preserves_event_and_idempotency(store, task):
    run = store.create(task, "create")
    script = textwrap.dedent("""
        import os, sys
        from datetime import datetime
        from horizon.adapters.persistence.sqlite import SQLiteEventStore
        from horizon.domain.events import NewEvent
        store = SQLiteEventStore(sys.argv[1], clock=lambda: datetime.fromisoformat(sys.argv[3]))
        store.command(sys.argv[2], "commit", {}, lambda run: [
            NewEvent(event_type="STATE_CHANGED", payload={"from":"CREATED", "to":"PLANNING"})
        ])
        os._exit(18)
    """)
    result = subprocess.run(
        [sys.executable, "-c", script, str(store.path), run.run_id, store.clock().isoformat()],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 18, result.stderr.decode(errors="replace")
    reopened = SQLiteEventStore(store.path)
    state = reopened.get(run.run_id)
    assert state.status == RunStatus.PLANNING
    assert state.seq == 2
    assert reopened.command(run.run_id, "commit", {}, lambda _: []).seq == 2
