from __future__ import annotations

import re
from dataclasses import dataclass

ASCII_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
IDENTIFIER_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
MAX_QUERY_CHARS = 200
MAX_QUERY_TERMS = 20

LOW_SIGNAL_TERMS = frozenset({"a", "an", "for", "of", "should", "test", "tests", "the", "to"})
TEST_SCAFFOLD_TERMS = frozenset({"test", "tests", "testcase"})

# These aliases are deliberately small and domain-generic. They are frozen before the
# development treatment is run; no source paths or implementation symbols are included.
ENGLISH_TERM_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "characters": ("character",),
    "chars": ("character",),
    "decodes": ("decode",),
    "failed": ("failure",),
    "formatting": ("format",),
    "generated": ("generate",),
    "generation": ("generate",),
    "preserves": ("preserve",),
    "repr": ("representation",),
    "representation": ("repr",),
    "wont": ("not",),
}

# Longest phrases must appear first so, for example, `无法` wins over `无`.
CHINESE_TERM_GLOSSARY: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("请求体", ("request", "body")),
    ("客户端", ("client",)),
    ("上下文", ("context",)),
    ("调度器", ("scheduler",)),
    ("重定向", ("redirect",)),
    ("自定义", ("custom",)),
    ("不应", ("not",)),
    ("无法", ()),
    ("解码", ("decode",)),
    ("字符", ("character",)),
    ("生成", ("generate",)),
    ("状态", ("status",)),
    ("覆盖", ("override",)),
    ("已有", ("existing",)),
    ("单个", ("single",)),
    ("表示", ("representation", "repr")),
    ("失败", ("failure",)),
    ("无", ("without",)),
    ("非", ("non",)),
    ("值", ("value",)),
)


@dataclass(frozen=True)
class RetrievalQueryNormalization:
    original_query: str
    normalized_query: str
    terms: tuple[str, ...]
    applied_rules: tuple[str, ...]


def _identifier_parts(value: str) -> tuple[str, ...]:
    parts: list[str] = []
    for segment in value.split("_"):
        parts.extend(part.casefold() for part in IDENTIFIER_BOUNDARY.split(segment) if part)
    return tuple(parts)


def _without_test_scaffolding(parts: tuple[str, ...]) -> tuple[tuple[str, ...], bool]:
    retained: list[str] = []
    removed = False
    for index, part in enumerate(parts):
        if part in TEST_SCAFFOLD_TERMS:
            removed = True
            continue
        if part == "case" and index > 0 and parts[index - 1] in TEST_SCAFFOLD_TERMS:
            removed = True
            continue
        retained.append(part)
    return tuple(retained), removed


def _camel_identifier(parts: tuple[str, ...]) -> str:
    if len(parts) < 2:
        return ""
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])


def _chinese_events(query: str) -> tuple[tuple[int, tuple[str, ...]], ...]:
    events: list[tuple[int, tuple[str, ...]]] = []
    index = 0
    while index < len(query):
        matched = False
        for phrase, terms in CHINESE_TERM_GLOSSARY:
            if query.startswith(phrase, index):
                if terms:
                    events.append((index, terms))
                index += len(phrase)
                matched = True
                break
        if not matched:
            index += 1
    return tuple(events)


def normalize_retrieval_query(query: str) -> RetrievalQueryNormalization:
    """Produce a bounded, deterministic lexical query without a model or source inspection."""
    if not query or len(query) > MAX_QUERY_CHARS:
        raise ValueError("Retrieval query normalization requires 1 to 200 characters")

    events: list[tuple[int, tuple[str, ...]]] = list(_chinese_events(query))
    rules: list[str] = []
    if events:
        rules.append("zh_glossary_v1")

    for match in ASCII_IDENTIFIER.finditer(query):
        raw_parts = _identifier_parts(match.group())
        parts, removed_scaffolding = _without_test_scaffolding(raw_parts)
        if removed_scaffolding:
            rules.append("test_scaffold_drop_v1")
        filtered = tuple(part for part in parts if part not in LOW_SIGNAL_TERMS)
        if filtered != parts:
            rules.append("low_signal_drop_v1")
        if not filtered:
            continue

        event_terms: list[str] = []
        composite = _camel_identifier(filtered)
        if composite:
            event_terms.append(composite)
            rules.append("identifier_split_v1")
        for part in filtered:
            event_terms.append(part)
            expansions = ENGLISH_TERM_EXPANSIONS.get(part, ())
            if expansions:
                event_terms.extend(expansions)
                rules.append("english_alias_v1")
        events.append((match.start(), tuple(event_terms)))

    events.sort(key=lambda event: event[0])
    terms: list[str] = []
    seen_terms: set[str] = set()
    for _, event_terms in events:
        for term in event_terms:
            folded = term.casefold()
            if folded and folded not in seen_terms:
                seen_terms.add(folded)
                terms.append(term if term != folded else folded)
            if len(terms) == MAX_QUERY_TERMS:
                break
        if len(terms) == MAX_QUERY_TERMS:
            rules.append("term_limit_v1")
            break

    if not terms:
        normalized_query = query
        rules.append("preserve_unmapped_query_v1")
    else:
        normalized_query = " ".join(terms)
    if len(normalized_query) > MAX_QUERY_CHARS:
        raise ValueError("Normalized retrieval query exceeds 200 characters")

    return RetrievalQueryNormalization(
        original_query=query,
        normalized_query=normalized_query,
        terms=tuple(terms),
        applied_rules=tuple(dict.fromkeys(rules)),
    )
