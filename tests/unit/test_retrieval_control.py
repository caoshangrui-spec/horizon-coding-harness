from horizon.application.retrieval_control import (
    find_evidence_reacquisition,
    find_redundant_retrieval,
)
from horizon.domain.model import FunctionCall, ModelMessage, ToolCall
from horizon.domain.retrieval import EvidenceChunk, EvidencePack


def _settled_messages(
    query: str = "split PATH entries executable discovery os.pathsep",
    *,
    max_chunks: int = 1,
    workspace_revision: str = "a" * 64,
    path: str = "thefuck/utils.py",
    snippet: str = "def which(program):\n    paths = PATH.split(os.pathsep)",
) -> tuple[ModelMessage, ...]:
    arguments = {"query": query}
    if max_chunks != 1:
        arguments["max_chunks"] = max_chunks
    evidence = EvidencePack(
        query=query,
        normalized_terms=("split", "path", "executable"),
        workspace_revision=workspace_revision,
        source_manifest_ref="b" * 64,
        scope_hash="c" * 64,
        index_key="d" * 64,
        backend="sqlite_fts5",
        status="ok",
        indexed_file_count=380,
        skipped_file_count=0,
        chunks=(
            EvidenceChunk(
                rank=1,
                path=path,
                start_line=36,
                end_line=75,
                content_hash="e" * 64,
                snippet=snippet,
            ),
        ),
    )
    return (
        ModelMessage(
            role="assistant",
            tool_calls=(
                ToolCall(
                    id="prior-retrieval",
                    function=FunctionCall(name="retrieve_code", arguments=arguments),
                ),
            ),
        ),
        ModelMessage(
            role="tool",
            content=evidence.model_dump_json(),
            tool_call_id="prior-retrieval",
        ),
    )


def _settled_read(
    call_id: str,
    path: str,
    content: str,
) -> tuple[ModelMessage, ModelMessage]:
    return (
        ModelMessage(
            role="assistant",
            tool_calls=(
                ToolCall(
                    id=call_id,
                    function=FunctionCall(
                        name="read_file",
                        arguments={"path": path, "start_line": 1},
                    ),
                ),
            ),
        ),
        ModelMessage(role="tool", content=content, tool_call_id=call_id),
    )


def test_observed_thefuck_query_is_reused_with_a_neighboring_path_hint():
    hint = find_redundant_retrieval(
        _settled_messages(),
        {"query": "PATH split executable find"},
        "a" * 64,
    )

    assert hint is not None
    assert hint.shared_term_count == 3
    assert hint.current_term_count == 4
    assert hint.containment == 0.75
    assert hint.path == "thefuck/utils.py"
    assert (hint.suggested_start_line, hint.suggested_end_line) == (16, 155)
    assert hint.detail.startswith("Near-duplicate retrieve_code was not dispatched")
    assert "read_file" in hint.detail


def test_retrieval_reuse_allows_an_explicit_candidate_expansion():
    hint = find_redundant_retrieval(
        _settled_messages(),
        {"query": "PATH split executable find", "max_chunks": 3},
        "a" * 64,
    )

    assert hint is None


def test_retrieval_reuse_allows_a_new_symbol_or_meaningful_term():
    symbol_hint = find_redundant_retrieval(
        _settled_messages(),
        {"query": "PATH split executable get_all_executables"},
        "a" * 64,
    )
    domain_hint = find_redundant_retrieval(
        _settled_messages(),
        {"query": "PATH split executable aliases"},
        "a" * 64,
    )

    assert symbol_hint is None
    assert domain_hint is None


def test_retrieval_reuse_never_crosses_a_workspace_revision():
    hint = find_redundant_retrieval(
        _settled_messages(),
        {"query": "PATH split executable find"},
        "f" * 64,
    )

    assert hint is None


