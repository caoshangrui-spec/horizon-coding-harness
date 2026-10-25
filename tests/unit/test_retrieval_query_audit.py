from pathlib import Path

import yaml

from horizon.adapters.retrieval.query_normalization import (
    normalize_retrieval_query_conservatively,
)
from horizon.domain.common import digest

ROOT = Path(__file__).resolve().parents[2]
AUDIT_PATH = (
    ROOT / "benchmarks" / "retrieval" / "historical-model-query-audit-v1" / "result-v1.yaml"
)


def _load_audit():
    return yaml.safe_load(AUDIT_PATH.read_text(encoding="utf-8"))


def test_historical_model_query_audit_counts_cover_every_reserved_retrieval():
    audit = _load_audit()
    scope = audit["scope"]
    traces = audit["traces"]
    documented = audit["documented_argument_observations"]
    unresolved = audit["unresolved_argument_observations"]

    assert len(traces) == scope["trace_count"] == 7
    assert sum(trace["event_count"] for trace in traces) == scope["total_event_count"] == 272
    assert (
        sum(trace["settled_model_call_count"] for trace in traces)
        == scope["settled_model_call_count"]
        == 22
    )
    assert (
        sum(trace["retrieve_code_reservation_count"] for trace in traces)
        == scope["retrieve_code_reservation_count"]
        == 10
    )
    assert len(documented) == scope["documented_argument_count"] == 6
    assert len(unresolved) == scope["unresolved_argument_count"] == 4

    observed_by_run: dict[str, int] = {}
    for observation in [*documented, *unresolved]:
        run_id = observation["run_id"]
        observed_by_run[run_id] = observed_by_run.get(run_id, 0) + 1
    assert observed_by_run == {
        trace["run_id"]: trace["retrieve_code_reservation_count"]
        for trace in traces
        if trace["retrieve_code_reservation_count"]
    }

    argument_hashes = {observation["arguments_hash"] for observation in [*documented, *unresolved]}
    unresolved_hashes = {item["arguments_hash"] for item in unresolved}
    assert len(argument_hashes) == scope["unique_retrieve_argument_hash_count"] == 6
    assert len(unresolved_hashes) == scope["unresolved_unique_argument_hash_count"] == 3
    assert all(len(trace["trace_sha256"]) == 64 for trace in traces)
    assert all(len(arguments_hash) == 64 for arguments_hash in argument_hashes)


def test_documented_queries_match_trace_hashes_and_bypass_conservative_policy():
    audit = _load_audit()
    documented = audit["documented_argument_observations"]

    for observation in documented:
        assert digest(observation["arguments"]) == observation["arguments_hash"]
        query = observation["arguments"]["query"]
        result = normalize_retrieval_query_conservatively(query)
        assert result.normalized_query == observation["normalized_query"] == query
        assert list(result.applied_rules) == observation["applied_rules"] == ["gate_bypass_v1"]

    assert len({item["arguments"]["query"] for item in documented}) == 3
    outcome = audit["observed_policy_outcome"]
    assert outcome["documented_argument_count"] == outcome["exact_bypass_count"] == 6
    assert outcome["normalization_change_count"] == 0
    assert outcome["known_cjk_count"] == 0
    assert outcome["unresolved_language_shape_count"] == 4
    assert audit["decision"]["default_runtime_policy"] == "unchanged"
    assert audit["decision"]["holdout_status"] == "sealed"


def test_unresolved_query_arguments_remain_unknown_instead_of_being_guessed():
    audit = _load_audit()
    unresolved = audit["unresolved_argument_observations"]

    assert all(item["status"] == "unknown_raw_arguments" for item in unresolved)
    assert all("arguments" not in item for item in unresolved)
    assert all("normalized_query" not in item for item in unresolved)
    assert all("query_shape" not in item for item in unresolved)
