"""Bounded source selection; coordinates and syntax outrank lexical coincidences."""

from __future__ import annotations

import ast
import re
import textwrap

from horizon.domain.retrieval import EvidenceChunk

READ_RANGE_HEADER = re.compile(
    r"\AREAD_RANGE path=(?P<path>.+?) lines=(?P<start>[1-9][0-9]*)-"
    r"(?P<end>[1-9][0-9]*) total_lines=[1-9][0-9]*\n"
)
DEFINITION = re.compile(r"^(\s*)(?:async\s+)?(?:def|class)\s+(\w+)\b")


def source_excerpt(
    content: str,
    max_chars: int,
    terms: tuple[str, ...],
    chunks: tuple[EvidenceChunk, ...] = (),
) -> tuple[int, int, str] | None:
    """Select whole source lines, never manufacture or cut an edit preimage.

    Named scopes restrict competition to the named function/class. Otherwise verified
    retrieval coordinates restrict it. AST statements keep multiline calls together and
    exclude docstrings. Partial/non-Python reads use whole-line windows as a fallback.
    """
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    header = READ_RANGE_HEADER.match(normalized)
    offset = int(header.group("start")) - 1 if header else 0
    body = normalized[header.end() :] if header else normalized
    lines = body.splitlines()
    if not lines or max_chars <= 0:
        return None
    scopes: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        match = DEFINITION.match(line)
        if match is None or match.group(2).casefold() not in terms:
            continue
        indent = len(match.group(1))
        end = index + 1
        while end < len(lines):
            following = lines[end]
            if following.strip() and len(following) - len(following.lstrip()) <= indent:
                break
            end += 1
        scopes.append((index, end))

    anchored: set[int] = set()
    if header:
        for chunk in chunks:
            if chunk.path != header.group("path"):
                continue
            snippet_lines = chunk.snippet.replace("\r\n", "\n").splitlines()
            if chunk.truncated:
                snippet_lines = snippet_lines[:-1]
            overlaps = [
                (chunk.start_line - 1 + i - offset, line)
                for i, line in enumerate(snippet_lines)
                if 0 <= chunk.start_line - 1 + i - offset < len(lines)
            ]
            if overlaps and all(lines[i] == line for i, line in overlaps):
                anchored.update(i for i, _ in overlaps)

    eligible = set(range(len(lines)))
    if scopes:
        eligible = {i for start, end in scopes for i in range(start, end)}
    if anchored and eligible.intersection(anchored):
        eligible.intersection_update(anchored)

    # Parse individual scopes: a following incomplete read must not invalidate a complete
    # function. For module-level data, parse the whole read if possible.
    statements: list[tuple[int, int]] = []
    for start, end in scopes or [(0, len(lines))]:
        try:
            tree = ast.parse(textwrap.dedent("\n".join(lines[start:end])))
        except (SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.stmt) or isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                continue
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue
            first, last = start + node.lineno - 1, start + (node.end_lineno or node.lineno)
            if eligible.intersection(range(first, last)):
                statements.append((first, last))

    candidates = statements or [(i, i + 1) for i in sorted(eligible)]
    scored: list[tuple[int, int, int, int]] = []
    for start, end in candidates:
        text = "\n".join(lines[start:end])
        if len(text) > max_chars or not text.strip():
            continue
        matches = [term for term in terms if term in text.casefold() and "/" not in term]
        independent = [t for t in matches if not any(t != u and t in u for u in matches)]
        if independent:
            scored.append((len(independent), sum(map(len, independent)), start, end))
    if not scored:
        return None
    _, _, start, end = max(scored, key=lambda row: (row[0], row[1], -row[2]))
    # Expand only within the selected scope and with complete lines.
    lower, upper = next(((a, b) for a, b in scopes if a <= start < b), (0, len(lines)))
    while True:
        changed = False
        if end < upper and len("\n".join(lines[start : end + 1])) <= max_chars:
            end += 1
            changed = True
        if start > lower and len("\n".join(lines[start - 1 : end])) <= max_chars:
            start -= 1
            changed = True
        if not changed:
            break
    return offset + start + 1, offset + end, "\n".join(lines[start:end])
