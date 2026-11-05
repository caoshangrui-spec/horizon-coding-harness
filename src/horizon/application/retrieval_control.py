from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from horizon.application.code_evidence import READ_RANGE_HEADER, source_excerpt
from horizon.domain.common import canonical_json
from horizon.domain.model import ModelMessage
from horizon.domain.retrieval import EvidenceChunk, EvidencePack

QUERY_TERM = re.compile(r"[A-Za-z0-9]+")
STRUCTURED_TERM = re.compile(r"[A-Za-z][A-Za-z0-9_./\\-]*")
CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
MIN_SHARED_QUERY_TERMS = 3
MIN_QUERY_CONTAINMENT = 0.75
NEIGHBOR_CONTEXT_BEFORE = 20
NEIGHBOR_CONTEXT_AFTER = 80
MAX_READ_LINES = 400
RETRIEVAL_QUERY_POLICY = "RetrievalQueryPolicy"
EVIDENCE_TRANSITION_POLICY = "EvidenceTransitionPolicy"
MIN_IDENTICAL_TARGET_READS = 2
MAX_TRANSITION_SNIPPET_CHARS = 240

# A shorter paraphrase that adds only one of these words does not add repository evidence.
# A concrete symbol, path, or domain term still bypasses the guard.
LOW_SIGNAL_REFINEMENT_TERMS = frozenset(
    {
        "code",
        "find",
        "fix",
        "function",
        "implementation",
        "locate",
        "method",
        "search",
        "use",
    }
)


@dataclass(frozen=True)
class RetrievalReuseHint:
    previous_query: str
    current_query: str
    shared_term_count: int
    current_term_count: int
    containment: float
    path: str
    start_line: int
    end_line: int
    suggested_start_line: int
    suggested_end_line: int

    @property
    def detail(self) -> str:
        read = {
            "path": self.path,
            "start_line": self.suggested_start_line,
            "end_line": self.suggested_end_line,
        }
        return (
            "Near-duplicate retrieve_code was not dispatched. Next call read_file "
            f"{canonical_json(read)}. Reuse settled rank-1 {self.path}:{self.start_line}-"
            f"{self.end_line}; search again only with a new symbol/path/domain term or larger "
            f"max_chunks (shared_terms={self.shared_term_count}, "
            f"shorter_query_containment={self.containment:.3f})."
        )


@dataclass(frozen=True)
class EvidenceTransitionHint:
    path: str
    query: str
    start_line: int
    end_line: int
    identical_read_count: int
    snippet: str

    @property
    def detail(self) -> str:
        evidence = canonical_json(
            {
                "path": self.path,
                "start_line": self.start_line,
                "end_line": self.end_line,
                "snippet": self.snippet,
            }
        )
        return (
            f"Edit-ready repeated-read evidence {evidence}. Same-revision rank-1 evidence "
            "was followed by "
            f"{self.identical_read_count} read_file calls that returned identical target "
            f"content for {self.path}. Further read_file/retrieve_code was not dispatched; "
            "do not reacquire it with different arguments. Next choose "
            f"an allowed write tool for {self.path}, then submit for protected validation. "
            "If repository evidence contradicts the WorkItem, revise the Plan; if no safe exact "
            "edit can be formed, stop rather than reacquire the same evidence."
        )


@dataclass(frozen=True)
class _SettledRetrieval:
    arguments: dict[str, Any]
    evidence: EvidencePack


@dataclass(frozen=True)
class _SettledToolTurn:
    order: int
    name: str
    arguments: dict[str, Any]
    content: str


def _settled_tool_turns(messages: Sequence[ModelMessage]) -> Iterator[_SettledToolTurn]:
    proposed: dict[str, tuple[str, dict[str, Any]]] = {}
    order = 0
    for message in messages:
        if message.role == "assistant":
            for call in message.tool_calls:
                proposed[call.id] = (call.function.name, call.function.arguments)
            continue
        if message.role != "tool" or message.tool_call_id not in proposed:
            continue
        name, arguments = proposed[message.tool_call_id]
        order += 1
        yield _SettledToolTurn(
            order=order,
            name=name,
            arguments=arguments,
            content=message.content or "",
        )


def _query_terms(query: str) -> frozenset[str]:
    return frozenset(term.casefold() for term in QUERY_TERM.findall(query) if len(term) > 1)


def _structured_terms(query: str) -> frozenset[str]:
    terms: set[str] = set()
    for match in STRUCTURED_TERM.finditer(query):
        value = match.group()
        if (
            "_" in value
            or "/" in value
            or "\\" in value
            or "." in value
            or CAMEL_BOUNDARY.search(value) is not None
        ):
            terms.add(value.casefold())
    return frozenset(terms)


def _max_chunks(arguments: dict[str, Any]) -> int | None:
    value = arguments.get("max_chunks", 1)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 8:
        return None
    return value


