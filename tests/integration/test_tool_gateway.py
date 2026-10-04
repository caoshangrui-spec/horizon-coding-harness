from pathlib import Path

import pytest

from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.adapters.workspace.snapshot import SnapshotManager
from horizon.domain.errors import Conflict, PolicyDenied
from horizon.domain.plan import WorkItem
from horizon.domain.tools import AcceptanceResult
from horizon.tools.gateway import RetrieveArgs, WorkspaceToolGateway, tool_definitions


class FakeChecks:
    def __init__(self, passed=True):
        self.passed = passed
        self.calls = []
        self.attempt_ids = []

    def execute(self, workspace, check):
        self.calls.append((workspace, check))
        output = "ok" if self.passed else "assertion failed"
        return AcceptanceResult(
            check_id=check.id,
            passed=self.passed,
            exit_code=0 if self.passed else 1,
            timed_out=False,
            output=output,
            output_hash=("a" if self.passed else "b") * 64,
        )

    def execute_attempt(self, workspace, check, attempt_id):
        self.attempt_ids.append(attempt_id)
        return self.execute(workspace, check)


def test_retrieve_code_defaults_to_one_bounded_chunk():
    assert RetrieveArgs.model_validate({"query": "unescapeHTML"}).max_chunks == 1


def test_read_file_schema_requires_line_bounds_as_a_pair():
    definition = next(tool for tool in tool_definitions() if tool.name == "read_file")

    assert definition.parameters["dependentRequired"] == {
        "start_line": ["end_line"],
        "end_line": ["start_line"],
    }
    assert "Never send only one bound" in definition.description


def make_gateway(tmp_path: Path, task, *, checks=None):
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "tests").mkdir()
    (workspace / "src/parser.py").write_text("def parse(value):\n    return [value]\n")
    (workspace / "src/formatter.py").write_text("def format_value(value):\n    return value\n")
    (workspace / "tests/test_parser.py").write_text("# protected test fixture\n")
    (workspace / ".env").write_text("SECRET=not-visible\n")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    item = WorkItem(
        work_item_id="fix",
        title="Fix parser",
        objective="Return an empty list for empty input",
        expected_artifacts=("patch",),
        acceptance_ids=("unit",),
        allowed_tools=(
            "search_repo",
            "read_file",
            "replace_text",
            "apply_patch",
            "create_file",
            "run_check",
        ),
    )
    executor = checks or FakeChecks()
    return (
        WorkspaceToolGateway(
            workspace,
            task,
            item,
            SnapshotManager(artifacts),
            executor,
        ),
        workspace,
        executor,
    )


def test_search_read_and_exact_replace_are_bounded_and_snapshotted(tmp_path, task):
    gateway, workspace, _ = make_gateway(tmp_path, task)
    searched = gateway.dispatch("search_repo", {"query": "return [value]"})
    assert "src/parser.py:2" in searched.content
    assert "SECRET" not in searched.content

    read = gateway.dispatch("read_file", {"path": "src/parser.py"})
    assert "def parse" in read.content
    assert read.workspace_revision_before == read.workspace_revision_after
    assert read.artifact_ref != read.workspace_manifest_ref

    replaced = gateway.dispatch(
        "replace_text",
        {
            "path": "src/parser.py",
            "old": "return [value]",
            "new": "return [] if value == '' else [value]",
        },
    )
    assert replaced.workspace_revision_before != replaced.workspace_revision_after
    assert "return [] if" in (workspace / "src/parser.py").read_text()
    assert replaced.workspace_manifest_ref is not None


def test_large_read_requires_a_bounded_line_range(tmp_path, task):
    gateway, workspace, _ = make_gateway(tmp_path, task)
    target = workspace / "src/large.py"
    target.write_text(
        "".join(f"line-{index:04d}-{'x' * 100}\n" for index in range(500)),
        encoding="utf-8",
    )

    unbounded = gateway.dispatch_safe("read_file", {"path": "src/large.py"})

    assert unbounded.status == "error"
    assert "unbounded read_file output" in unbounded.content
    assert "start_line and end_line" in unbounded.content
    assert "retrieve_code evidence" in unbounded.content
    assert len(unbounded.content) < 1_000

    ranged = gateway.dispatch(
        "read_file",
        {"path": "src/large.py", "start_line": 10, "end_line": 12},
    )

    assert "READ_RANGE path=src/large.py lines=10-12 total_lines=500" in ranged.content
    assert "line-0009-" in ranged.content
    assert "line-0011-" in ranged.content
    assert "line-0008-" not in ranged.content
    assert "line-0012-" not in ranged.content


