# Horizon v0.1.0 — Recoverable Coding Agent Harness

Horizon v0.1.0 delivers the first runnable engineering core of a durable control plane for
long-horizon coding agents. The release focuses on recoverability, evidence and bounded execution
rather than claiming autonomous benchmark performance.

## Highlights

- Event-sourced Run state, idempotent commands and fenced Worker leases with epoch-based handoff.
- Content-addressed snapshots, checkpoints, model responses, tool receipts and replayable JSONL
  traces.
- Bounded sequential WorkItem DAG execution, one-shot model planning, one evidence-bound replan and
  narrow persistent human intervention.
- Deterministic context projection, mandatory fact ledgers, evidence-backed Run Memory and
  revision-bound lexical Code RAG.
- Typed read/retrieval/edit/check tools, isolated staging workspaces, protected Docker validation
  and bounded promotion.
- Campaign/Run cost gates, conservative unknown-outcome handling and recovery across model, tool and
  promotion commit windows.
- A zero-network, zero-paid-call portfolio demo that exports a self-verifying EvidencePack.

## Verified evidence at release preparation

- Public GitHub Actions CI is green on Python 3.12 and 3.13.
- Offline regression suite: `282 passed, 5 skipped`.
- Docker-only contract slice: `5 passed` with the preinstalled `python:3.12-alpine` image.
- Source-bound full-checkout A/B runs pass on tqdm (82 files) and youtube-dl (872 files), including
  initial-failure gates, recovery/replan behavior, protected validation and Trace replay.
- Four SiliconFlow real-model Pilot runs are preserved as negative evidence with settled accounting,
  unchanged source checkouts and replayable partial traces.

## Quick demo

```powershell
uv sync --locked
uv run --locked --cache-dir .uv-cache horizon demo run
```

The demo does not need an API key, Docker or network access and has zero external model cost.

## Claim boundaries

This release does **not** claim a successful real-model Issue repair, an official BugsInPy or
SWE-bench score, production-grade sandbox security, general semantic memory, autonomous fallback,
parallel WorkItems or completion of the full design backlog. Scripted-model A/B evidence validates
the Harness path, not model quality.

See the [development evidence](development-progress.md), [architecture and design](coding-agent-development-design.md),
and [security boundary](../SECURITY.md) for the full record.
