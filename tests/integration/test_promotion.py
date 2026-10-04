import json
import subprocess
import sys
import textwrap
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.adapters.workspace.promotion import WorkspacePromoter
from horizon.adapters.workspace.snapshot import SnapshotManager
from horizon.application.checkpoints import commit_checkpoint
from horizon.application.promotion import PromotionService
from horizon.application.services import HarnessService, LeaseToken
from horizon.domain.errors import Conflict, PolicyDenied
from horizon.domain.plan import Plan, WorkItem
from horizon.domain.promotion import PromotionIntent
from horizon.domain.states import RunStatus
from horizon.domain.task import TaskSpec
from horizon.interfaces.cli.app import app

cli = CliRunner()


def _git(source: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(source), *args],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )


def prepare_successful_candidate(
    tmp_path: Path,
    task_dict,
    *,
    git: bool = True,
    multi_file: bool = False,
    modify_existing: bool = True,
    created_file: bool = False,
    created_content: bytes | None = None,
):
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "tests").mkdir()
    (source / "src/parser.py").write_text(
        "def parse(value):\n    return [value]\n",
        encoding="utf-8",
    )
    (source / "tests/test_parser.py").write_text(
        "# protected by controller\n",
        encoding="utf-8",
    )
    if git:
        _git(source, "init", "-q")
        _git(source, "config", "user.email", "horizon@example.test")
        _git(source, "config", "user.name", "Horizon Test")
        _git(source, "add", "src/parser.py", "tests/test_parser.py")
        _git(source, "commit", "-q", "-m", "base")

    control_root = tmp_path / ".horizon"
    artifacts = ArtifactStore(control_root / "artifacts")
    snapshots = SnapshotManager(artifacts)
    promoter = WorkspacePromoter(snapshots)
    constraints = TaskSpec.model_validate(task_dict).constraints
    origin = promoter.bind_origin(source, constraints)
    candidate = control_root / "staging" / "agent-promotion"
    snapshots.restore(origin.source_manifest_ref, candidate)
    if modify_existing:
        (candidate / "src/parser.py").write_text(
            "def parse(value):\n    return [] if value == '' else [value]\n",
            encoding="utf-8",
        )
    if multi_file:
        (candidate / "tests/test_parser.py").write_text(
            "# protected by controller\n# verifies empty input\n",
            encoding="utf-8",
        )
    if created_file:
        generated_content = (
            "def generated():\n    return '你好'\n".encode()
            if created_content is None
            else created_content
        )
        (candidate / "src/generated.py").write_bytes(generated_content)

    task_data = {**task_dict, "model_policy_id": "fake-policy"}
    task_data["repository"] = {
        "source": "local",
        "path": str(candidate),
        "base_commit": origin.source_revision,
    }
    task = TaskSpec.model_validate(task_data)
    plan = Plan(
        items=(
            WorkItem(
                work_item_id="fix",
                title="Fix parser",
                objective="Return [] for empty input",
                expected_artifacts=(
                    *(("src/parser.py",) if modify_existing else ()),
                    *(("tests/test_parser.py",) if multi_file else ()),
                    *(("src/generated.py",) if created_file else ()),
                ),
                acceptance_ids=("unit",),
                allowed_tools=(
                    "read_file",
                    "replace_text",
                    "apply_patch",
                    *(("create_file",) if created_file else ()),
                    "run_check",
                ),
            ),
        )
    )
    db = control_root / "control.sqlite3"
    store = SQLiteEventStore(db)
    service = HarnessService(store)
    run = store.create(task, f"create-{uuid4().hex}")
    run = service.bind_workspace_origin(run.run_id, origin, f"origin-{uuid4().hex}")
    run = service.set_plan(run.run_id, plan, f"plan-{uuid4().hex}")
    run = service.acquire_lease(run.run_id, "promotion-fixture", f"lease-{uuid4().hex}")
    token = LeaseToken.from_run(run)
    run = service.transition(run.run_id, RunStatus.RUNNING, token, f"start-{uuid4().hex}")
    candidate_snapshot, candidate_manifest = snapshots.capture(
        candidate,
        denied_paths=task.constraints.denied_paths,
    )
    run = commit_checkpoint(
        service,
        run.run_id,
        token,
        f"checkpoint-{uuid4().hex}",
        candidate_manifest,
        candidate_snapshot.workspace_revision,
        run.seq,
        snapshots.verify,
    )
    run = service.transition(
        run.run_id,
        RunStatus.VALIDATING,
        token,
        f"validating-{uuid4().hex}",
    )
    evidence = artifacts.put(b"promotion fixture validation passed")
    run = service.record_validation(
        run.run_id,
        ("unit",),
        evidence,
        token,
        f"validation-{uuid4().hex}",
    )
    run = service.pass_work_item(
        run.run_id,
        "fix",
        token,
        f"pass-{uuid4().hex}",
    )
    run = service.transition(
        run.run_id,
        RunStatus.SUCCEEDED,
        token,
        f"success-{uuid4().hex}",
    )
    return source, candidate, artifacts, promoter, service, run, db


