from enum import StrEnum

from horizon.domain.errors import InvalidTransition


class RunStatus(StrEnum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    READY = "READY"
    RUNNING = "RUNNING"
    CHECKPOINTING = "CHECKPOINTING"
    COMPACTING = "COMPACTING"
    VALIDATING = "VALIDATING"
    REPAIRING = "REPAIRING"
    RECOVERING = "RECOVERING"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL = frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED})
NORMAL = {
    RunStatus.CREATED: {RunStatus.PLANNING},
    RunStatus.PLANNING: {RunStatus.READY},
    RunStatus.READY: {RunStatus.RUNNING},
    RunStatus.RUNNING: {
        RunStatus.CHECKPOINTING,
        RunStatus.COMPACTING,
        RunStatus.VALIDATING,
        RunStatus.REPAIRING,
    },
    RunStatus.CHECKPOINTING: {RunStatus.RUNNING},
    RunStatus.COMPACTING: {RunStatus.RUNNING},
    RunStatus.VALIDATING: {RunStatus.RUNNING, RunStatus.SUCCEEDED, RunStatus.REPAIRING},
    RunStatus.REPAIRING: {RunStatus.RUNNING},
}


def check_transition(
    previous: RunStatus, target: RunStatus, resume_state: RunStatus | None = None
) -> None:
    if previous in TERMINAL:
        raise InvalidTransition(f"Terminal run cannot transition: {previous} -> {target}")
    if target in {RunStatus.CANCELLED, RunStatus.FAILED}:
        return
    if target in {RunStatus.RECOVERING, RunStatus.WAITING_FOR_USER} and target != previous:
        return
    if previous in {RunStatus.RECOVERING, RunStatus.WAITING_FOR_USER}:
        if resume_state is not None and target == resume_state:
            return
    elif target in NORMAL.get(previous, set()):
        return
    raise InvalidTransition(f"Illegal run transition: {previous} -> {target}")
