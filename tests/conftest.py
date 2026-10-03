from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.application.services import HarnessService, LeaseToken
from horizon.domain.plan import Plan, WorkItem
from horizon.domain.states import RunStatus
from horizon.domain.task import TaskSpec


def pytest_configure(config):
    # Keep tests self-contained; never clean a shared user TEMP or reuse an evidence directory.
    if config.option.basetemp is None:
        root = Path(__file__).resolve().parents[1] / ".horizon" / "test-tmp"
        root.mkdir(parents=True, exist_ok=True)
        config.option.basetemp = str(root / f"run-{uuid4().hex}")


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 30, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def task_dict():
    return {
        "task_id": "parser-fix",
        "title": "Fix empty input",
        "objective": "Return [] for empty input without changing the public API",
        "repository": {"source": "local", "path": "/fixture", "base_commit": "a" * 40},
        "constraints": {"allowed_paths": ["src/**", "tests/**"]},
        "acceptance": [{"id": "unit", "command": "python -m pytest -q", "required": True}],
        "budgets": {
            "max_steps": 10,
            "max_model_calls": 10,
            "max_tool_calls": 20,
            "max_wall_time_seconds": 3600,
            "max_cost_usd": "1.00",
        },
        "execution_mode": "workspace_write",
        "authority_scope": "workspace_write",
        "task_kind": "bugfix",
    }


@pytest.fixture
def task(task_dict):
    return TaskSpec.model_validate(task_dict)


@pytest.fixture
def plan():
    return Plan(
        items=(
            WorkItem(
                work_item_id="fix",
                title="Fix",
                objective="Fix and verify",
                expected_artifacts=("patch",),
                acceptance_ids=("unit",),
            ),
        )
    )


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return SQLiteEventStore(tmp_path / "control.sqlite3", clock=clock)


@pytest.fixture
def service(store):
    return HarnessService(store)


@pytest.fixture
def running(store, service, task, plan):
    run = store.create(task, "create")
    service.set_plan(run.run_id, plan, "plan")
    leased = service.acquire_lease(run.run_id, "worker-1", "lease")
    token = LeaseToken.from_run(leased)
    run = service.transition(run.run_id, RunStatus.RUNNING, token, "start")
    return run, token