def test_promotion_applies_validated_single_file_and_preserves_git_head(tmp_path, task_dict):
    source, _, artifacts, promoter, service, run, _ = prepare_successful_candidate(
        tmp_path,
        task_dict,
    )
    promotion = PromotionService(service, promoter)
    plan = promotion.plan(run.run_id, source)

    assert len(plan.changes) == 1
    assert plan.changes[0].path == "src/parser.py"
    assert plan.git_head_before == run.workspace_origin.git_head
    patch = artifacts.read(plan.diff_artifact_ref).decode("utf-8")
    assert "--- a/src/parser.py" in patch
    assert "+    return [] if value == '' else [value]" in patch
    result = promotion.promote(run.run_id, source)

    assert result.promotion_receipt is not None
    assert result.promotion_receipt.recovered_after_crash is False
    assert result.promotion_receipt.source_revision_after == plan.candidate_revision
    assert "return [] if" in (source / "src/parser.py").read_text(encoding="utf-8")
    assert _git(source, "rev-parse", "HEAD").stdout.strip() == plan.git_head_before
    assert "src/parser.py" in _git(source, "status", "--short").stdout


def test_promotion_applies_validated_multi_file_candidate(tmp_path, task_dict):
    source, _, artifacts, promoter, service, run, _ = prepare_successful_candidate(
        tmp_path,
        task_dict,
        git=False,
        multi_file=True,
    )
    promotion = PromotionService(service, promoter)
    plan = promotion.plan(run.run_id, source)

    assert [change.path for change in plan.changes] == [
        "src/parser.py",
        "tests/test_parser.py",
    ]
    patch = artifacts.read(plan.diff_artifact_ref).decode("utf-8")
    assert "--- a/src/parser.py" in patch
    assert "--- a/tests/test_parser.py" in patch

    result = promotion.promote(run.run_id, source)

    assert result.promotion_receipt is not None
    assert result.promotion_receipt.recovered_after_crash is False
    assert "return [] if" in (source / "src/parser.py").read_text(encoding="utf-8")
    assert "verifies empty input" in (source / "tests/test_parser.py").read_text(encoding="utf-8")