def test_read_range_contract_rejects_partial_or_oversized_ranges(tmp_path, task):
    gateway, _, _ = make_gateway(tmp_path, task)

    partial = gateway.dispatch_safe(
        "read_file",
        {"path": "src/parser.py", "start_line": 1},
    )
    oversized = gateway.dispatch_safe(
        "read_file",
        {"path": "src/parser.py", "start_line": 1, "end_line": 401},
    )

    assert partial.status == "error"
    assert "must be supplied together" in partial.content
    assert '"start_line":<start_line>,"end_line":<end_line>' in partial.content
    assert oversized.status == "error"
    assert "limited to 400 lines" in oversized.content


def test_search_reports_result_truncation_and_recommends_ranked_retrieval(tmp_path, task):
    gateway, workspace, _ = make_gateway(tmp_path, task)
    (workspace / "src/parser.py").write_text(
        "".join(f"needle = {index}\n" for index in range(40)),
        encoding="utf-8",
    )

    searched = gateway.dispatch("search_repo", {"query": "needle"})

    assert searched.content.count("src/parser.py:") == 30
    assert "SEARCH_TRUNCATED reason=result_limit" in searched.content
    assert "Use retrieve_code for ranked symbol or concept localization." in searched.content


def test_exact_replace_preserves_lf_line_endings(tmp_path, task):
    gateway, workspace, _ = make_gateway(tmp_path, task)
    target = workspace / "src/parser.py"
    target.write_bytes(b"def parse(value):\n    return [value]\n")

    gateway.dispatch(
        "replace_text",
        {
            "path": "src/parser.py",
            "old": "return [value]",
            "new": "return []",
        },
    )

    assert target.read_bytes() == b"def parse(value):\n    return []\n"


def test_apply_patch_prevalidates_and_changes_multiple_files(tmp_path, task):
    gateway, workspace, _ = make_gateway(tmp_path, task)
    arguments = {
        "edits": [
            {
                "path": "src/parser.py",
                "old": "return [value]",
                "new": "return [] if value == '' else [value]",
            },
            {
                "path": "src/formatter.py",
                "old": "return value",
                "new": "return str(value)",
            },
        ]
    }

    outcome = gateway.dispatch("apply_patch", arguments)

    assert outcome.workspace_revision_before != outcome.workspace_revision_after
    assert "return [] if" in (workspace / "src/parser.py").read_text(encoding="utf-8")
    assert "return str(value)" in (workspace / "src/formatter.py").read_text(encoding="utf-8")
    assert "2 exact edit(s) across 2 file(s)" in outcome.content


def test_apply_patch_validation_failure_has_no_effect(tmp_path, task):
    gateway, workspace, _ = make_gateway(tmp_path, task)
    parser_before = (workspace / "src/parser.py").read_bytes()
    formatter_before = (workspace / "src/formatter.py").read_bytes()

    with pytest.raises(ValueError, match="occurrence"):
        gateway.dispatch(
            "apply_patch",
            {
                "edits": [
                    {
                        "path": "src/parser.py",
                        "old": "return [value]",
                        "new": "return []",
                    },
                    {
                        "path": "src/formatter.py",
                        "old": "missing exact text",
                        "new": "replacement",
                    },
                ]
            },
        )

    assert (workspace / "src/parser.py").read_bytes() == parser_before
    assert (workspace / "src/formatter.py").read_bytes() == formatter_before


def test_create_file_writes_exact_utf8_content_and_snapshots_it(tmp_path, task):
    gateway, workspace, _ = make_gateway(tmp_path, task)
    content = "def greet():\n    return '你好'\n"

    outcome = gateway.dispatch(
        "create_file",
        {"path": "src/greeting.py", "content": content},
    )

    encoded = content.encode("utf-8")
    assert (workspace / "src/greeting.py").read_bytes() == encoded
    assert outcome.status == "success"
    assert outcome.workspace_revision_before != outcome.workspace_revision_after
    assert outcome.workspace_manifest_ref is not None
    manifest = gateway.verify_manifest(outcome.workspace_manifest_ref)
    created = next(entry for entry in manifest.files if entry.path == "src/greeting.py")
    assert created.size_bytes == len(encoded)
    assert created.sha256 in outcome.content
    assert f"{len(encoded)} UTF-8 bytes" in outcome.content


