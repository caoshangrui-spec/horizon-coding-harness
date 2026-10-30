from horizon.application.retrieval_control import find_redundant_retrieval
from horizon.domain.model import FunctionCall, ModelMessage, ToolCall
from horizon.domain.retrieval import EvidenceChunk, EvidencePack


def _settled_messages(
    query: str = "split PATH entries executable discovery os.pathsep",
    *,
    max_chunks: int = 1,
    workspace_revision: str = "a" * 64,
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
                path="thefuck/utils.py",
                start_line=36,
                end_line=75,
                content_hash="e" * 64,
                snippet="def which(program):\n    paths = PATH.split(os.pathsep)",
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
    assert "already-correct implementation" in hint.detail
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