def test_promotion_applies_one_validated_created_file_and_replays_trace(tmp_path, task_dict):
    source, _, artifacts, promoter, service, run, db = prepare_successful_candidate(
        tmp_path,
        task_dict,
        modify_existing=False,
        created_file=True,
    )
    promotion = PromotionService(service, promoter)
    plan = promotion.plan(run.run_id, source)

    assert len(plan.changes) == 1
    change = plan.changes[0]
    assert change.path == "src/generated.py"
    assert change.kind == "created"
    assert change.before_sha256 is None
    assert artifacts.read(change.after_sha256) == ("def generated():\n    return '你好'\n".encode())
    patch = artifacts.read(plan.diff_artifact_ref).decode("utf-8")
    assert "--- /dev/null" in patch
    assert "+++ b/src/generated.py" in patch
    assert not any(child.name == "generated.py" for child in (source / "src").iterdir())

    result = promotion.promote(run.run_id, source)

    assert result.promotion_receipt is not None
    assert result.promotion_receipt.recovered_after_crash is False
    assert (source / "src/generated.py").read_bytes() == artifacts.read(change.after_sha256)
    assert "?? src/generated.py" in _git(source, "status", "--short").stdout
    repeated = promotion.promote(run.run_id, source)
    assert repeated.as_dict() == result.as_dict()
    trace = SQLiteEventStore(db).export_jsonl(run.run_id)
    replayed = SQLiteEventStore.replay_jsonl(trace)
    assert replayed.as_dict() == result.as_dict()


def test_promotion_preserves_an_empty_created_file(tmp_path, task_dict):
    source, _, artifacts, promoter, service, run, _ = prepare_successful_candidate(
        tmp_path,
        task_dict,
        git=False,
        modify_existing=False,
        created_file=True,
        created_content=b"",
    )
    promotion = PromotionService(service, promoter)
    plan = promotion.plan(run.run_id, source)

    assert plan.changes[0].kind == "created"
    patch = artifacts.read(plan.diff_artifact_ref).decode("utf-8")
    assert patch == "--- /dev/null\n+++ b/src/generated.py\n"

    result = promotion.promote(run.run_id, source)

    assert result.promotion_receipt is not None
    assert (source / "src/generated.py").is_file()
    assert (source / "src/generated.py").read_bytes() == b""


def test_promotion_rejects_multiple_created_files_and_deleted_files(tmp_path, task_dict):
    source, candidate, _, promoter, service, run, _ = prepare_successful_candidate(
        tmp_path,
        task_dict,
        modify_existing=False,
        created_file=True,
    )
    (candidate / "src/second.py").write_text("value = 2\n", encoding="utf-8")

    with pytest.raises(Conflict, match="at most one created file"):
        PromotionService(service, promoter).plan(run.run_id, source)

    (candidate / "src/second.py").unlink()
    (candidate / "tests/test_parser.py").unlink()
    with pytest.raises(Conflict, match="does not support deleted files"):
        PromotionService(service, promoter).plan(run.run_id, source)


def test_created_file_promotion_enforces_size_utf8_and_path_authority(tmp_path, task_dict):
    source, candidate, _, promoter, service, run, _ = prepare_successful_candidate(
        tmp_path,
        task_dict,
        modify_existing=False,
        created_file=True,
    )
    promotion = PromotionService(service, promoter)
    created = candidate / "src/generated.py"

    created.write_bytes(b"x" * (64 * 1024 + 1))
    with pytest.raises(PolicyDenied, match="64 KiB"):
        promotion.plan(run.run_id, source)

    created.write_bytes(b"\xff")
    with pytest.raises(PolicyDenied, match="UTF-8"):
        promotion.plan(run.run_id, source)

    created.unlink()
    (candidate / "README.md").write_text("outside scope\n", encoding="utf-8")
    with pytest.raises(PolicyDenied, match="path authority"):
        promotion.plan(run.run_id, source)


def test_created_file_promotion_rolls_back_an_ordinary_post_create_failure(
    tmp_path,
    task_dict,
    monkeypatch,
):
    source, _, _, promoter, service, run, _ = prepare_successful_candidate(
        tmp_path,
        task_dict,
        git=False,
        modify_existing=False,
        created_file=True,
    )

    promotion = PromotionService(service, promoter)
    plan = promotion.plan(run.run_id, source)
    intent = PromotionIntent(promotion_id="promotion-created-rollback", plan=plan)
    service.reserve_promotion(run.run_id, intent, "reserve-promotion-created-rollback")
    capture = promoter.snapshots.capture
    source_captures = 0

    def fail_post_effect_snapshot(root, **kwargs):
        nonlocal source_captures
        result = capture(root, **kwargs)
        if Path(root).resolve() == source.resolve():
            source_captures += 1
            if source_captures == 2:
                raise RuntimeError("simulated post-create promotion failure")
        return result

    monkeypatch.setattr(promoter.snapshots, "capture", fail_post_effect_snapshot)
    with pytest.raises(RuntimeError, match="post-create promotion"):
        promotion.promote(run.run_id, source)

    assert not (source / "src/generated.py").exists()
    pending = service.store.get(run.run_id)
    assert pending.promotion_intent is not None
    assert pending.promotion_receipt is None