def test_create_file_rejects_existing_denied_missing_parent_and_oversized_content(
    tmp_path,
    task,
):
    gateway, workspace, _ = make_gateway(tmp_path, task)
    original = (workspace / "src/parser.py").read_bytes()

    existing = gateway.dispatch_safe(
        "create_file",
        {"path": "src/parser.py", "content": "overwritten"},
    )
    denied = gateway.dispatch_safe(
        "create_file",
        {"path": ".env", "content": "replacement secret"},
    )
    missing_parent = gateway.dispatch_safe(
        "create_file",
        {"path": "src/generated/module.py", "content": "value = 1\n"},
    )
    oversized = gateway.dispatch_safe(
        "create_file",
        {"path": "src/oversized.py", "content": "界" * 21_846},
    )
    non_portable = gateway.dispatch_safe(
        "create_file",
        {"path": "src/CON.py", "content": "value = 1\n"},
    )

    assert existing.status == "error"
    assert "must not already exist" in existing.content
    assert denied.status == "error"
    assert "authority scope" in denied.content
    assert missing_parent.status == "error"
    assert "parent must be an existing" in missing_parent.content
    assert oversized.status == "error"
    assert "UTF-8 byte limit" in oversized.content
    assert non_portable.status == "error"
    assert "not portable to Windows" in non_portable.content
    assert (workspace / "src/parser.py").read_bytes() == original
    assert not (workspace / "src/generated").exists()
    assert not (workspace / "src/oversized.py").exists()
    assert "con.py" not in {child.name.casefold() for child in (workspace / "src").iterdir()}


def test_create_file_rolls_back_an_ordinary_post_write_failure(tmp_path, task, monkeypatch):
    gateway, workspace, _ = make_gateway(tmp_path, task)
    snapshot = gateway._snapshot
    calls = 0

    def fail_second_snapshot():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated post-write snapshot failure")
        return snapshot()

    monkeypatch.setattr(gateway, "_snapshot", fail_second_snapshot)

    with pytest.raises(RuntimeError, match="post-write snapshot"):
        gateway.dispatch(
            "create_file",
            {"path": "src/transient.py", "content": "value = 1\n"},
        )

    assert not (workspace / "src/transient.py").exists()


def test_create_file_schema_is_exposed_only_when_work_item_allows_it(tmp_path, task):
    gateway, _, _ = make_gateway(tmp_path, task)
    assert "create_file" in {definition.name for definition in gateway.definitions}

    gateway.activate_work_item(
        WorkItem(
            work_item_id="read",
            title="Inspect parser",
            objective="Read without changing files",
            expected_artifacts=("observation",),
            acceptance_ids=("unit",),
            allowed_tools=("read_file",),
        )
    )

    assert "create_file" not in {definition.name for definition in gateway.definitions}
    with pytest.raises(PolicyDenied, match="not allowed"):
        gateway.dispatch(
            "create_file",
            {"path": "src/not-created.py", "content": "value = 1\n"},
        )


def test_apply_patch_runtime_failure_rolls_back_written_files(tmp_path, task, monkeypatch):
    gateway, workspace, _ = make_gateway(tmp_path, task)
    parser_before = (workspace / "src/parser.py").read_bytes()
    formatter_before = (workspace / "src/formatter.py").read_bytes()
    atomic_write = gateway._atomic_write
    patch_writes = 0

    def fail_second_patch_write(target, content, suffix):
        nonlocal patch_writes
        if suffix == "patch":
            patch_writes += 1
            if patch_writes == 2:
                raise RuntimeError("simulated second-file write failure")
        atomic_write(target, content, suffix)

    monkeypatch.setattr(gateway, "_atomic_write", fail_second_patch_write)
    with pytest.raises(RuntimeError, match="second-file"):
        gateway.dispatch(
            "apply_patch",
            {
                "edits": [
                    {
                        "path": "src/parser.py",
                        "old": "return [value]",
                        "new": "return []",
                    },
                    {
                        "path": "src/formatter.py",
                        "old": "return value",
                        "new": "return str(value)",
                    },
                ]
            },
        )

    assert (workspace / "src/parser.py").read_bytes() == parser_before
    assert (workspace / "src/formatter.py").read_bytes() == formatter_before


def test_apply_patch_recovery_accepts_exact_effect_and_rolls_back(tmp_path, task):
    gateway, workspace, _ = make_gateway(tmp_path, task)
    arguments = {
        "edits": [
            {
                "path": "src/parser.py",
                "old": "return [value]",
                "new": "return [] if value == '' else [value]",
            },
            {
                "path": "src/formatter.py",
                "old": "return value",
                "new": "return str(value)",
            },
        ]
    }
    pre_revision, pre_manifest = gateway.checkpoint()
    outcome = gateway.dispatch("apply_patch", arguments)

    assessment = gateway.assess_patch_recovery(arguments, pre_manifest)
    assert assessment.state == "expected_effect"
    assert assessment.paths == ("src/parser.py", "src/formatter.py")
    assert assessment.current_revision == outcome.workspace_revision_after
    restored_revision, restored_manifest = gateway.rollback_patch_recovery(
        arguments,
        pre_manifest,
        assessment.current_revision,
    )

    assert restored_revision == pre_revision
    assert gateway.verify_manifest(restored_manifest).workspace_revision == pre_revision
    assert "return [value]" in (workspace / "src/parser.py").read_text(encoding="utf-8")
    assert "return value" in (workspace / "src/formatter.py").read_text(encoding="utf-8")