def test_sanic5_pattern_transitions_from_duplicate_target_reads_to_action():
    revision = "a" * 64
    snippet = (
        "LOGGING_CONFIG_DEFAULTS = dict(\n"
        "    loggers={\n"
        '        "root": {"level": "INFO", "handlers": ["console"]},'
    )
    read_content = (
        "READ_RANGE path=sanic/log.py lines=1-58 total_lines=58\n"
        f"{snippet}\n"
        'logger = logging.getLogger("sanic.root")\n'
    )
    messages = (
        *_settled_messages(
            "root logger default handler level sanic/log.py",
            workspace_revision=revision,
            path="sanic/log.py",
            snippet=snippet,
        ),
        *_settled_read("read-1", "sanic/log.py", read_content),
        *_settled_read("read-2", "sanic/log.py", read_content),
        *_settled_read(
            "read-3",
            "sanic/log.py",
            "NoProgressPolicy: identical tool name and arguments were already settled twice.",
        ),
    )

    hint = find_evidence_reacquisition(
        messages,
        "retrieve_code",
        {"query": "LOGGING_CONFIG_DEFAULTS root handlers level sanic/log.py"},
        revision,
    )

    assert hint is not None
    assert hint.path == "sanic/log.py"
    assert hint.identical_read_count == 2
    assert '"root"' in hint.snippet
    assert "Further read_file/retrieve_code was not dispatched" in hint.detail
    assert "replace_text" not in hint.detail
    assert "allowed write tool" in hint.detail
    assert "protected validation" in hint.detail


def test_evidence_transition_uses_repeated_read_when_rank_one_chunk_misses_edit_line():
    revision = "a" * 64
    retrieval_snippet = (
        '        "formatter": "access",\n'
        "    },\n"
        "    formatters={\n"
        'logger = logging.getLogger("sanic.root")'
    )
    read_content = (
        "READ_RANGE path=sanic/log.py lines=1-58 total_lines=58\n"
        "import logging\nimport sys\n\n"
        "LOGGING_CONFIG_DEFAULTS = dict(\n"
        "    version=1,\n"
        "    disable_existing_loggers=False,\n"
        "    loggers={\n"
        '        "root": {"level": "INFO", "handlers": ["console"]},\n'
        "    },\n"
        f"{retrieval_snippet}\n"
    )
    messages = (
        *_settled_messages(
            "root logger name configuration in sanic/log.py",
            workspace_revision=revision,
            path="sanic/log.py",
            snippet=retrieval_snippet,
        ),
        *_settled_read("read-1", "sanic/log.py", read_content),
        *_settled_read("read-2", "sanic/log.py", read_content),
    )

    hint = find_evidence_reacquisition(
        messages,
        "retrieve_code",
        {"query": "sanic/log.py LOGGING_CONFIG_DEFAULTS root logger configuration"},
        revision,
    )

    assert hint is not None
    assert (hint.start_line, hint.end_line) == (1, 58)
    assert '"root": {"level": "INFO"' in hint.snippet
    assert not hint.snippet.startswith('        "formatter": "access"')
    assert "Edit-ready repeated-read evidence" in hint.detail


def test_evidence_transition_requires_two_identical_reads_containing_rank_one_snippet():
    revision = "a" * 64
    first = "def which(program):\n    paths = PATH.split(os.pathsep)\n# first range"
    second = "def which(program):\n    paths = PATH.split(os.pathsep)\n# second range"
    messages = (
        *_settled_messages(workspace_revision=revision),
        *_settled_read("read-1", "thefuck/utils.py", first),
        *_settled_read("read-2", "thefuck/utils.py", second),
    )

    assert (
        find_evidence_reacquisition(
            messages,
            "retrieve_code",
            {"query": "split PATH entries executable discovery"},
            revision,
        )
        is None
    )


def test_evidence_transition_allows_explicit_candidate_expansion():
    revision = "a" * 64
    content = "def which(program):\n    paths = PATH.split(os.pathsep)"
    messages = (
        *_settled_messages(workspace_revision=revision),
        *_settled_read("read-1", "thefuck/utils.py", content),
        *_settled_read("read-2", "thefuck/utils.py", content),
    )

    assert (
        find_evidence_reacquisition(
            messages,
            "retrieve_code",
            {"query": "split PATH executable", "max_chunks": 3},
            revision,
        )
        is None
    )


def test_evidence_transition_allows_an_explicit_different_path_query():
    revision = "a" * 64
    content = "def which(program):\n    paths = PATH.split(os.pathsep)"
    messages = (
        *_settled_messages(workspace_revision=revision),
        *_settled_read("read-1", "thefuck/utils.py", content),
        *_settled_read("read-2", "thefuck/utils.py", content),
    )

    assert (
        find_evidence_reacquisition(
            messages,
            "retrieve_code",
            {"query": "split PATH executable thefuck/shells.py"},
            revision,
        )
        is None
    )