def test_created_file_rollback_preserves_a_concurrent_replacement(
    tmp_path,
    task_dict,
    monkeypatch,
):
    source, _, _, promoter, service, run, _ = prepare_successful_candidate(
        tmp_path,
        task_dict,
        git=False,
        modify_existing=False,
        created_file=True,
    )
    promotion = PromotionService(service, promoter)
    plan = promotion.plan(run.run_id, source)
    intent = PromotionIntent(promotion_id="promotion-created-race", plan=plan)
    service.reserve_promotion(run.run_id, intent, "reserve-promotion-created-race")
    capture = promoter.snapshots.capture
    source_captures = 0
    target = source / "src/generated.py"

    def replace_before_post_effect_failure(root, **kwargs):
        nonlocal source_captures
        result = capture(root, **kwargs)
        if Path(root).resolve() == source.resolve():
            source_captures += 1
            if source_captures == 2:
                replacement = source / "src/.external-generated.py"
                replacement.write_bytes(target.read_bytes())
                replacement.replace(target)
                raise RuntimeError("simulated concurrent replacement")
        return result

    monkeypatch.setattr(promoter.snapshots, "capture", replace_before_post_effect_failure)
    with pytest.raises(RuntimeError, match="concurrent replacement"):
        promotion.promote(run.run_id, source)

    assert target.is_file()
    assert target.read_bytes() == ("def generated():\n    return '你好'\n".encode())
    assert service.store.get(run.run_id).promotion_receipt is None


def test_created_file_promotion_rejects_divergent_existing_effect(tmp_path, task_dict):
    source, _, _, promoter, service, run, _ = prepare_successful_candidate(
        tmp_path,
        task_dict,
        git=False,
        modify_existing=False,
        created_file=True,
    )
    promotion = PromotionService(service, promoter)
    plan = promotion.plan(run.run_id, source)
    intent = PromotionIntent(promotion_id="promotion-created-diverged", plan=plan)
    service.reserve_promotion(run.run_id, intent, "reserve-promotion-created-diverged")
    (source / "src/generated.py").write_text("external = True\n", encoding="utf-8")

    with pytest.raises(Conflict, match="divergent created promotion effect"):
        promotion.promote(run.run_id, source)

    assert (source / "src/generated.py").read_text(encoding="utf-8") == "external = True\n"
    assert service.store.get(run.run_id).promotion_receipt is None


def test_pending_promotion_recovers_after_file_effect_without_second_write(tmp_path, task_dict):
    source, candidate, _, promoter, service, run, db = prepare_successful_candidate(
        tmp_path,
        task_dict,
        git=False,
    )
    promotion = PromotionService(service, promoter)
    plan = promotion.plan(run.run_id, source)
    intent = PromotionIntent(promotion_id="promotion-crash", plan=plan)
    service.reserve_promotion(run.run_id, intent, "reserve-promotion-crash")
    revision, _, recovered = promoter.apply_or_recover(
        plan,
        source,
        candidate,
        run.task.constraints,
    )
    assert revision == plan.candidate_revision
    assert recovered is False
    assert SQLiteEventStore(db).get(run.run_id).promotion_receipt is None

    reopened = HarnessService(SQLiteEventStore(db))
    settled = PromotionService(reopened, promoter).promote(run.run_id, source)

    assert settled.promotion_receipt is not None
    assert settled.promotion_receipt.recovered_after_crash is True
    assert "return [] if" in (source / "src/parser.py").read_text(encoding="utf-8")