def test_apply_patch_recovery_keeps_partial_effect_blocked(tmp_path, task):
    gateway, workspace, _ = make_gateway(tmp_path, task)
    arguments = {
        "edits": [
            {
                "path": "src/parser.py",
                "old": "return [value]",
                "new": "return []",
            },
            {
                "path": "src/formatter.py",
                "old": "return value",
                "new": "return str(value)",
            },
        ]
    }
    _, pre_manifest = gateway.checkpoint()
    (workspace / "src/parser.py").write_text(
        "def parse(value):\n    return []\n",
        encoding="utf-8",
    )

    assessment = gateway.assess_patch_recovery(arguments, pre_manifest)

    assert assessment.state == "diverged"
    with pytest.raises(Conflict, match="Diverged patch"):
        gateway.rollback_patch_recovery(
            arguments,
            pre_manifest,
            assessment.current_revision,
        )


def test_replace_recovery_assesses_exact_effect_and_rolls_back(tmp_path, task):
    gateway, workspace, _ = make_gateway(tmp_path, task)
    arguments = {
        "path": "src/parser.py",
        "old": "return [value]",
        "new": "return [] if value == '' else [value]",
    }
    pre_revision, pre_manifest = gateway.checkpoint()
    outcome = gateway.dispatch("replace_text", arguments)

    assessment = gateway.assess_replace_recovery(arguments, pre_manifest)
    assert assessment.state == "expected_effect"
    assert assessment.pre_revision == pre_revision
    assert assessment.current_revision == outcome.workspace_revision_after
    restored_revision, restored_manifest = gateway.rollback_replace_recovery(
        arguments,
        pre_manifest,
        assessment.current_revision,
    )

    assert restored_revision == pre_revision
    assert gateway.verify_manifest(restored_manifest).workspace_revision == pre_revision
    assert "return [value]" in (workspace / "src/parser.py").read_text(encoding="utf-8")
    assert gateway.assess_replace_recovery(arguments, pre_manifest).state == "pre_effect"


def test_replace_recovery_rejects_diverged_workspace(tmp_path, task):
    gateway, workspace, _ = make_gateway(tmp_path, task)
    arguments = {
        "path": "src/parser.py",
        "old": "return [value]",
        "new": "return [] if value == '' else [value]",
    }
    _, pre_manifest = gateway.checkpoint()
    gateway.dispatch("replace_text", arguments)
    (workspace / "tests/test_parser.py").write_text(
        "# external drift\n",
        encoding="utf-8",
    )
    assessment = gateway.assess_replace_recovery(arguments, pre_manifest)
    assert assessment.state == "diverged"

    with pytest.raises(Conflict, match="Diverged"):
        gateway.rollback_replace_recovery(
            arguments,
            pre_manifest,
            assessment.current_revision,
        )


def test_denied_paths_and_ambiguous_replacements_have_no_effect(tmp_path, task):
    gateway, workspace, _ = make_gateway(tmp_path, task)
    with pytest.raises(PolicyDenied, match="authority"):
        gateway.dispatch("read_file", {"path": ".env"})
    original = (workspace / "src/parser.py").read_bytes()
    with pytest.raises(ValueError, match="occurrence"):
        gateway.dispatch(
            "replace_text",
            {
                "path": "src/parser.py",
                "old": "missing",
                "new": "replacement",
            },
        )
    assert (workspace / "src/parser.py").read_bytes() == original


def test_model_can_only_select_protected_check_id(tmp_path, task):
    checks = FakeChecks(passed=False)
    gateway, workspace, _ = make_gateway(tmp_path, task, checks=checks)
    outcome = gateway.dispatch("run_check", {"check_id": "unit"})
    assert outcome.status == "error"
    assert "assertion failed" in outcome.content
    assert checks.calls[0][0] == workspace
    assert checks.calls[0][1].command == "python -m pytest -q"
    with pytest.raises(PolicyDenied, match="check"):
        gateway.dispatch("run_check", {"check_id": "invented"})


def test_run_check_passes_persisted_tool_call_id_to_durable_executor(tmp_path, task):
    checks = FakeChecks()
    gateway, _, _ = make_gateway(tmp_path, task, checks=checks)

    outcome = gateway.dispatch(
        "run_check",
        {"check_id": "unit"},
        attempt_id="tool_check_123",
    )

    assert outcome.status == "success"
    assert checks.attempt_ids == ["tool_check_123"]


def test_unlisted_tools_are_rejected_before_dispatch(tmp_path, task):
    gateway, _, _ = make_gateway(tmp_path, task)
    with pytest.raises(PolicyDenied, match="not allowed"):
        gateway.dispatch("run_command", {"command": "anything"})