def _latest_settled_retrieval(
    messages: Sequence[ModelMessage],
    workspace_revision: str,
) -> _SettledRetrieval | None:
    latest: _SettledRetrieval | None = None
    for turn in _settled_tool_turns(messages):
        if turn.name != "retrieve_code":
            continue
        try:
            evidence = EvidencePack.model_validate_json(turn.content)
        except ValidationError:
            continue
        if evidence.workspace_revision != workspace_revision or not evidence.chunks:
            continue
        latest = _SettledRetrieval(
            arguments=turn.arguments,
            evidence=evidence,
        )
    return latest


def _explicit_query_paths(query: str) -> frozenset[str]:
    return frozenset(
        term.replace("\\", "/") for term in _structured_terms(query) if "/" in term or "\\" in term
    )


def _normalized_text(content: str) -> str:
    return content.replace("\r\n", "\n").replace("\r", "\n")


def _read_excerpt(
    turn: _SettledToolTurn,
    *,
    fallback_start_line: int,
    fallback_end_line: int,
    terms: tuple[str, ...] = (),
    chunks: tuple[EvidenceChunk, ...] = (),
) -> tuple[int, int, str]:
    content = _normalized_text(turn.content)
    selected = source_excerpt(content, MAX_TRANSITION_SNIPPET_CHARS, terms, chunks)
    if selected is not None:
        return selected
    match = READ_RANGE_HEADER.match(content)
    if match is not None:
        body = content[match.end() :]
        return (
            int(match.group("start")),
            int(match.group("end")),
            body[:MAX_TRANSITION_SNIPPET_CHARS],
        )
    start_line = turn.arguments.get("start_line", fallback_start_line)
    end_line = turn.arguments.get("end_line", fallback_end_line)
    if isinstance(start_line, bool) or not isinstance(start_line, int) or start_line < 1:
        start_line = fallback_start_line
    if isinstance(end_line, bool) or not isinstance(end_line, int) or end_line < start_line:
        end_line = fallback_end_line
    return start_line, end_line, content[:MAX_TRANSITION_SNIPPET_CHARS]


def evidence_is_visible(snippet: str, messages: Sequence[ModelMessage]) -> bool:
    """Check the actual request, including JSON-encoded memory, not just artifact refs."""
    if not snippet.strip():
        return False
    normalized = _normalized_text(snippet)
    escaped = canonical_json(normalized)[1:-1]
    return any(
        normalized in _normalized_text(message.content or "") or escaped in (message.content or "")
        for message in messages
        if message.role in {"system", "tool"}
    )


def read_evidence_missing(
    messages: Sequence[ModelMessage],
    visible_messages: Sequence[ModelMessage],
    arguments: dict[str, Any],
) -> bool:
    """A soft duplicate guard may not withhold a receipt absent from this request."""
    prior = next(
        (
            turn
            for turn in reversed(list(_settled_tool_turns(messages)))
            if turn.name == "read_file"
            and turn.arguments == arguments
            and READ_RANGE_HEADER.match(_normalized_text(turn.content)) is not None
        ),
        None,
    )
    return prior is not None and not evidence_is_visible(prior.content, visible_messages)