def test_promotion_recovers_after_subprocess_hard_exit_before_receipt(tmp_path, task_dict):
    source, candidate, artifacts, promoter, service, run, db = prepare_successful_candidate(
        tmp_path,
        task_dict,
        git=False,
    )
    promotion = PromotionService(service, promoter)
    plan = promotion.plan(run.run_id, source)
    intent = PromotionIntent(promotion_id="promotion-hard-exit", plan=plan)
    service.reserve_promotion(run.run_id, intent, "reserve-promotion-hard-exit")
    script = textwrap.dedent(
        """
        import os
        import sys
        from pathlib import Path

        from horizon.adapters.persistence.artifacts import ArtifactStore
        from horizon.adapters.persistence.sqlite import SQLiteEventStore
        from horizon.adapters.workspace.promotion import WorkspacePromoter
        from horizon.adapters.workspace.snapshot import SnapshotManager

        store = SQLiteEventStore(Path(sys.argv[1]))
        run = store.get(sys.argv[2])
        assert run.promotion_intent is not None
        promoter = WorkspacePromoter(
            SnapshotManager(ArtifactStore(Path(sys.argv[3])))
        )
        promoter.apply_or_recover(
            run.promotion_intent.plan,
            Path(sys.argv[4]),
            Path(sys.argv[5]),
            run.task.constraints,
        )
        os._exit(27)
        """
    )
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(db),
            run.run_id,
            str(artifacts.root),
            str(source),
            str(candidate),
        ],
        capture_output=True,
        timeout=20,
    )
    assert crashed.returncode == 27, crashed.stderr.decode(errors="replace")
    pending = SQLiteEventStore(db).get(run.run_id)
    assert pending.promotion_intent == intent
    assert pending.promotion_receipt is None
    assert "return [] if" in (source / "src/parser.py").read_text(encoding="utf-8")

    reopened = HarnessService(SQLiteEventStore(db))
    settled = PromotionService(reopened, promoter).promote(run.run_id, source)
    assert settled.promotion_receipt is not None
    assert settled.promotion_receipt.recovered_after_crash is True


def test_multi_file_promotion_resumes_after_partial_subprocess_effect(tmp_path, task_dict):
    source, candidate, artifacts, promoter, service, run, db = prepare_successful_candidate(
        tmp_path,
        task_dict,
        git=False,
        multi_file=True,
    )
    promotion = PromotionService(service, promoter)
    plan = promotion.plan(run.run_id, source)
    intent = PromotionIntent(promotion_id="promotion-partial-hard-exit", plan=plan)
    service.reserve_promotion(run.run_id, intent, "reserve-promotion-partial-hard-exit")
    script = textwrap.dedent(
        """
        import os
        import sys
        from pathlib import Path

        from horizon.adapters.persistence.artifacts import ArtifactStore
        from horizon.adapters.persistence.sqlite import SQLiteEventStore
        from horizon.adapters.workspace.promotion import WorkspacePromoter
        from horizon.adapters.workspace.snapshot import SnapshotManager

        class ExitAfterFirstWrite(WorkspacePromoter):
            def _atomic_write(self, target, content, suffix):
                WorkspacePromoter._atomic_write(target, content, suffix)
                os._exit(28)

        store = SQLiteEventStore(Path(sys.argv[1]))
        run = store.get(sys.argv[2])
        assert run.promotion_intent is not None
        promoter = ExitAfterFirstWrite(
            SnapshotManager(ArtifactStore(Path(sys.argv[3])))
        )
        promoter.apply_or_recover(
            run.promotion_intent.plan,
            Path(sys.argv[4]),
            Path(sys.argv[5]),
            run.task.constraints,
        )
        """
    )
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(db),
            run.run_id,
            str(artifacts.root),
            str(source),
            str(candidate),
        ],
        capture_output=True,
        timeout=20,
    )

    assert crashed.returncode == 28, crashed.stderr.decode(errors="replace")
    assert "return [] if" in (source / "src/parser.py").read_text(encoding="utf-8")
    assert "verifies empty input" not in (source / "tests/test_parser.py").read_text(
        encoding="utf-8"
    )
    pending = SQLiteEventStore(db).get(run.run_id)
    assert pending.promotion_intent == intent
    assert pending.promotion_receipt is None

    reopened = HarnessService(SQLiteEventStore(db))
    settled = PromotionService(reopened, promoter).promote(run.run_id, source)

    assert settled.promotion_receipt is not None
    assert settled.promotion_receipt.recovered_after_crash is True
    assert "return [] if" in (source / "src/parser.py").read_text(encoding="utf-8")
    assert "verifies empty input" in (source / "tests/test_parser.py").read_text(encoding="utf-8")


