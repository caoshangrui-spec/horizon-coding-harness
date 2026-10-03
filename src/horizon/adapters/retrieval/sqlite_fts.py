from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from horizon.adapters.persistence.artifacts import reject_link
from horizon.adapters.workspace.snapshot import FileEntry, SnapshotManager
from horizon.domain.common import digest
from horizon.domain.errors import IntegrityError
from horizon.domain.retrieval import EvidenceChunk, EvidencePack

SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE retrieval_indexes(
    index_key TEXT PRIMARY KEY,
    workspace_revision TEXT NOT NULL,
    source_manifest_ref TEXT NOT NULL,
    scope_hash TEXT NOT NULL,
    indexed_file_count INTEGER NOT NULL,
    skipped_file_count INTEGER NOT NULL,
    degradation_reasons_json TEXT NOT NULL
);
CREATE VIRTUAL TABLE code_chunks USING fts5(
    index_key UNINDEXED,
    path,
    start_line UNINDEXED,
    end_line UNINDEXED,
    content_hash UNINDEXED,
    content,
    tokenize='unicode61'
);
PRAGMA user_version=1;
COMMIT;
"""

MAX_INDEX_FILES = 2_000
MAX_INDEX_BYTES = 16 * 1024 * 1024
MAX_CACHED_INDEXES = 16
MAX_FILE_BYTES = 512 * 1024
MAX_LINE_CHARS = 24_000
CHUNK_LINES = 40
CHUNK_OVERLAP = 5
MAX_SNIPPET_CHARS = 1_200
MAX_PRIORITY_CANDIDATES = 256
INDEX_ALGORITHM_VERSION = 5
TERM_PATTERN = re.compile(r"[^\W]+", re.UNICODE)
IDENTIFIER_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
DEFINITION_PATTERN = re.compile(
    r"^\s*(?:async\s+def|def|class)\s+([^\W\d]\w*)",
    re.MULTILINE | re.UNICODE,
)


def _permitted(path: str, allowed_paths: tuple[str, ...], denied_paths: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in allowed_paths) and not any(
        fnmatch.fnmatchcase(path, pattern) for pattern in denied_paths
    )


def _term_sets(query: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    raw_values = TERM_PATTERN.findall(query)
    candidates = [value.casefold() for value in raw_values]
    identifier_parts: list[str] = []
    for value in raw_values:
        for segment in value.split("_"):
            identifier_parts.extend(
                part.casefold() for part in IDENTIFIER_BOUNDARY.split(segment) if part
            )
    candidates.extend(identifier_parts)
    joined_identifier = ""
    if len(identifier_parts) > 1 and all(
        part.isascii() and part.isalnum() for part in identifier_parts
    ):
        joined_identifier = "".join(identifier_parts)[:80]
        candidates.append(joined_identifier)

    terms: list[str] = []
    for value in candidates:
        value = value[:80]
        if value and value not in terms:
            terms.append(value)
        if len(terms) == 20:
            break
    normalized = tuple(terms)
    priority = (joined_identifier,) if joined_identifier and joined_identifier in normalized else ()
    return normalized, priority


def _terms(query: str) -> tuple[str, ...]:
    return _term_sets(query)[0]


def _fts_query(terms: tuple[str, ...]) -> str:
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)


def _defines_identifier(content: str, identifiers: tuple[str, ...]) -> bool:
    if not identifiers:
        return False
    normalized = set(identifiers)
    return any(
        match.group(1).replace("_", "").casefold() in normalized
        for match in DEFINITION_PATTERN.finditer(content)
    )


def _file_chunks(content: str):
    lines = content.splitlines()
    if not lines and content:
        lines = [content]
    step = CHUNK_LINES - CHUNK_OVERLAP
    offset = 0
    while offset < len(lines):
        selected = lines[offset : offset + CHUNK_LINES]
        if not selected:
            break
        text = "\n".join(selected)
        if text.strip():
            yield offset + 1, offset + len(selected), text
        if offset + len(selected) == len(lines):
            break
        offset += step


class SQLiteCodeRetriever:
    """A derived FTS5 cache keyed by immutable workspace manifest and path scope."""

    def __init__(self, path: str | Path, snapshots: SnapshotManager):
        self.path = Path(path)
        self.snapshots = snapshots
        for part in (self.path.absolute(), *self.path.absolute().parents):
            reject_link(part)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fts5_available = self._initialize()

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    def _initialize(self) -> bool:
        try:
            with self._connection() as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in {0, 1}:
                    raise IntegrityError(f"Unsupported retrieval index schema version: {version}")
                if version == 0:
                    db.executescript(SCHEMA)
                db.execute("SELECT count(*) FROM code_chunks").fetchone()
                return True
        except sqlite3.OperationalError as exc:
            if "fts5" not in str(exc).casefold():
                raise
            return False

    def _scope(
        self,
        source_manifest_ref: str,
        workspace_revision: str,
        allowed_paths: tuple[str, ...],
        denied_paths: tuple[str, ...],
    ) -> tuple[str, str]:
        scope_hash = digest(
            {
                "allowed_paths": allowed_paths,
                "denied_paths": denied_paths,
                "index_algorithm_version": INDEX_ALGORITHM_VERSION,
                "chunk_lines": CHUNK_LINES,
                "chunk_overlap": CHUNK_OVERLAP,
                "max_file_bytes": MAX_FILE_BYTES,
                "max_line_chars": MAX_LINE_CHARS,
                "max_index_bytes": MAX_INDEX_BYTES,
                "max_index_files": MAX_INDEX_FILES,
            }
        )
        index_key = digest(
            {
                "source_manifest_ref": source_manifest_ref,
                "workspace_revision": workspace_revision,
                "scope_hash": scope_hash,
            }
        )
        return scope_hash, index_key

    def _eligible_entries(
        self,
        entries: tuple[FileEntry, ...],
        allowed_paths: tuple[str, ...],
        denied_paths: tuple[str, ...],
    ) -> tuple[FileEntry, ...]:
        return tuple(
            entry for entry in entries if _permitted(entry.path, allowed_paths, denied_paths)
        )

    def _materialize(
        self,
        entries: tuple[FileEntry, ...],
    ) -> tuple[list[tuple[str, int, int, str, str]], int, int, tuple[str, ...]]:
        rows: list[tuple[str, int, int, str, str]] = []
        indexed_files = 0
        skipped_files = 0
        indexed_bytes = 0
        reasons: list[str] = []

        for position, entry in enumerate(entries):
            if (
                indexed_files >= MAX_INDEX_FILES
                or indexed_bytes + entry.size_bytes > MAX_INDEX_BYTES
            ):
                skipped_files += len(entries) - position
                reasons.append("index_budget_exceeded")
                break
            if entry.size_bytes > MAX_FILE_BYTES:
                skipped_files += 1
                reasons.append("oversized_file_skipped")
                continue
            raw = self.snapshots.artifacts.read(entry.sha256, max_bytes=MAX_FILE_BYTES)
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                skipped_files += 1
                reasons.append("non_utf8_file_skipped")
                continue
            if any(len(line) > MAX_LINE_CHARS for line in content.splitlines()):
                skipped_files += 1
                reasons.append("oversized_line_skipped")
                continue
            for start_line, end_line, chunk in _file_chunks(content):
                rows.append(
                    (
                        entry.path,
                        start_line,
                        end_line,
                        hashlib.sha256(chunk.encode("utf-8")).hexdigest(),
                        chunk,
                    )
                )
            indexed_files += 1
            indexed_bytes += entry.size_bytes

        return rows, indexed_files, skipped_files, tuple(dict.fromkeys(reasons))

    def _ensure_index(
        self,
        *,
        index_key: str,
        workspace_revision: str,
        source_manifest_ref: str,
        scope_hash: str,
        entries: tuple[FileEntry, ...],
    ) -> tuple[int, int, tuple[str, ...]]:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                state = db.execute(
                    "SELECT * FROM retrieval_indexes WHERE index_key=?",
                    (index_key,),
                ).fetchone()
                if state is None:
                    rows, indexed_files, skipped_files, reasons = self._materialize(entries)
                    db.executemany(
                        "INSERT INTO code_chunks "
                        "(index_key,path,start_line,end_line,content_hash,content) "
                        "VALUES (?,?,?,?,?,?)",
                        ((index_key, *row) for row in rows),
                    )
                    db.execute(
                        "INSERT INTO retrieval_indexes VALUES (?,?,?,?,?,?,?)",
                        (
                            index_key,
                            workspace_revision,
                            source_manifest_ref,
                            scope_hash,
                            indexed_files,
                            skipped_files,
                            json.dumps(reasons, separators=(",", ":")),
                        ),
                    )
                    expired = db.execute(
                        "SELECT index_key FROM retrieval_indexes ORDER BY rowid DESC "
                        "LIMIT -1 OFFSET ?",
                        (MAX_CACHED_INDEXES,),
                    ).fetchall()
                    for expired_row in expired:
                        db.execute(
                            "DELETE FROM code_chunks WHERE index_key=?",
                            (expired_row["index_key"],),
                        )
                        db.execute(
                            "DELETE FROM retrieval_indexes WHERE index_key=?",
                            (expired_row["index_key"],),
                        )
                    db.commit()
                    return indexed_files, skipped_files, reasons
                expected = (workspace_revision, source_manifest_ref, scope_hash)
                actual = tuple(
                    state[key]
                    for key in ("workspace_revision", "source_manifest_ref", "scope_hash")
                )
                if actual != expected:
                    raise IntegrityError("Retrieval index key identifies different source data")
                db.commit()
                return (
                    state["indexed_file_count"],
                    state["skipped_file_count"],
                    tuple(json.loads(state["degradation_reasons_json"])),
                )
            except BaseException:
                db.rollback()
                raise

    def _fts_results(
        self,
        index_key: str,
        terms: tuple[str, ...],
        priority_terms: tuple[str, ...],
        max_chunks: int,
    ) -> list[sqlite3.Row]:
        if not terms:
            return []
        results: list[sqlite3.Row] = []
        seen: set[tuple[str, int, int, str]] = set()
        with self._connection() as db:
            for query_terms in (priority_terms, terms):
                if not query_terms:
                    continue
                candidate_limit = MAX_PRIORITY_CANDIDATES if priority_terms else max_chunks
                rows = db.execute(
                    "SELECT path,start_line,end_line,content_hash,content,"
                    "bm25(code_chunks) AS score FROM code_chunks "
                    "WHERE code_chunks MATCH ? AND index_key=? "
                    "ORDER BY score,path,CAST(start_line AS INTEGER) LIMIT ?",
                    (_fts_query(query_terms), index_key, candidate_limit),
                ).fetchall()
                if priority_terms:
                    rows.sort(
                        key=lambda row: (
                            -int(_defines_identifier(row["content"], priority_terms)),
                            row["score"],
                            row["path"],
                            int(row["start_line"]),
                        )
                    )
                for row in rows:
                    key = (
                        row["path"],
                        int(row["start_line"]),
                        int(row["end_line"]),
                        row["content_hash"],
                    )
                    if key not in seen:
                        seen.add(key)
                        results.append(row)
                    if len(results) == max_chunks:
                        return results
        return results

    @staticmethod
    def _chunks_from_rows(rows) -> tuple[EvidenceChunk, ...]:
        chunks: list[EvidenceChunk] = []
        for rank, row in enumerate(rows, start=1):
            content = row["content"]
            snippet = content[:MAX_SNIPPET_CHARS]
            chunks.append(
                EvidenceChunk(
                    rank=rank,
                    path=row["path"],
                    start_line=int(row["start_line"]),
                    end_line=int(row["end_line"]),
                    content_hash=row["content_hash"],
                    snippet=snippet,
                    truncated=len(snippet) != len(content),
                )
            )
        return tuple(chunks)

    def _validate_result_rows(
        self,
        rows,
        entries: tuple[FileEntry, ...],
    ) -> None:
        by_path = {entry.path: entry for entry in entries}
        for row in rows:
            entry = by_path.get(row["path"])
            if entry is None:
                raise IntegrityError("Retrieval index returned an out-of-scope path")
            raw = self.snapshots.artifacts.read(entry.sha256, max_bytes=MAX_FILE_BYTES)
            try:
                lines = raw.decode("utf-8").splitlines()
            except UnicodeDecodeError as exc:
                raise IntegrityError("Retrieval index references non-UTF-8 evidence") from exc
            start = int(row["start_line"])
            end = int(row["end_line"])
            if start < 1 or end < start or end > len(lines):
                raise IntegrityError("Retrieval index returned an invalid line range")
            content = "\n".join(lines[start - 1 : end])
            if (
                content != row["content"]
                or hashlib.sha256(content.encode("utf-8")).hexdigest() != row["content_hash"]
            ):
                raise IntegrityError("Retrieval evidence does not match the immutable source")

    def _scan_results(
        self,
        entries: tuple[FileEntry, ...],
        terms: tuple[str, ...],
        priority_terms: tuple[str, ...],
        max_chunks: int,
    ) -> tuple[tuple[EvidenceChunk, ...], int, int, tuple[str, ...]]:
        rows, indexed_files, skipped_files, reasons = self._materialize(entries)
        matches = []
        for path, start_line, end_line, content_hash, content in rows:
            haystack = f"{path}\n{content}".casefold()
            score = sum(haystack.count(term) for term in terms)
            if score:
                priority_score = sum(haystack.count(term) for term in priority_terms)
                definition_priority = _defines_identifier(content, priority_terms)
                matches.append(
                    {
                        "path": path,
                        "start_line": start_line,
                        "end_line": end_line,
                        "content_hash": content_hash,
                        "content": content,
                        "score": score,
                        "priority_score": priority_score,
                        "definition_priority": definition_priority,
                    }
                )
        matches.sort(
            key=lambda row: (
                -int(row["definition_priority"]),
                -int(row["priority_score"] > 0),
                -row["priority_score"],
                -row["score"],
                row["path"],
                row["start_line"],
            )
        )
        return (
            self._chunks_from_rows(matches[:max_chunks]),
            indexed_files,
            skipped_files,
            tuple(dict.fromkeys(("fts5_unavailable", *reasons))),
        )

    def retrieve(
        self,
        *,
        source_manifest_ref: str,
        workspace_revision: str,
        allowed_paths: tuple[str, ...],
        denied_paths: tuple[str, ...],
        query: str,
        max_chunks: int,
    ) -> EvidencePack:
        if not 1 <= max_chunks <= 8:
            raise ValueError("Code retrieval accepts between 1 and 8 chunks")
        manifest = self.snapshots.verify(source_manifest_ref)
        if manifest.workspace_revision != workspace_revision:
            raise IntegrityError("Retrieval manifest does not match the workspace revision")
        scope_hash, index_key = self._scope(
            source_manifest_ref,
            workspace_revision,
            allowed_paths,
            denied_paths,
        )
        entries = self._eligible_entries(manifest.files, allowed_paths, denied_paths)
        terms, priority_terms = _term_sets(query)

        if self.fts5_available:
            indexed_files, skipped_files, reasons = self._ensure_index(
                index_key=index_key,
                workspace_revision=workspace_revision,
                source_manifest_ref=source_manifest_ref,
                scope_hash=scope_hash,
                entries=entries,
            )
            result_rows = self._fts_results(
                index_key,
                terms,
                priority_terms,
                max_chunks,
            )
            self._validate_result_rows(result_rows, entries)
            chunks = self._chunks_from_rows(result_rows)
            backend = "sqlite_fts5"
        else:
            chunks, indexed_files, skipped_files, reasons = self._scan_results(
                entries,
                terms,
                priority_terms,
                max_chunks,
            )
            backend = "lexical_scan"

        if reasons:
            status = "degraded"
        elif chunks:
            status = "ok"
        else:
            status = "empty"
        return EvidencePack(
            query=query,
            normalized_terms=terms,
            workspace_revision=workspace_revision,
            source_manifest_ref=source_manifest_ref,
            scope_hash=scope_hash,
            index_key=index_key,
            backend=backend,
            status=status,
            degradation_reasons=reasons,
            indexed_file_count=indexed_files,
            skipped_file_count=skipped_files,
            chunks=chunks,
        )
