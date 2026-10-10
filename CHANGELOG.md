# Changelog

All notable changes to this project are documented here. The project follows semantic versioning
for public releases; unreleased work must not be presented as a published release.

## Unreleased

- Added a single-command, zero-provider Docker create-to-start crash recovery demonstration. A
  real child process exits with code 34 after the production sandbox creates its labeled container
  but before `docker start`; the normal recovery CLI observes the exact `created` attempt, removes
  it without execution, rechecks the workspace revision, and persists a `cancelled / discard_check`
  receipt. The seven-file EvidencePack includes its Trace, final projection, crash observation,
  CLI receipt, workspace manifest, recovery artifact, and deterministic summary. Its standalone
  verifier needs neither Docker nor the control database, replays the Trace, and rejects semantic
  forgery even if file hashes are recomputed. The public Docker workflow now publishes this pack;
  the claim remains limited to this exact crash window and makes no exactly-once assertion.
- Distinguished Docker `created` attempts from running and naturally stopped `run_check`
  containers. An explicitly discarded attempt observed in `created` can now be removed without a
  kill, but removal rechecks that exact state and the controller recaptures the workspace revision
  before writing the recovery receipt. Transitional states, missing attempts, concurrent state
  changes, and post-removal workspace drift remain blocked. Exact stopped-result recovery now also
  permits anonymous volumes declared by the image while continuing to reject every additional
  explicit bind or volume. The existing public Docker evidence workflow now runs these sandbox
  contracts and uploads their JUnit/log evidence before the frozen source-bound suites. This adds
  no retry, fallback, network call from the harness, or paid model use.
- Added an offline terminal Run suite that takes an ordered manifest of existing terminal Run IDs,
  exports one replay-verified EvidencePack per Run, and builds a deterministic aggregate report for
  successes, failures, cancellations, budget-stop reasons, and unknown/open effects. The standalone
  verifier recursively checks every child Trace and recomputes the report without a database. It
  never executes tasks, models, tools, network calls, or repository code, so this is evidence
  aggregation rather than a new batch execution engine.
- Extended the exact, revision-bound NoProgress policy from identical and period-2 actions to
  primitive period-3 and period-4 cycles. The controller soft-blocks one action before a third
  complete cycle, requires the next exact continuation before entering recoverable operator
  guidance, persists the exact cycle period, and revalidates the full receipt window during Trace
  replay. The frozen offline policy suite now covers 14 cases and 62 decisions with explicit
  period-3/4 positives and shorter-period false-positive protection; no semantic matching,
  automatic replan, retry, provider call, or budget expansion was added.
- Added a generic terminal EvidencePack for `SUCCEEDED`, `FAILED`, and `CANCELLED` Runs. The
  read-only `trace bundle` command exports the append-only Trace, canonical final projection, and a
  deterministic human summary, while `trace verify-bundle` checks file hashes, replays the Trace,
  and recomputes both state and summary. Unknown/open effects remain explicit, failed or cancelled
  Runs never claim task success, existing destinations are not overwritten, and self-consistency is
  explicitly not presented as origin authentication.
- Bound parent-crash cancellation recovery to the exact PID already persisted in the Worker stop
  target. Recovery CLI calls now require both the explicit old-worker-stopped acknowledgement and
  `--confirm-old-worker-pid <pid>` before writing operator stop evidence; missing or mismatched PIDs
  leave the fence unchanged. Ordinary expired-lease recovery without a cancellation target remains
  backward compatible.
- Fixed the operator-confirmed cancellation takeover to timestamp its stop receipt and replacement
  lease from the same decision clock. This prevents a faster clock from making the second event
  appear older than the first within one SQLite append; the regression test now advances by one
  microsecond on every clock read so the ordering guarantee is platform-independent.