def test_promotion_resumes_after_created_file_subprocess_effect(tmp_path, task_dict):
    source, candidate, artifacts, promoter, service, run, db = prepare_successful_candidate(
        tmp_path,
        task_dict,
        git=False,
        created_file=True,
    )
    promotion = PromotionService(service, promoter)
    plan = promotion.plan(run.run_id, source)
    assert [change.kind for change in plan.changes] == ["created", "modified"]
    intent = PromotionIntent(promotion_id="promotion-created-hard-exit", plan=plan)
    service.reserve_promotion(run.run_id, intent, "reserve-promotion-created-hard-exit")
    script = textwrap.dedent(
        """
        import os
        import sys
        from pathlib import Path

        from horizon.adapters.persistence.artifacts import ArtifactStore
        from horizon.adapters.persistence.sqlite import SQLiteEventStore
        from horizon.adapters.workspace.promotion import WorkspacePromoter
        from horizon.adapters.workspace.snapshot import SnapshotManager

        class ExitAfterCreate(WorkspacePromoter):
            def _exclusive_create(self, target, content):
                WorkspacePromoter._exclusive_create(target, content)
                os._exit(29)

        store = SQLiteEventStore(Path(sys.argv[1]))
        run = store.get(sys.argv[2])
        assert run.promotion_intent is not None
        promoter = ExitAfterCreate(
            SnapshotManager(ArtifactStore(Path(sys.argv[3])))
        )
        promoter.apply_or_recover(
            run.promotion_intent.plan,
            Path(sys.argv[4]),
            Path(sys.argv[5]),
            run.task.constraints,
        )
        """
    )
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(db),
            run.run_id,
            str(artifacts.root),
            str(source),
            str(candidate),
        ],
        capture_output=True,
        timeout=20,
    )

    assert crashed.returncode == 29, crashed.stderr.decode(errors="replace")
    assert (source / "src/generated.py").is_file()
    assert "return [value]" in (source / "src/parser.py").read_text(encoding="utf-8")
    pending = SQLiteEventStore(db).get(run.run_id)
    assert pending.promotion_intent == intent
    assert pending.promotion_receipt is None

    reopened = HarnessService(SQLiteEventStore(db))
    settled = PromotionService(reopened, promoter).promote(run.run_id, source)

    assert settled.promotion_receipt is not None
    assert settled.promotion_receipt.recovered_after_crash is True
    assert "return [] if" in (source / "src/parser.py").read_text(encoding="utf-8")
    trace = SQLiteEventStore(db).export_jsonl(run.run_id)
    assert SQLiteEventStore.replay_jsonl(trace).as_dict() == settled.as_dict()


def test_multi_file_promotion_rejects_divergent_partial_effect(tmp_path, task_dict):
    source, candidate, _, promoter, service, run, _ = prepare_successful_candidate(
        tmp_path,
        task_dict,
        git=False,
        multi_file=True,
    )
    promotion = PromotionService(service, promoter)
    plan = promotion.plan(run.run_id, source)
    intent = PromotionIntent(promotion_id="promotion-diverged", plan=plan)
    service.reserve_promotion(run.run_id, intent, "reserve-promotion-diverged")
    (source / "src/parser.py").write_bytes((candidate / "src/parser.py").read_bytes())
    (source / "tests/test_parser.py").write_text(
        "# unrelated external content\n",
        encoding="utf-8",
    )

    with pytest.raises(Conflict, match="divergent partial"):
        promotion.promote(run.run_id, source)

    pending = service.store.get(run.run_id)
    assert pending.promotion_intent == intent
    assert pending.promotion_receipt is None
    assert "unrelated external content" in (source / "tests/test_parser.py").read_text(
        encoding="utf-8"
    )


