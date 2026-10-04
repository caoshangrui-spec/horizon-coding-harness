import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import horizon.adapters.retrieval.sqlite_fts as retrieval_module
from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.adapters.retrieval.sqlite_fts import SQLiteCodeRetriever
from horizon.adapters.workspace.snapshot import SnapshotManager
from horizon.domain.errors import IntegrityError
from horizon.domain.plan import WorkItem
from horizon.domain.retrieval import EvidencePack
from horizon.tools.gateway import WorkspaceToolGateway


def setup_retrieval(tmp_path: Path):
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "tests").mkdir()
    (workspace / "src/parser.py").write_text(
        "def parse_value(value):\n    if value == '':\n        return []\n    return [value]\n",
        encoding="utf-8",
    )
    (workspace / "tests/test_parser.py").write_text(
        "# unitOnlySentinelXYZ\ndef test_empty_value():\n    assert parse_value('') == []\n",
        encoding="utf-8",
    )
    (workspace / ".env").write_text("SECRET_TOKEN=never-index\n", encoding="utf-8")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    snapshots = SnapshotManager(artifacts)
    snapshot, manifest = snapshots.capture(workspace)
    retriever = SQLiteCodeRetriever(tmp_path / "retrieval.sqlite3", snapshots)
    return workspace, snapshots, snapshot, manifest, retriever


def test_fts_retrieval_is_revision_bound_ranked_and_cached(tmp_path):
    _, _, snapshot, manifest, retriever = setup_retrieval(tmp_path)

    first = retriever.retrieve(
        source_manifest_ref=manifest,
        workspace_revision=snapshot.workspace_revision,
        allowed_paths=("src/**", "tests/**"),
        denied_paths=(".env",),
        query="parse_value empty",
        max_chunks=4,
    )
    second = retriever.retrieve(
        source_manifest_ref=manifest,
        workspace_revision=snapshot.workspace_revision,
        allowed_paths=("src/**", "tests/**"),
        denied_paths=(".env",),
        query="parse_value empty",
        max_chunks=4,
    )

    assert first == second
    assert first.backend == "sqlite_fts5"
    assert first.status == "ok"
    assert first.chunks
    assert first.chunks[0].rank == 1
    assert first.chunks[0].path in {"src/parser.py", "tests/test_parser.py"}
    assert (
        first.chunks[0].content_hash
        == hashlib.sha256(first.chunks[0].snippet.encode("utf-8")).hexdigest()
    )
    assert "never-index" not in json.dumps(first.model_dump(mode="json"))
    with sqlite3.connect(retriever.path) as db:
        assert db.execute("SELECT count(*) FROM retrieval_indexes").fetchone()[0] == 1


@pytest.mark.parametrize("force_scan", [False, True])
def test_retrieval_diversifies_paths_before_repeating_a_file(tmp_path, force_scan):
    workspace, snapshots, _, _, retriever = setup_retrieval(tmp_path)
    if force_scan:
        retriever.fts5_available = False
    (workspace / "src/primary.py").write_text(
        "\n".join(f"shared_marker = {index}" for index in range(90)) + "\n",
        encoding="utf-8",
    )
    (workspace / "src/secondary.py").write_text(
        "shared_marker = 'secondary'\n",
        encoding="utf-8",
    )
    snapshot, manifest = snapshots.capture(workspace)

    result = retriever.retrieve(
        source_manifest_ref=manifest,
        workspace_revision=snapshot.workspace_revision,
        allowed_paths=("src/**",),
        denied_paths=(".env",),
        query="shared_marker",
        max_chunks=3,
    )

    paths = tuple(chunk.path for chunk in result.chunks)
    assert len(paths) == 3
    assert set(paths[:2]) == {"src/primary.py", "src/secondary.py"}
    assert paths[2] == "src/primary.py"
    assert result.backend == ("lexical_scan" if force_scan else "sqlite_fts5")


