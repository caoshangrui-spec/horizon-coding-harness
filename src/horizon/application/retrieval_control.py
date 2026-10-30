from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from horizon.domain.common import canonical_json
from horizon.domain.model import ModelMessage
from horizon.domain.retrieval import EvidencePack

QUERY_TERM = re.compile(r"[A-Za-z0-9]+")
STRUCTURED_TERM = re.compile(r"[A-Za-z][A-Za-z0-9_./\\-]*")
CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
MIN_SHARED_QUERY_TERMS = 3
MIN_QUERY_CONTAINMENT = 0.75
NEIGHBOR_CONTEXT_BEFORE = 20
NEIGHBOR_CONTEXT_AFTER = 80
MAX_READ_LINES = 400
RETRIEVAL_QUERY_POLICY = "RetrievalQueryPolicy"

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
class _SettledRetrieval:
    arguments: dict[str, Any]
    evidence: EvidencePack


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
    proposed: dict[str, dict[str, Any]] = {}
    latest: _SettledRetrieval | None = None
    for message in messages:
        if message.role == "assistant":
            for call in message.tool_calls:
                if call.function.name == "retrieve_code":
                    proposed[call.id] = call.function.arguments
            continue
        if message.role != "tool" or message.tool_call_id not in proposed:
            continue
        try:
            evidence = EvidencePack.model_validate_json(message.content)
        except ValidationError:
            continue
        if evidence.workspace_revision != workspace_revision or not evidence.chunks:
            continue
        latest = _SettledRetrieval(
            arguments=proposed[message.tool_call_id],
            evidence=evidence,
        )
    return latest


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