- Added recovery-safe cancellation for one directly supervised local Worker. The trusted parent
  now persists an exact PID/lease/epoch/launch-cursor stop target before touching the process,
  then performs bounded terminate/wait, kill/wait fallback, or synchronous reap of an already
  exited child. Only a matching stop receipt releases the lease or closes cancellation; pending
  effects remain intact for the existing recovery path and never redispatch. A cancellation-bound
  Worker blocks every new lease, including after cooperative lease release. If the parent itself
  crashes, the existing explicit old-worker-stopped confirmation can clear the target only after
  lease expiry and records distinct operator evidence. Real subprocess and deterministic fault
  tests cover fence ordering, pending intent, forced kill, early exit, cooperative release,
  mismatched identity, parent-crash recovery, CLI terminal handling, and Trace replay. This is an
  application API for a single direct child, not process-tree control, a daemon, or a queue.
- Made cancellation recovery-safe instead of terminalizing over in-flight effects. A Run now
  persists `CANCEL_PENDING` as a durable dispatch fence, accepts only late receipts, conservative
  unknown classification, or explicit tool recovery, and enters `CANCELLED` automatically once no
  unclassified or recoverable tool effect remains. Agent multi-tool and protected-validation loops
  recheck the fence after every dispatch. The trusted CLI can additionally stop the single exact
  labeled `run_check` container and record its call/container/image identity in the replayable
  Trace. Historical projections remain compatible; no retry, fallback, network call, or paid model
  use was added.
- Made wall-clock expiry a replayable execution boundary. Active commands now raise a typed
  `RunDeadlineExceeded` and persist `FAILED / wall_clock_limit` whenever no external effect still
  needs classification. Model/tool receipts, conservative unknown classification, lease release,
  and explicit recovery leases remain available after the deadline, so a late receipt is recorded
  before terminalization and a recoverable tool effect is not hidden by a forced failure. Agent
  slice yields and recovery CLI paths apply the same rule; no retry, fallback, network call, or
  budget expansion was added.
- Extended the local sequential Supervisor with a narrow trusted-parent handoff for reaped child
  Workers. A launch boundary binds the exact Run, lease epoch, event cursor, and AgentSession;
  continuation requires zero reservations plus the existing RecoveryService's safe-resume verdict.
  A live lease uses an evidence-bearing release; an expired exact lease is atomically replaced by
  an evidence-bearing recovery epoch only after the parent confirms the process exit. Real
  subprocess tests cover both lease states through validation and Trace replay. Pending-intent
  cases return `reconciliation_required` without reconciliation, model redispatch, or fallback;
  the expired case changes only the fenced lease epoch so explicit recovery can proceed. Separate
  zero-reservation subprocess exits still block when no valid AgentSession boundary exists.
- Added an opt-in local sequential Supervisor for `agent run` and `agent resume`. It executes
  bounded model-iteration slices, hands off only after a newer AgentSession is durable and the Run
  is quiescent, reopens worker-owned persistence/retrieval/tool adapters, and acquires a new lease
  epoch before continuing. Structured output reports slice and handoff counts. Terminal states,
  human requests, explicit slice limits, and unexpected failures stop supervision; unknown effects
  still require the existing reconciliation flow, and no retry, fallback, parallel queue, or budget
  expansion was added.
- Prepared a zero-cost tqdm v3 real-model candidate that exercises task-level least privilege:
  only bounded read, ranked retrieval, and exact replacement are model-visible, while protected
  validation remains controller-owned. Its CNY 0.049 Run cap keeps the frozen series below CNY
  0.25. The current-source, clean-full-checkout, network-disabled Docker preflight is `ready=true`
  with a CNY 0.033774 initial planning reservation; it loaded no credential, made no network/model
  call, and does not authorize paid execution.
- Added an optional TaskSpec `constraints.allowed_tools` capability allowlist. Automatic planning
  schemas and controller validation now use the intersection of execution mode and this task-level
  ceiling; each WorkItem can narrow it again, and the gateway still adds only controller-owned
  `submit`. Omitting the field preserves historical TaskSpec serialization and hashes. Unit,
  planning, execution, and doctor coverage prove least-privilege schemas and reject empty,
  duplicate, unknown, or mode-escalating lists.
