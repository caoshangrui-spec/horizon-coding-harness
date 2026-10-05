"""Exit after a promotion effect is durable but before its receipt is published."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.adapters.workspace.promotion import WorkspacePromoter
from horizon.adapters.workspace.snapshot import SnapshotManager
from horizon.domain.common import canonical_json
from horizon.domain.recovery_evaluation import PROMOTION_CRASH_EXIT_CODE


def main() -> None:
    store = SQLiteEventStore(Path(sys.argv[1]))
    run = store.get(sys.argv[2])
    if run.promotion_intent is None:
        raise ValueError("Promotion crash worker requires a durable promotion intent")
    source = Path(sys.argv[4])
    promoter = WorkspacePromoter(SnapshotManager(ArtifactStore(Path(sys.argv[3]))))
    revision, manifest, recovered = promoter.apply_or_recover(
        run.promotion_intent.plan,
        source,
        Path(sys.argv[5]),
        run.task.constraints,
    )
    targets = []
    for change in run.promotion_intent.plan.changes:
        target = source.joinpath(*change.path.split("/"))
        stat = target.stat(follow_symlinks=False)
        content = target.read_bytes()
        targets.append(
            {
                "path": change.path,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
                "device": stat.st_dev,
                "inode": stat.st_ino,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    marker = {
        "promotion_id": run.promotion_intent.promotion_id,
        "plan_hash": run.promotion_intent.plan.sha256,
        "source_revision_after": revision,
        "source_manifest_ref_after": manifest,
        "already_applied_before_worker": recovered,
        "targets": targets,
    }
    with Path(sys.argv[6]).open("x", encoding="utf-8") as stream:
        stream.write(canonical_json(marker))
        stream.flush()
        os.fsync(stream.fileno())
    os._exit(PROMOTION_CRASH_EXIT_CODE)


if __name__ == "__main__":
    main()