def test_natural_language_terms_find_a_camel_case_identifier(tmp_path):
    workspace, snapshots, _, _, retriever = setup_retrieval(tmp_path)
    (workspace / "src/http_client.py").write_text(
        "def parseHTTPResponseCode(payload):\n    return payload.status\n",
        encoding="utf-8",
    )
    (workspace / "src/http_notes.py").write_text(
        ("# parse HTTP response code documentation\n" * 20),
        encoding="utf-8",
    )
    (workspace / "src/http_calls.py").write_text(
        ("result = parseHTTPResponseCode(payload)\n" * 20),
        encoding="utf-8",
    )
    snapshot, manifest = snapshots.capture(workspace)

    result = retriever.retrieve(
        source_manifest_ref=manifest,
        workspace_revision=snapshot.workspace_revision,
        allowed_paths=("src/**",),
        denied_paths=(".env",),
        query="parse HTTP response code",
        max_chunks=3,
    )

    assert "parsehttpresponsecode" in result.normalized_terms
    assert result.status == "ok"
    assert result.chunks[0].path == "src/http_client.py"


def test_camel_case_query_finds_a_snake_case_identifier(tmp_path):
    workspace, snapshots, _, _, retriever = setup_retrieval(tmp_path)
    (workspace / "src/status_parser.py").write_text(
        "def parse_http_response_code(payload):\n    return payload.status\n",
        encoding="utf-8",
    )
    (workspace / "src/status_notes.py").write_text(
        ("# parse HTTP response code documentation\n" * 20),
        encoding="utf-8",
    )
    snapshot, manifest = snapshots.capture(workspace)

    result = retriever.retrieve(
        source_manifest_ref=manifest,
        workspace_revision=snapshot.workspace_revision,
        allowed_paths=("src/**",),
        denied_paths=(".env",),
        query="parseHTTPResponseCode",
        max_chunks=3,
    )

    assert {"parse", "http", "response", "code"}.issubset(result.normalized_terms)
    assert result.status == "ok"
    assert result.chunks[0].path == "src/status_parser.py"


@pytest.mark.parametrize("force_scan", [False, True])
def test_same_name_definitions_use_module_path_context(tmp_path, force_scan):
    workspace, snapshots, _, _, retriever = setup_retrieval(tmp_path)
    if force_scan:
        retriever.fts5_available = False
    (workspace / "src/orders").mkdir()
    (workspace / "src/audit").mkdir()
    (workspace / "src/orders/invoice.py").write_text(
        '"""Audit audit audit integration notes."""\n'
        "def render_invoice(order):\n"
        "    return order['id']\n",
        encoding="utf-8",
    )
    (workspace / "src/audit/invoice.py").write_text(
        '"""Orders orders orders integration notes."""\n'
        "def render_invoice(event):\n"
        "    return event['id']\n",
        encoding="utf-8",
    )
    snapshot, manifest = snapshots.capture(workspace)

    orders = retriever.retrieve(
        source_manifest_ref=manifest,
        workspace_revision=snapshot.workspace_revision,
        allowed_paths=("src/**",),
        denied_paths=(".env",),
        query="orders renderInvoice",
        max_chunks=1,
    )
    audit = retriever.retrieve(
        source_manifest_ref=manifest,
        workspace_revision=snapshot.workspace_revision,
        allowed_paths=("src/**",),
        denied_paths=(".env",),
        query="audit renderInvoice",
        max_chunks=1,
    )

    assert "renderinvoice" in orders.normalized_terms
    assert orders.chunks[0].path == "src/orders/invoice.py"
    assert audit.chunks[0].path == "src/audit/invoice.py"
    assert orders.backend == ("lexical_scan" if force_scan else "sqlite_fts5")