- Gated the large execution `revise_plan` tool schema on durable evidence instead of exposing it
  on every fresh WorkItem request. It now appears after at least two persisted execution turns (or
  a multi-tool evidence turn), or immediately when a previous WorkItem has passed; it still
  disappears after the single successful revision. The gate uses AgentSession metadata so model
  and read-only-tool crash recovery reconstruct the same request. An offline reconstruction of the
  sixth paid pilot shows a 1,235-byte / 2,470-token-ceiling / CNY 0.007410 reservation reduction,
  without changing the conservative estimator, spending money, or reinterpreting the failed Run.
- Added source-bound write-effect-before-receipt recovery to the multi-stage Run A/B evaluator.
  Both arms execute the same first Luigi production write in a real child process that fsyncs a
  crash marker and exits before its tool receipt. The parent conservatively records the pending
  effect as unknown, fences the old lease, accepts only the exact existing effect on the next
  epoch, and continues without model redispatch before crossing the two existing WorkItem
  boundaries. Manifest/report schema v2 carries typed crash evidence while schema v1 digests stay
  frozen. The 382-file local trusted and public network-disabled Docker runs both pass 1/1,
  reaching epoch 4 with replayable traces and zero open or unknown calls; the public workflow
  uploads the complete content-addressed state and evidence summary.
- Expanded the three-stage full-checkout recovery suite from one youtube-dl task to two
  independent upstream tasks while preserving the frozen v1/v2 manifests and digests. The new
  Luigi case separates collector binding, handler ownership, and the upstream regression update
  into three dependent WorkItems, crosses two persisted worker boundaries, and compares a
  safely-waiting Baseline with a Treatment that revises only the final incomplete item. A trusted
  local run passed the source-bound, dependency-free runtime, Trace replay, unchanged-source,
  and zero-open-call checks. The public network-disabled Docker workflow then passed both cases
  and published their content-addressed state with an artifact digest.
- Added budget-aware deterministic execution-context projection. Before each model dispatch, the
  runner converts the smallest remaining Run, Campaign, or per-call CNY headroom into an effective
  input-token ceiling while retaining the unchanged conservative sizing formula and output
  reservation. Complete historical tool units are compacted only as needed; when a monetary cap
  cannot be represented or a protected prefix cannot fit it, the unchanged reservation gates make
  the final dispatch-or-typed-BudgetStop decision. The
  effective ceiling is persisted with the request and reused during recovery, preventing a changed
  ledger balance from changing the recovered request hash. Offline integration tests cover a
  successful low-headroom run and crash recovery without redispatch or rebilling.
- Generalized scripted Run A/B worker-boundary injection from one restart to an ordered set while
  preserving the legacy scalar manifest contract and digest. A new youtube-dl full-checkout v2 case
  executes three dependent WorkItems across two complete worker-adapter reopenings, carries
  revision-bound RAG and event-derived Run Memory into lease epoch 3, then compares a Baseline that
  safely waits with a Treatment that revises only the final incomplete item. Local trusted execution
  and the public network-disabled Docker workflow both pass 1/1 with replayable traces, unchanged
  source, zero unknown/open calls, and no paid model use.
- Expanded the clean full-checkout suite from two to three source-bound BugsInPy cases while
  preserving the v1 cases and digest. The new Luigi case fixes the exact 382-file buggy checkout,
  executes the real `MetricsHandler` class without installing its historical dependency stack,
  and retrieves `luigi/server.py` at rank 1 with skipped files reported as degraded. The local
  trusted v2 suite passes 3/3 with replayable traces and unchanged sources. A dedicated public CI
  job now reconstructs all three exact upstream commits and passes the same suite in network-disabled
  Docker containers, publishing the content-addressed state as an artifact. An initial CRLF-sensitive
  multiline edit failure is retained and covered by a line-ending regression test.
- Expanded the source-bound dependency-reduced BugsInPy suite from three to five frozen cases while
  preserving the v1 case objects and digest. The new Apache-2.0 Luigi and Tornado fixtures bind
  upstream buggy/fixed commits, tests, fixes, and licenses. A trusted test-only executor now proves
  all five initial failures and scripted repairs by running their dependency-free Python checks.
  The unified public source-bound workflow also passes all five cases in network-disabled Docker
  and publishes their report/Trace state beside the three-project full-checkout evidence.
