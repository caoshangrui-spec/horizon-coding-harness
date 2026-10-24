from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import tarfile
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Protocol
from uuid import uuid4

from horizon.domain.common import canonical_json, digest
from horizon.domain.errors import IntegrityError, PolicyDenied
from horizon.domain.evaluation import (
    RetrievalEvalManifest,
    RetrievalEvalSuiteManifest,
    RetrievalSourceCaseReceipt,
    RetrievalSourceManifest,
    RetrievalSourceReceipt,
)
from horizon.domain.task import relative_pattern


class GitArchiveTransport(Protocol):
    def ensure_commits(
        self,
        repository_url: str,
        commits: tuple[str, ...],
        cache_root: Path,
    ) -> tuple[Path, bool]: ...

    def archive(self, repository: Path, commit: str, paths: tuple[str, ...]) -> bytes: ...


class SubprocessGitArchiveTransport:
    """Fetch pinned public commits into a bare cache without checking out or running code."""

    def __init__(self, *, timeout_seconds: int = 180):
        self.timeout_seconds = timeout_seconds

    def _run(
        self,
        arguments: tuple[str, ...],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        environment = os.environ.copy()
        environment["GIT_TERMINAL_PROMPT"] = "0"
        try:
            result = subprocess.run(
                ("git", *arguments),
                check=False,
                capture_output=True,
                timeout=self.timeout_seconds,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise IntegrityError("Pinned Git source preparation timed out") from exc
        if check and result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()[-2000:]
            raise IntegrityError(
                f"Pinned Git source preparation failed with exit {result.returncode}: {detail}"
            )
        return result

    def ensure_commits(
        self,
        repository_url: str,
        commits: tuple[str, ...],
        cache_root: Path,
    ) -> tuple[Path, bool]:
        repository_key = hashlib.sha256(repository_url.encode("utf-8")).hexdigest()
        repository = cache_root / f"{repository_key}.git"
        if not repository.exists():
            self._run(("init", "--bare", "--quiet", str(repository)))
            self._run(("-C", str(repository), "remote", "add", "origin", repository_url))
        elif not repository.is_dir():
            raise IntegrityError("Git source cache entry is not a directory")

        bare = self._run(("-C", str(repository), "rev-parse", "--is-bare-repository"))
        if bare.stdout.decode("ascii", errors="replace").strip() != "true":
            raise IntegrityError("Git source cache entry is not a bare repository")
        remote = self._run(("-C", str(repository), "remote", "get-url", "origin"))
        if remote.stdout.decode("utf-8", errors="replace").strip() != repository_url:
            raise IntegrityError("Git source cache remote does not match its pinned manifest")

        unique_commits = tuple(dict.fromkeys(commits))
        missing = tuple(
            commit
            for commit in unique_commits
            if self._run(
                ("-C", str(repository), "cat-file", "-e", f"{commit}^{{commit}}"),
                check=False,
            ).returncode
            != 0
        )
        if missing:
            self._run(
                (
                    "-C",
                    str(repository),
                    "fetch",
                    "--quiet",
                    "--no-tags",
                    "--depth=1",
                    "origin",
                    *missing,
                )
            )
        for commit in unique_commits:
            resolved = (
                self._run(("-C", str(repository), "rev-parse", f"{commit}^{{commit}}"))
                .stdout.decode("ascii", errors="replace")
                .strip()
            )
            if resolved != commit:
                raise IntegrityError("Fetched Git commit did not resolve to the pinned object")
        return repository, bool(missing)

    def archive(self, repository: Path, commit: str, paths: tuple[str, ...]) -> bytes:
        return self._run(
            ("-C", str(repository), "archive", "--format=tar", commit, "--", *paths)
        ).stdout


def source_tree_evidence(root: Path) -> tuple[int, int, str]:
    """Return portable content evidence; timestamps and platform file modes are excluded."""
    entries: list[dict[str, str | int]] = []
    byte_count = 0
    for path in sorted(root.rglob("*"), key=lambda candidate: candidate.as_posix().casefold()):
        if path.is_symlink():
            raise IntegrityError("Materialized source trees cannot contain symbolic links")
        if not path.is_file():
            continue
        payload = path.read_bytes()
        byte_count += len(payload)
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "byte_count": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    if not entries:
        raise IntegrityError("Materialized source tree is empty")
    return len(entries), byte_count, digest(entries)


def _extract_archive(payload: bytes, destination: Path) -> None:
    destination.mkdir(parents=True)
    seen: set[str] = set()
    try:
        archive = tarfile.open(fileobj=io.BytesIO(payload), mode="r:")
    except tarfile.TarError as exc:
        raise IntegrityError("Git returned an invalid source archive") from exc
    with archive:
        for member in archive.getmembers():
            name = member.name.rstrip("/")
            if not name:
                continue
            try:
                relative_pattern(name)
            except ValueError as exc:
                raise IntegrityError("Git source archive contains an unsafe path") from exc
            if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                raise IntegrityError("Git source archive contains an unsupported entry type")
            normalized = PurePosixPath(name).as_posix()
            if normalized in seen:
                raise IntegrityError("Git source archive contains a duplicate path")
            seen.add(normalized)
            target = destination.joinpath(*PurePosixPath(normalized).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise IntegrityError("Git source archive file has no payload")
            target.write_bytes(source.read())


def _path_is_within(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == root or path.startswith(f"{root}/") for root in roots)


def validate_source_manifest(
    source_manifest: RetrievalSourceManifest,
    evaluation_suite: RetrievalEvalSuiteManifest,
    case_manifests: dict[str, RetrievalEvalManifest],
) -> None:
    if source_manifest.suite_id != evaluation_suite.suite_id:
        raise IntegrityError("Source and evaluation suite IDs do not match")
    if source_manifest.partition != evaluation_suite.partition:
        raise IntegrityError("Source and evaluation suite partitions do not match")
    source_ids = tuple(case.case_id for case in source_manifest.cases)
    evaluation_ids = tuple(case.case_id for case in evaluation_suite.cases)
    if source_ids != evaluation_ids:
        raise IntegrityError("Source cases must exactly preserve evaluation suite order")

    for source_spec, evaluation_spec in zip(
        source_manifest.cases, evaluation_suite.cases, strict=True
    ):
        if source_spec.buggy_commit != evaluation_spec.buggy_commit:
            raise IntegrityError("Source and evaluation commits do not match")
        case_manifest = case_manifests.get(source_spec.case_id)
        if case_manifest is None:
            raise IntegrityError("Source preparation is missing an evaluation case manifest")
        if len(case_manifest.cases) != 1:
            raise IntegrityError("Source preparation requires one evaluation case per manifest")
        case = case_manifest.cases[0]
        if case.case_id != source_spec.case_id:
            raise IntegrityError("Source and evaluation case IDs do not match")
        if any(
            not _path_is_within(expected_path, source_spec.archive_paths)
            for expected_path in case.expected_paths
        ):
            raise IntegrityError("A gold path is outside its pinned source archive paths")


class RetrievalSourceMaterializer:
    def __init__(self, transport: GitArchiveTransport | None = None):
        self.transport = transport or SubprocessGitArchiveTransport()

    def materialize(
        self,
        source_manifest: RetrievalSourceManifest,
        evaluation_suite: RetrievalEvalSuiteManifest,
        case_manifests: dict[str, RetrievalEvalManifest],
        *,
        destination: Path,
        cache_root: Path,
        allow_holdout: bool = False,
    ) -> RetrievalSourceReceipt:
        validate_source_manifest(source_manifest, evaluation_suite, case_manifests)
        if source_manifest.partition == "holdout" and not allow_holdout:
            raise PolicyDenied("Holdout source materialization requires --allow-holdout")

        destination = destination.resolve()
        cache_root = cache_root.resolve()
        if destination.exists():
            raise IntegrityError("Source materialization destination must be new")
        if (
            destination == cache_root
            or destination.is_relative_to(cache_root)
            or cache_root.is_relative_to(destination)
        ):
            raise IntegrityError("Source destination and Git cache must not contain each other")
        destination.parent.mkdir(parents=True, exist_ok=True)
        cache_root.mkdir(parents=True, exist_ok=True)

        repositories: dict[str, Path] = {}
        network_called = False
        commits_by_repository: dict[str, list[str]] = defaultdict(list)
        for case in source_manifest.cases:
            commits_by_repository[case.repository_url].append(case.buggy_commit)
        for repository_url, commits in commits_by_repository.items():
            repository, fetched = self.transport.ensure_commits(
                repository_url,
                tuple(commits),
                cache_root,
            )
            repositories[repository_url] = repository
            network_called = network_called or fetched

        staging = destination.parent / f".{destination.name}.tmp-{uuid4().hex}"
        receipts: list[RetrievalSourceCaseReceipt] = []
        try:
            staging.mkdir()
            for spec in source_manifest.cases:
                case_destination = staging / spec.case_id
                payload = self.transport.archive(
                    repositories[spec.repository_url],
                    spec.buggy_commit,
                    spec.archive_paths,
                )
                _extract_archive(payload, case_destination)
                file_count, byte_count, tree_digest = source_tree_evidence(case_destination)
                if (
                    file_count != spec.expected_file_count
                    or byte_count != spec.expected_byte_count
                    or tree_digest != spec.expected_tree_digest
                ):
                    raise IntegrityError(
                        f"Materialized source evidence mismatch for {spec.case_id}"
                    )
                receipts.append(
                    RetrievalSourceCaseReceipt(
                        case_id=spec.case_id,
                        repository_url=spec.repository_url,
                        buggy_commit=spec.buggy_commit,
                        archive_paths=spec.archive_paths,
                        file_count=file_count,
                        byte_count=byte_count,
                        tree_digest=tree_digest,
                    )
                )

            receipt = RetrievalSourceReceipt(
                suite_id=source_manifest.suite_id,
                source_manifest_digest=source_manifest.sha256,
                evaluation_suite_digest=evaluation_suite.sha256,
                partition=source_manifest.partition,
                case_count=len(receipts),
                network_called=network_called,
                cases=tuple(receipts),
            )
            (staging / "materialization-receipt.json").write_text(
                canonical_json(receipt),
                encoding="utf-8",
            )
            staging.rename(destination)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise
        return receipt
