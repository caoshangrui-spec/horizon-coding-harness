import hashlib

import pytest

from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.adapters.workspace.snapshot import FileEntry, SnapshotManager
from horizon.application.checkpoints import commit_checkpoint
from horizon.domain.budget import Usage
from horizon.domain.errors import Conflict, IntegrityError, PolicyDenied


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "src/parser.py").write_text(
        "def parse(text):\n    return text.split()\n", encoding="utf-8"
    )
    (root / "new.txt").write_text("untracked content\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=do-not-copy\n", encoding="utf-8")
    return root


@pytest.fixture
def snapshots(tmp_path):
    return SnapshotManager(ArtifactStore(tmp_path / "artifacts"))


def test_content_addressed_snapshot_and_fresh_restore(workspace, snapshots, tmp_path):
    first, manifest = snapshots.capture(workspace)
    repeated, repeated_manifest = snapshots.capture(workspace)
    assert manifest == repeated_manifest
    assert first == repeated
    assert first.excluded_paths == (".env",)
    restored = tmp_path / "restored"
    snapshots.restore(manifest, restored)
    assert (restored / "new.txt").read_bytes() == (workspace / "new.txt").read_bytes()
    assert not (restored / ".env").exists()
    with pytest.raises(PolicyDenied, match="NEW directory"):
        snapshots.restore(manifest, restored)
    (workspace / "src/parser.py").write_text("partial uncommitted change", encoding="utf-8")
    later, _ = snapshots.capture(workspace)
    assert later.workspace_revision != first.workspace_revision


def test_corrupt_artifact_blocks_restore_and_preserves_original(workspace, snapshots, tmp_path):
    snapshot, manifest = snapshots.capture(workspace)
    entry = snapshot.files[0]
    artifact_path = snapshots.artifacts.root / entry.sha256[:2] / entry.sha256
    artifact_path.write_bytes(b"corruption")
    with pytest.raises(IntegrityError, match="digest"):
        snapshots.restore(manifest, tmp_path / "destination")
    assert not (tmp_path / "destination").exists()
    assert (workspace / "new.txt").exists()


@pytest.mark.parametrize("path", ["../escape", "C:/escape", "/escape", ".git/config", "NUL", "a."])
def test_malicious_snapshot_paths_are_rejected(path):
    with pytest.raises(ValueError):
        FileEntry(path=path, sha256="a" * 64, size_bytes=1)


def test_oversized_snapshot_fails_without_manifest_commit(workspace, snapshots):
    with pytest.raises(PolicyDenied, match="limit"):
        snapshots.capture(workspace, max_file_bytes=1)


def test_scoped_snapshot_never_reads_or_budgets_out_of_scope_files(workspace, snapshots):
    cache = workspace / ".uv-cache"
    cache.mkdir()
    (cache / "large.bin").write_bytes(b"x" * 1_024)

    snapshot, _ = snapshots.capture(
        workspace,
        allowed_paths=("src/**",),
        max_total_bytes=100,
    )

    assert tuple(entry.path for entry in snapshot.files) == ("src/parser.py",)
    assert ".uv-cache/large.bin" in snapshot.excluded_paths
    assert "new.txt" in snapshot.excluded_paths


def test_checkpoint_reference_and_event_commit_atomically(
    store,
    service,
    running,
    snapshots,
    workspace,
):
    run, token = running
    snapshot, manifest = snapshots.capture(workspace)
    result = commit_checkpoint(
        service,
        run.run_id,
        token,
        "checkpoint",
        manifest,
        snapshot.workspace_revision,
        run.seq,
        snapshots.verify,
    )
    assert result.last_checkpoint["event_seq"] == run.seq
    assert result.seq == run.seq + 1
    assert store.get(run.run_id).last_checkpoint == result.last_checkpoint
    assert result.workspace_revision == snapshot.workspace_revision


def test_checkpoint_rejects_inflight_operations(service, running, snapshots, workspace):
    run, token = running
    held = service.reserve(run.run_id, "inflight", Usage(model_calls=1), token, "reserve")
    snapshot, manifest = snapshots.capture(workspace)
    with pytest.raises(Conflict, match="in-flight"):
        commit_checkpoint(
            service,
            run.run_id,
            token,
            "checkpoint",
            manifest,
            snapshot.workspace_revision,
            held.seq,
            snapshots.verify,
        )


def test_no_artifact_reference_if_verification_failed(store, service, running):
    run, token = running

    def unavailable(_):
        raise IntegrityError("Missing artifact")

    with pytest.raises(IntegrityError):
        commit_checkpoint(
            service, run.run_id, token, "checkpoint", "a" * 64, "tree", run.seq, unavailable
        )
    assert store.get(run.run_id).last_checkpoint is None
    assert store.get(run.run_id).seq == run.seq


def test_artifact_identity_is_sha256(snapshots):
    value = b"durable bytes"
    result = snapshots.artifacts.put(value)
    assert result == hashlib.sha256(value).hexdigest()
    assert snapshots.artifacts.read(result) == value


def test_repeated_put_reuses_an_artifact_verified_by_the_same_store(tmp_path, monkeypatch):
    artifacts = ArtifactStore(tmp_path / "artifacts")
    value = b"content already verified by this process"
    artifact_ref = artifacts.put(value)

    def unexpected_read(*_args, **_kwargs):
        raise AssertionError("unchanged verified artifact should not be re-read")

    monkeypatch.setattr(artifacts, "read", unexpected_read)

    assert artifacts.put(value) == artifact_ref


def test_repeated_put_reverifies_an_artifact_when_its_metadata_changes(tmp_path):
    artifacts = ArtifactStore(tmp_path / "artifacts")
    value = b"immutable artifact"
    artifact_ref = artifacts.put(value)
    artifact_path = artifacts.root / artifact_ref[:2] / artifact_ref
    artifact_path.write_bytes(b"corrupt")

    with pytest.raises(IntegrityError, match="digest"):
        artifacts.put(value)

    with pytest.raises(IntegrityError, match="digest"):
        ArtifactStore(artifacts.root).put(value)