- Added `horizon eval recovery` with frozen, backward-readable recovery matrices. v1 contains one
  real child-process exit-after-write-effect case plus 18 exact write state/decision cases. v2
  preserves the v1 digest and adds a real process crash after a model response returns but before
  its Response Artifact is published; recovery must remain blocked and perform zero model
  redispatches. v3 preserves both earlier digests and adds a source-promotion effect-before-receipt
  crash; recovery must settle the exact existing effect without rewriting its target. Reports
  separate `auto_recovered` from `safely_blocked`, count unrecoverable/incorrect resumes and
  duplicate effects, persist per-case content-addressed evidence, detect post-run tampering, and
  remain offline with zero external cost. The matrix explicitly does not claim arbitrary crash
  recovery or the complete designed fault-injection suite.
- Upgraded `horizon demo run` from a graceful handoff-only story to a bounded real-process crash
  recovery demonstration. A child Worker now exits with `os._exit(86)` after `replace_text` changes
  the staging file but before its tool receipt. The supervisor observes the durable intent without
  a receipt, conservatively records `tool_effect_unknown`, fences the reaped Worker, accepts only
  the exact manifest-derived effect, and resumes validation on lease epoch 3 without replaying the
  write. Report schema v3 and the self-verifier bind the crash marker, recovered call ID, Trace,
  evidence-to-write lineage, and final state while retaining v1/v2 report compatibility.
- Added exact recovery for a naturally completed but unreceipted Docker `run_check`. Attempts now
  bind image, request, workspace, and a bounded readable log configuration; the trusted CLI accepts
  only a stopped, non-OOM/non-signal result with matching isolation metadata and complete output no
  larger than 64 KiB. It atomically persists the original success/error observation and next Agent
  session before removing the container. Normal Agent execution now also retains each durable
  attempt until its tool receipt exists, then performs best-effort cleanup and reports cleanup
  failures. Running, timed-out, signaled, oversized, drifted, missing, or mismatched attempts remain
  unknown or use the existing explicit discard path.
- Added explicit recovery for an uncertain single-file `create_file`: the recorded response,
  argument hash, WorkItem authority, pre-dispatch manifest, and exact content-derived manifest
  classify the live workspace as pre-effect, expected-effect, or diverged. Trusted offline
  `--accept-write` / `--rollback-write` decisions settle only the first two states, persist the
  recovery observation and next Agent session atomically, never replay the unknown creation, and
  keep partial or externally drifted states blocked. Real subprocess exit, CLI, rollback conflict
  restoration, Trace replay, and snapshot-hidden path rejection are covered.
- Extended explicit staging promotion to carry at most one validated new UTF-8 file within the
  existing eight-change cap. The plan records `kind=created`, an absent before hash, the candidate
  hash, path authority, source/candidate revisions, optional Git HEAD, and a `/dev/null` audit diff.
  Exclusive publication, identity-bound ordinary rollback, exact absent/after crash recovery,
  mixed partial-effect continuation, CLI dry-run/confirmation, and Trace replay are covered; delete,
  rename, and multiple created files remain rejected.
- Added a deliberately narrow `create_file` tool: one allowed UTF-8 file, a 64 KiB byte cap,
  an existing-parent requirement, and exclusive no-overwrite creation. Exact arguments are bound
  through the recorded model response and intent hash; successful receipts bind revisions and the
  post-effect manifest. Ordinary failures clean up the file, while a real subprocess hard exit
  remains blocking `unknown` without replay or unsupported automatic recovery.
- Persisted a controller-generated client Trace ID in every new planning/execution model intent
  before provider dispatch. Canonical response publication is now shared and verified; ordinary
  post-response persistence/receipt failures immediately quarantine both ledgers, while a real
  subprocess hard exit after model return but before Artifact publication replays as blocking
  `unknown` without an automatic model retry.
- Unified input sizing with the exact canonical OpenAI-compatible body sent by the adapter. New
  reservations persist the payload hash, total UTF-8 bytes, per-top-level-field value bytes, and
  JSON structural bytes under estimator `openai_payload_utf8_bytes_x2_plus_1024_v2`; historical
  v1 traces and recovery remain readable.