def test_promotion_rejects_source_drift_without_reserving_intent(tmp_path, task_dict):
    source, _, _, promoter, service, run, _ = prepare_successful_candidate(
        tmp_path,
        task_dict,
        git=False,
    )
    (source / "tests/test_parser.py").write_text("# external source drift\n", encoding="utf-8")

    with pytest.raises(Conflict, match="Source workspace changed"):
        PromotionService(service, promoter).promote(run.run_id, source)

    assert service.store.get(run.run_id).promotion_intent is None
    assert "return [value]" in (source / "src/parser.py").read_text(encoding="utf-8")


def test_promotion_cli_is_dry_run_then_requires_explicit_confirmation(
    tmp_path,
    monkeypatch,
    task_dict,
):
    source, _, _, _, _, run, db = prepare_successful_candidate(
        tmp_path,
        task_dict,
        git=False,
    )
    monkeypatch.chdir(tmp_path)
    prefix = ["--db", str(db), "agent"]
    diff = cli.invoke(
        app,
        prefix + ["diff", run.run_id, "--source", str(source)],
    )
    assert diff.exit_code == 0, diff.output
    diff_data = json.loads(diff.stdout)
    assert diff_data["mutated"] is False
    assert diff_data["changes"][0]["path"] == "src/parser.py"
    assert "return [value]" in (source / "src/parser.py").read_text(encoding="utf-8")

    denied = cli.invoke(
        app,
        prefix + ["promote", run.run_id, "--source", str(source)],
    )
    assert denied.exit_code == 2
    assert "--confirm-promote" in denied.output
    assert "return [value]" in (source / "src/parser.py").read_text(encoding="utf-8")

    promoted = cli.invoke(
        app,
        prefix
        + [
            "promote",
            run.run_id,
            "--source",
            str(source),
            "--confirm-promote",
        ],
    )
    assert promoted.exit_code == 0, promoted.output
    promoted_data = json.loads(promoted.stdout)
    assert promoted_data["git_commit_created"] is False
    assert promoted_data["recovered_after_crash"] is False
    assert "return [] if" in (source / "src/parser.py").read_text(encoding="utf-8")


def test_promotion_cli_reports_and_applies_one_created_file(
    tmp_path,
    monkeypatch,
    task_dict,
):
    source, _, _, _, _, run, db = prepare_successful_candidate(
        tmp_path,
        task_dict,
        git=False,
        modify_existing=False,
        created_file=True,
    )
    monkeypatch.chdir(tmp_path)
    prefix = ["--db", str(db), "agent"]

    diff = cli.invoke(app, prefix + ["diff", run.run_id, "--source", str(source)])

    assert diff.exit_code == 0, diff.output
    diff_data = json.loads(diff.stdout)
    assert diff_data["mutated"] is False
    assert len(diff_data["changes"]) == 1
    assert diff_data["changes"][0]["path"] == "src/generated.py"
    assert diff_data["changes"][0]["kind"] == "created"
    assert diff_data["changes"][0]["before_sha256"] is None
    assert len(diff_data["changes"][0]["after_sha256"]) == 64
    assert not (source / "src/generated.py").exists()

    promoted = cli.invoke(
        app,
        prefix
        + [
            "promote",
            run.run_id,
            "--source",
            str(source),
            "--confirm-promote",
        ],
    )

    assert promoted.exit_code == 0, promoted.output
    promoted_data = json.loads(promoted.stdout)
    assert promoted_data["recovered_after_crash"] is False
    assert (source / "src/generated.py").is_file()