def test_dirty_revision_rebuilds_same_name_ranking_and_preserves_old_evidence(tmp_path):
    workspace, snapshots, _, _, retriever = setup_retrieval(tmp_path)
    (workspace / "src/orders").mkdir()
    (workspace / "src/audit").mkdir()
    orders_path = workspace / "src/orders/invoice.py"
    orders_path.write_text(
        "def render_invoice(order):\n    return order['id']\n",
        encoding="utf-8",
    )
    (workspace / "src/audit/invoice.py").write_text(
        "# orders orders orders\ndef render_invoice(event):\n    return event['id']\n",
        encoding="utf-8",
    )
    first_snapshot, first_manifest = snapshots.capture(workspace)
    before = retriever.retrieve(
        source_manifest_ref=first_manifest,
        workspace_revision=first_snapshot.workspace_revision,
        allowed_paths=("src/**",),
        denied_paths=(".env",),
        query="orders renderInvoice",
        max_chunks=1,
    )

    orders_path.write_text(
        "def render_order_receipt(order):\n    return order['id']\n",
        encoding="utf-8",
    )
    (workspace / "src/billing").mkdir()
    (workspace / "src/billing/invoice.py").write_text(
        "# audit audit audit\ndef render_invoice(invoice):\n    return invoice['id']\n",
        encoding="utf-8",
    )
    second_snapshot, second_manifest = snapshots.capture(workspace)
    after = retriever.retrieve(
        source_manifest_ref=second_manifest,
        workspace_revision=second_snapshot.workspace_revision,
        allowed_paths=("src/**",),
        denied_paths=(".env",),
        query="billing renderInvoice",
        max_chunks=1,
    )
    old_replay = retriever.retrieve(
        source_manifest_ref=first_manifest,
        workspace_revision=first_snapshot.workspace_revision,
        allowed_paths=("src/**",),
        denied_paths=(".env",),
        query="orders renderInvoice",
        max_chunks=1,
    )

    assert before.chunks[0].path == "src/orders/invoice.py"
    assert after.chunks[0].path == "src/billing/invoice.py"
    assert after.index_key != before.index_key
    assert after.workspace_revision != before.workspace_revision
    assert old_replay == before


def test_scope_and_empty_are_distinct_from_degraded(tmp_path):
    _, _, snapshot, manifest, retriever = setup_retrieval(tmp_path)

    result = retriever.retrieve(
        source_manifest_ref=manifest,
        workspace_revision=snapshot.workspace_revision,
        allowed_paths=("src/**",),
        denied_paths=(".env",),
        query="unitOnlySentinelXYZ",
        max_chunks=3,
    )

    assert result.status == "empty"
    assert result.degradation_reasons == ()
    assert result.chunks == ()
    assert result.indexed_file_count == 1


def test_dirty_workspace_revision_creates_a_separate_index(tmp_path):
    workspace, snapshots, first_snapshot, first_manifest, retriever = setup_retrieval(tmp_path)
    (workspace / "src/parser.py").write_text(
        "def parse_value(value):\n    return normalize_empty(value)\n",
        encoding="utf-8",
    )
    second_snapshot, second_manifest = snapshots.capture(workspace)

    old = retriever.retrieve(
        source_manifest_ref=first_manifest,
        workspace_revision=first_snapshot.workspace_revision,
        allowed_paths=("src/**",),
        denied_paths=(".env",),
        query="normalize_empty",
        max_chunks=3,
    )
    new = retriever.retrieve(
        source_manifest_ref=second_manifest,
        workspace_revision=second_snapshot.workspace_revision,
        allowed_paths=("src/**",),
        denied_paths=(".env",),
        query="normalize_empty",
        max_chunks=3,
    )

    assert old.status == "empty"
    assert new.status == "ok"
    assert new.index_key != old.index_key
    assert new.workspace_revision != old.workspace_revision
    with sqlite3.connect(retriever.path) as db:
        assert db.execute("SELECT count(*) FROM retrieval_indexes").fetchone()[0] == 2


def test_non_utf8_file_marks_evidence_degraded_without_hiding_matches(tmp_path):
    workspace, snapshots, _, _, retriever = setup_retrieval(tmp_path)
    (workspace / "src/blob.bin").write_bytes(b"\xff\xfe\x00")
    snapshot, manifest = snapshots.capture(workspace)

    result = retriever.retrieve(
        source_manifest_ref=manifest,
        workspace_revision=snapshot.workspace_revision,
        allowed_paths=("src/**",),
        denied_paths=(".env",),
        query="parse_value",
        max_chunks=3,
    )

    assert result.status == "degraded"
    assert result.backend == "sqlite_fts5"
    assert result.skipped_file_count == 1
    assert result.degradation_reasons == ("non_utf8_file_skipped",)
    assert result.chunks


def test_lexical_scan_fallback_is_explicitly_degraded(tmp_path):
    _, _, snapshot, manifest, retriever = setup_retrieval(tmp_path)
    retriever.fts5_available = False

    result = retriever.retrieve(
        source_manifest_ref=manifest,
        workspace_revision=snapshot.workspace_revision,
        allowed_paths=("src/**",),
        denied_paths=(".env",),
        query="parse_value",
        max_chunks=3,
    )

    assert result.backend == "lexical_scan"
    assert result.status == "degraded"
    assert result.degradation_reasons == ("fts5_unavailable",)
    assert result.chunks[0].path == "src/parser.py"