def find_evidence_reacquisition(
    messages: Sequence[ModelMessage],
    current_tool_name: str,
    current_arguments: dict[str, Any],
    workspace_revision: str,
    *,
    visible_messages: Sequence[ModelMessage] | None = None,
) -> EvidenceTransitionHint | None:
    """Block a third evidence acquisition after exact duplicate target reads.

    This is intentionally narrower than semantic no-progress detection. It requires an ok,
    same-revision rank-1 EvidencePack and two later reads of that exact path whose returned
    content is byte-for-byte identical and contains the retrieved snippet.
    """

    if current_tool_name not in {"read_file", "retrieve_code"}:
        return None

    retrievals: list[tuple[int, dict[str, Any], EvidencePack]] = []
    reads: list[_SettledToolTurn] = []
    for turn in _settled_tool_turns(messages):
        if turn.name in {"replace_text", "apply_patch", "create_file"}:
            # A read has no embedded revision. Never carry it across a possible write,
            # even when that write later reported an error.
            retrievals.clear()
            reads.clear()
            continue
        if turn.name == "retrieve_code":
            try:
                evidence = EvidencePack.model_validate_json(turn.content)
            except ValidationError:
                continue
            if (
                evidence.workspace_revision == workspace_revision
                and evidence.status == "ok"
                and evidence.chunks
            ):
                retrievals.append((turn.order, turn.arguments, evidence))
            continue
        if turn.name == "read_file":
            reads.append(turn)

    if not retrievals:
        return None
    target_paths = {evidence.chunks[0].path for _, _, evidence in retrievals}
    if len(target_paths) != 1:
        return None
    target_path = next(iter(target_paths))

    if current_tool_name == "read_file":
        if current_arguments.get("path") != target_path:
            return None
    else:
        current_query = current_arguments.get("query")
        current_max_chunks = _max_chunks(current_arguments)
        if not isinstance(current_query, str) or not current_query or current_max_chunks is None:
            return None
        latest_max_chunks = _max_chunks(retrievals[-1][1])
        if latest_max_chunks is None or current_max_chunks > latest_max_chunks:
            return None
        explicit_paths = _explicit_query_paths(current_query)
        normalized_target = target_path.casefold().replace("\\", "/")
        if any(path != normalized_target for path in explicit_paths):
            return None

    first_retrieval_order = retrievals[0][0]
    snippets = tuple(
        evidence.chunks[0].snippet
        for _, _, evidence in retrievals
        if evidence.chunks[0].path == target_path
    )
    matching_reads = [
        turn
        for turn in reads
        if turn.order > first_retrieval_order
        and turn.arguments.get("path") == target_path
        and any(_normalized_text(snippet) in _normalized_text(turn.content) for snippet in snippets)
    ]
    if not matching_reads:
        return None
    content_counts = Counter(turn.content for turn in matching_reads)
    repeated_content, identical_read_count = content_counts.most_common(1)[0]
    if identical_read_count < MIN_IDENTICAL_TARGET_READS:
        return None

    latest_evidence = retrievals[-1][2]
    chunk = latest_evidence.chunks[0]
    repeated_read = next(
        turn for turn in reversed(matching_reads) if turn.content == repeated_content
    )
    if current_tool_name == "read_file":
        header = READ_RANGE_HEADER.match(_normalized_text(repeated_read.content))
        if header is not None:
            requested_start = current_arguments.get("start_line")
            requested_end = current_arguments.get("end_line")
            if requested_start is None and requested_end is None:
                return None  # A whole-file request can extend the previous bounded read.
            if requested_start is None and isinstance(requested_end, int):
                requested_start = max(1, requested_end - 99)
            if requested_end is None and isinstance(requested_start, int):
                requested_end = requested_start + 99
            if (
                not isinstance(requested_start, int)
                or isinstance(requested_start, bool)
                or not isinstance(requested_end, int)
                or isinstance(requested_end, bool)
                or requested_start < int(header.group("start"))
                or requested_end > int(header.group("end"))
            ):
                return None
    start_line, end_line, snippet = _read_excerpt(
        repeated_read,
        fallback_start_line=chunk.start_line,
        fallback_end_line=chunk.end_line,
        terms=tuple(
            sorted(_structured_terms(latest_evidence.query) | _query_terms(latest_evidence.query))
        ),
        chunks=tuple(chunk for _, _, pack in retrievals for chunk in pack.chunks),
    )
    if not evidence_is_visible(snippet, messages if visible_messages is None else visible_messages):
        return None
    return EvidenceTransitionHint(
        path=target_path,
        query=latest_evidence.query,
        start_line=start_line,
        end_line=end_line,
        identical_read_count=identical_read_count,
        snippet=snippet,
    )


def find_redundant_retrieval(
    messages: Sequence[ModelMessage],
    current_arguments: dict[str, Any],
    workspace_revision: str,
) -> RetrievalReuseHint | None:
    """Return a revision-bound reuse hint for a synonym-only retrieval repeat."""

    current_query = current_arguments.get("query")
    current_max_chunks = _max_chunks(current_arguments)
    if not isinstance(current_query, str) or not current_query or current_max_chunks is None:
        return None
    previous = _latest_settled_retrieval(messages, workspace_revision)
    if previous is None:
        return None
    previous_max_chunks = _max_chunks(previous.arguments)
    if previous_max_chunks is None or current_max_chunks > previous_max_chunks:
        return None

    previous_terms = _query_terms(previous.evidence.query)
    current_terms = _query_terms(current_query)
    if not previous_terms or not current_terms:
        return None
    shared = previous_terms.intersection(current_terms)
    containment = len(shared) / min(len(previous_terms), len(current_terms))
    if len(shared) < MIN_SHARED_QUERY_TERMS or containment < MIN_QUERY_CONTAINMENT:
        return None

    if _structured_terms(current_query) - _structured_terms(previous.evidence.query):
        return None
    new_terms = current_terms - previous_terms
    if new_terms - LOW_SIGNAL_REFINEMENT_TERMS:
        return None

    chunk = previous.evidence.chunks[0]
    suggested_start = max(1, chunk.start_line - NEIGHBOR_CONTEXT_BEFORE)
    suggested_end = min(
        suggested_start + MAX_READ_LINES - 1,
        chunk.end_line + NEIGHBOR_CONTEXT_AFTER,
    )
    return RetrievalReuseHint(
        previous_query=previous.evidence.query,
        current_query=current_query,
        shared_term_count=len(shared),
        current_term_count=len(current_terms),
        containment=containment,
        path=chunk.path,
        start_line=chunk.start_line,
        end_line=chunk.end_line,
        suggested_start_line=suggested_start,
        suggested_end_line=suggested_end,
    )
