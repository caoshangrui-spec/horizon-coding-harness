import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from horizon.application.retrieval_sources import (
    RetrievalSourceMaterializer,
    SubprocessGitArchiveTransport,
    source_tree_evidence,
)
from horizon.domain.errors import IntegrityError, PolicyDenied
from horizon.domain.evaluation import (
    RetrievalEvalManifest,
    RetrievalEvalSuiteManifest,
    RetrievalSourceManifest,
)


def _tar_payload(files: dict[str, bytes], *, symlink: bool = False) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        if symlink:
            member = tarfile.TarInfo("src/escape.py")
            member.type = tarfile.SYMTYPE
            member.linkname = "../../escape.py"
            archive.addfile(member)
        for path, payload in files.items():
            member = tarfile.TarInfo(path)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    return output.getvalue()


class FakeGitArchiveTransport:
    def __init__(self, payload: bytes, *, fetched: bool = True):
        self.payload = payload
        self.fetched = fetched
        self.ensure_calls: list[tuple[str, tuple[str, ...]]] = []

    def ensure_commits(self, repository_url, commits, cache_root):
        self.ensure_calls.append((repository_url, commits))
        return cache_root / "fake.git", self.fetched

    def archive(self, repository, commit, paths):
        return self.payload


def test_subprocess_transport_forces_portable_archive_line_endings(monkeypatch):
    captured = {}

    def fake_run(arguments, **kwargs):
        captured["arguments"] = arguments
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(arguments, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = SubprocessGitArchiveTransport()._run(("version",))

    assert result.returncode == 0
    assert captured["arguments"] == (
        "git",
        "-c",
        "core.autocrlf=false",
        "-c",
        "core.eol=lf",
        "version",
    )
    assert captured["kwargs"]["env"]["GIT_TERMINAL_PROMPT"] == "0"


def _contracts(tmp_path: Path, *, partition: str = "development", tree_digest: str | None = None):
    expected_root = tmp_path / "expected"
    (expected_root / "src").mkdir(parents=True)
    payload = b"def reconcile_alpha():\n    return 'alpha'\n"
    (expected_root / "src/service.py").write_bytes(payload)
    file_count, byte_count, observed_digest = source_tree_evidence(expected_root)
    case_manifest = RetrievalEvalManifest.model_validate(
        {
            "benchmark_id": "source-case",
            "allowed_paths": ["src/**"],
            "cases": [
                {
                    "case_id": "source-case",
                    "query": "reconcile alpha",
                    "expected_paths": ["src/service.py"],
                }
            ],
        }
    )
    suite = RetrievalEvalSuiteManifest.model_validate(
        {
            "suite_id": "source-suite",
            "selection_commit": "0" * 40,
            "partition": partition,
            "cutoffs": [1, 3],
            "cases": [
                {
                    "case_id": "source-case",
                    "manifest_path": "cases/source-case.yaml",
                    "source_subdir": "source-case",
                    "buggy_commit": "1" * 40,
                }
            ],
        }
    )
    source = RetrievalSourceManifest.model_validate(
        {
            "suite_id": "source-suite",
            "evaluation_suite_path": "suite.yaml",
            "partition": partition,
            "cases": [
                {
                    "case_id": "source-case",
                    "repository_url": "https://github.com/example/project",
                    "buggy_commit": "1" * 40,
                    "archive_paths": ["src"],
                    "expected_file_count": file_count,
                    "expected_byte_count": byte_count,
                    "expected_tree_digest": tree_digest or observed_digest,
                }
            ],
        }
    )
    return payload, source, suite, {"source-case": case_manifest}


def test_materializer_groups_pinned_sources_and_writes_verified_receipt(tmp_path):
    payload, source, suite, cases = _contracts(tmp_path)
    transport = FakeGitArchiveTransport(_tar_payload({"src/service.py": payload}))
    destination = tmp_path / "sources"

    receipt = RetrievalSourceMaterializer(transport).materialize(
        source,
        suite,
        cases,
        destination=destination,
        cache_root=tmp_path / "cache",
    )

    assert (destination / "source-case/src/service.py").read_bytes() == payload
    persisted = json.loads(
        (destination / "materialization-receipt.json").read_text(encoding="utf-8")
    )
    assert persisted == receipt.model_dump(mode="json")
    assert receipt.case_count == 1
    assert receipt.network_called is True
    assert receipt.repository_code_executed is False
    assert transport.ensure_calls == [("https://github.com/example/project", ("1" * 40,))]


def test_materializer_rejects_digest_mismatch_without_partial_destination(tmp_path):
    payload, source, suite, cases = _contracts(tmp_path, tree_digest="f" * 64)
    transport = FakeGitArchiveTransport(_tar_payload({"src/service.py": payload}))
    destination = tmp_path / "sources"

    with pytest.raises(IntegrityError, match="evidence mismatch"):
        RetrievalSourceMaterializer(transport).materialize(
            source,
            suite,
            cases,
            destination=destination,
            cache_root=tmp_path / "cache",
        )

    assert not destination.exists()
    assert not list(tmp_path.glob(".sources.tmp-*"))


def test_materializer_requires_explicit_holdout_gate_before_network(tmp_path):
    payload, source, suite, cases = _contracts(tmp_path, partition="holdout")
    transport = FakeGitArchiveTransport(_tar_payload({"src/service.py": payload}))

    with pytest.raises(PolicyDenied, match="--allow-holdout"):
        RetrievalSourceMaterializer(transport).materialize(
            source,
            suite,
            cases,
            destination=tmp_path / "sources",
            cache_root=tmp_path / "cache",
        )

    assert transport.ensure_calls == []


def test_materializer_rejects_archive_links_without_partial_destination(tmp_path):
    _, source, suite, cases = _contracts(tmp_path)
    transport = FakeGitArchiveTransport(_tar_payload({}, symlink=True))
    destination = tmp_path / "sources"

    with pytest.raises(IntegrityError, match="unsupported entry type"):
        RetrievalSourceMaterializer(transport).materialize(
            source,
            suite,
            cases,
            destination=destination,
            cache_root=tmp_path / "cache",
        )

    assert not destination.exists()


def test_source_manifest_accepts_only_public_github_https_urls(tmp_path):
    _, source, _, _ = _contracts(tmp_path)
    raw = source.model_dump(mode="json")
    raw["cases"][0]["repository_url"] = "https://token@example.invalid/private.git"

    with pytest.raises(ValidationError, match="repository_url"):
        RetrievalSourceManifest.model_validate(raw)