def test_retrieval_rejects_manifest_revision_mismatch(tmp_path):
    _, _, snapshot, manifest, retriever = setup_retrieval(tmp_path)

    with pytest.raises(IntegrityError, match="workspace revision"):
        retriever.retrieve(
            source_manifest_ref=manifest,
            workspace_revision="f" * 64,
            allowed_paths=("src/**",),
            denied_paths=(".env",),
            query="parse_value",
            max_chunks=3,
        )


def test_retrieval_rejects_tampered_derived_index(tmp_path):
    _, _, snapshot, manifest, retriever = setup_retrieval(tmp_path)
    first = retriever.retrieve(
        source_manifest_ref=manifest,
        workspace_revision=snapshot.workspace_revision,
        allowed_paths=("src/**",),
        denied_paths=(".env",),
        query="parse_value",
        max_chunks=3,
    )
    with sqlite3.connect(retriever.path) as db:
        db.execute(
            "UPDATE code_chunks SET content=content || ? WHERE index_key=?",
            ("\ncorrupted-derived-cache", first.index_key),
        )

    with pytest.raises(IntegrityError, match="immutable source"):
        retriever.retrieve(
            source_manifest_ref=manifest,
            workspace_revision=snapshot.workspace_revision,
            allowed_paths=("src/**",),
            denied_paths=(".env",),
            query="parse_value",
            max_chunks=3,
        )


def test_retrieval_cache_is_bounded_and_pruned_indexes_rebuild(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(retrieval_module, "MAX_CACHED_INDEXES", 2)
    workspace, snapshots, first_snapshot, first_manifest, retriever = setup_retrieval(tmp_path)
    revisions = [(first_snapshot, first_manifest)]
    for version in (2, 3):
        (workspace / "src/parser.py").write_text(
            f"def parse_value_{version}(value):\n    return [value]\n",
            encoding="utf-8",
        )
        revisions.append(snapshots.capture(workspace))
    for snapshot, manifest in revisions:
        retriever.retrieve(
            source_manifest_ref=manifest,
            workspace_revision=snapshot.workspace_revision,
            allowed_paths=("src/**",),
            denied_paths=(".env",),
            query="parse",
            max_chunks=2,
        )
    with sqlite3.connect(retriever.path) as db:
        assert db.execute("SELECT count(*) FROM retrieval_indexes").fetchone()[0] == 2

    rebuilt = retriever.retrieve(
        source_manifest_ref=first_manifest,
        workspace_revision=first_snapshot.workspace_revision,
        allowed_paths=("src/**",),
        denied_paths=(".env",),
        query="parse_value",
        max_chunks=2,
    )

    assert rebuilt.status == "ok"
    with sqlite3.connect(retriever.path) as db:
        assert db.execute("SELECT count(*) FROM retrieval_indexes").fetchone()[0] == 2


def test_gateway_returns_a_content_addressed_evidence_pack(tmp_path, task):
    workspace, snapshots, _, _, retriever = setup_retrieval(tmp_path)
    item = WorkItem(
        work_item_id="inspect",
        title="Inspect parser",
        objective="Find parser behavior",
        expected_artifacts=("evidence",),
        acceptance_ids=("unit",),
        allowed_tools=("retrieve_code",),
    )
    gateway = WorkspaceToolGateway(
        workspace,
        task,
        item,
        snapshots,
        None,
        retriever,
    )

    outcome = gateway.dispatch(
        "retrieve_code",
        {"query": "parse_value empty", "max_chunks": 2},
    )
    pack = EvidencePack.model_validate_json(outcome.content)

    assert outcome.status == "success"
    assert outcome.workspace_revision_before == outcome.workspace_revision_after
    assert outcome.workspace_manifest_ref == pack.source_manifest_ref
    assert outcome.artifact_ref is not None
    assert snapshots.artifacts.read(outcome.artifact_ref).decode("utf-8") == outcome.content
    assert all(chunk.path != ".env" for chunk in pack.chunks)
