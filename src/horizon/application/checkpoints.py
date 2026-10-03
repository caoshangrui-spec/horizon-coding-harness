from __future__ import annotations

from typing import Any
from uuid import uuid4

from horizon.application.services import HarnessService, LeaseToken
from horizon.domain.errors import Conflict
from horizon.domain.events import NewEvent


def commit_checkpoint(
    service: HarnessService,
    run_id: str,
    token: LeaseToken,
    key: str,
    manifest_sha256: str,
    workspace_revision: str,
    expected_seq: int,
    verify_artifact: Any,
):
    """Called by a quiescent supervisor after artifact publication, never by model text."""
    request = {
        "operation": "checkpoint",
        "manifest_sha256": manifest_sha256,
        "workspace_revision": workspace_revision,
        "expected_seq": expected_seq,
        "token": token.model_dump(),
    }

    def decide(run):
        service.check_worker(run, token)
        if run.reservations:
            raise Conflict("A checkpoint cannot cover unconfirmed in-flight operations")
        snapshot = verify_artifact(manifest_sha256)
        if snapshot.workspace_revision != workspace_revision:
            raise Conflict("Artifact does not match the requested workspace revision")
        return [
            NewEvent(
                event_type="CHECKPOINT_COMMITTED",
                payload={
                    "checkpoint_id": f"chk_{uuid4().hex}",
                    "event_seq": run.seq,
                    "task_spec_hash": run.task.sha256,
                    "manifest_sha256": manifest_sha256,
                    "workspace_revision": workspace_revision,
                },
            )
        ]

    return service.store.command(run_id, key, request, decide, expected_seq=expected_seq)