- Added an offline `model sizing-report` over five deterministic request shapes: minimal ASCII,
  multibyte messages, nested tool schema, JSON-in-JSON tool arguments, and an 8 KiB tool result.
  It performs no network/model call and does not promote the candidate estimator.
- Persisted pre-dispatch model request sizing beside typed budget stops, including call/request
  identity, purpose, estimator input bytes, input ceiling, configured input cap, and output ceiling;
  planning/execution CLI output and Trace replay expose the same evidence without changing old
  Trace projection hashes.
- Added a zero-cost candidate-estimator replay to `trace reservation-report`. On the three
  historical calls that contain request-byte metadata, `request_bytes + 1024` had zero observed
  underestimates and a 4.137869 median ceiling/usage ratio; the sample is explicitly insufficient
  to change the production gate.
- Added an offline `trace reservation-report` command that replay-verifies one or more traces,
  correlates model reservations with settlements, quantifies token/cost pressure, rejects duplicate
  Runs, and preserves the distinction between PriceCard estimates and provider invoices.
- Measured all six real-model traces without another provider call: 20 settled calls had a 6.861621
  aggregate reserved-to-settled cost ratio; the evidence diagnoses over-reservation but does not
  automatically weaken the estimator or budget gates.
- Recorded a sixth replayable real-model negative result: revision-bound retrieval corrected an
  unsupported Plan location to the actual `tenumerate` implementation at rank 1, but the next
  model dispatch stopped at the Campaign gate before any edit or validation.
- Clarified that complete-inventory validation proves only path existence, not semantic location,
  and restored the planning instruction that exact paths require immutable-task grounding.
- Rejected model-generated paths that are absent from a complete planning inventory while
  preserving abstention when that inventory is truncated; the real-model response is still
  settled once and falls back to the existing human-plan path without an implicit retry.
- Recorded a fifth replayable real-model negative result: planning cost CNY 0.005574, then the
  first execution dispatch stopped before the provider because its reservation exceeded the Run
  remainder by CNY 0.000096; no tool, edit, or validation occurred.
- Added a Trace-verifiable retrieval-to-write lineage to the offline portfolio demo: report schema
  v2 binds the retrieval Artifact, write-time context projection, exact preimage hash, target path,
  and workspace revision while retaining read compatibility for schema v1 reports.
- Added a second, independently frozen external localization holdout across Luigi, Sanic, and
  Tornado; current v7 retrieval ranked all three changed production files first while preserving
  two degraded cases caused by explicitly skipped binary/minified assets.
- Added a three-project external localization blind baseline and a bounded path-diversity
  treatment; the treatment recovers all four changed production paths in top 5 while the
  unchanged 0/3 Hit@1 result remains an explicit limitation.
- Added a frozen same-name-symbol retrieval diagnostic and revision-aware path-context ranking;
  the pre-fix Hit@1/leakage failure remains recorded alongside the zero-model treatment report.
- Added a replayable full-request input-token budget: execution context projection now compacts
  against both character and conservative token ceilings, while planning and probes fail before
  provider dispatch when the same configured ceiling is exceeded.
- Added real-Docker hard-exit coverage showing that a pre-create attempt and an externally removed
  completed `run_check` are both missing after restart, so absence alone remains non-evidence.

## 0.1.0 - 2026-10-03

- Added an event-sourced, replayable Run state with idempotent commands and fenced Worker leases.
- Added durable checkpoints, recovery classification, typed budget stops, and CNY campaign limits.
- Added bounded tool calling, protected validation, staged promotion, and fault-injection tests.
- Added deterministic context projection, mandatory fact ledgers, Run Memory, and lexical Code RAG.
- Added narrow persistent HITL paths and one evidence-bound execution replan.
- Added offline retrieval, reliability, and source-bound Run A/B evaluations.
- Added a one-command portfolio demo with a self-verified EvidencePack.
- Added task-specific real-model preflight evidence for the initial planning reservation.
- Stabilized POSIX artifact verification after hard-link publication and enforced LF checkouts.

Evidence boundaries remain explicit: the release does not claim a real-model Issue success,
official benchmark score, production sandbox security, or completion of the full design backlog.
