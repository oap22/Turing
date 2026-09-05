# S01 — Completion-driven DAG orchestration

Status: adversarial-reviewed; dispatched to Luna xhigh. Parent: #419. Baseline: 94f1e1f. Implementation issue: #421.

## Problem and contract

DAGOrchestrator.run currently gathers a whole ready wave. With independent
A and B, and C depending only on A, C cannot start until B finishes. Replace
this barrier: a task whose dependencies have completed becomes eligible while
unrelated tasks remain in flight. Every node has at most one active attempt;
all owned asyncio tasks are cancelled and joined on failure or cancellation;
synthesis runs once only after every live node completes successfully.

## Ownership and non-goals

Own src/turing/coordinator/orchestrator.py and its three existing test modules
(test_orchestrator.py, test_orchestrator_remote.py,
test_orchestrator_needs_subtask.py); a new test_orchestrator_scheduling.py is
preferred for added cases. Do not change dispatch wire contracts, schema,
capability tokens, retry policy, scheduler worker selection, agent/core.py,
or dependencies. Keep constructor/run signatures and successful output shape.
Do not claim cancellation of remote execution: cancelling a local await only
cleans up coordinator tasks; remote work follows existing deadlines.

## Required behavior

1. Maintain completed and in-flight IDs separately. Dispatch ready nodes in
   live graph insertion order. Wait for FIRST_COMPLETED, incorporate completed
   results, then discover newly ready nodes without waiting for other tasks.
2. Process all results in a completion batch before dispatching more work. If
   any result failed, stop dispatch, cancel/join remaining owned tasks, raise
   the original error, and never synthesize. Choose the first failure in live
   graph insertion order when several complete together; consume all errors.
3. A worker returning NEEDS_SUBTASK remains incomplete and its in-flight entry
   is removed before rediscovery. The parent becomes eligible after its original dependencies and the
   designated rejoin_after nodes complete, even if other fragment nodes are
   still running; synthesis waits for all nodes. Retry once per deferral. No busy loop or second concurrent parent attempt. A resumed parent may
   request another valid fragment; do not introduce a one-deferral lifetime
   limit or change the existing retry policy.
4. Fragment insertion must be atomic: validate every new ID/output key against
   the live graph and the proposed fragment before mutation. A late collision
   must not leave earlier fragment nodes installed. Output URI collisions must
   fail before any ambiguous output can be consumed. Also reject duplicate output URIs in the initial DAG in from_dag before
   any worker is dispatched. Reject a rejoin node ID that collides with an
   existing parent input alias mapped to a different URI before mutation.
   Existing wire validation remains unchanged; put defensive runtime checks
   in the live graph layer.
5. Preserve original dependencies and resolved inputs when a deferred parent
   resumes. Two concurrent fragments with colliding IDs fail cleanly; one cannot
   overwrite the other's node. Existing local/remote episode semantics remain.
6. On caller cancellation, use finally to cancel/join every owned pending
   task; propagate CancelledError. Do not catch BaseException and turn it into
   success. Do not await unrelated global tasks. Normal return leaves no
   coordinator-owned task running. Cooperative worker cancellation is assumed;
   suppressing cancellation forever remains outside this in-process contract.
7. If unfinished nodes exist but neither ready nor in-flight work exists,
   raise an explicit stalled-graph error with unresolved IDs. Never synthesize
   partial results. Empty graphs remain governed by existing schema validation.

## Acceptance tests (real exported run path)

Use asyncio.Event barriers and bounded watchdogs solely to fail hung tests;
no sleep-based performance assertions, no private helper scheduler replicas.

- Start A and B. Release A, keep B blocked. Assert C starts and sees A's
  exact output while B is still blocked. Release B; assert one synthesis and
  correct leaf outputs. This must fail against the baseline wave loop.
- Diamond fan-in: C depends on A/B; release one at a time and assert C starts
  only after both outputs exist, with each worker invoked once.
- Fast chain alongside blocked unrelated leaf: no chain duplication at each
  completion; multiple simultaneous completions handled without lost outputs.
- Local deferral while an unrelated task is blocked: inserted child executes,
  parent resumes with child plus original inputs, and synthesis waits for all.
- Parent defers twice sequentially with distinct valid fragments, then
  succeeds: three parent calls total, never two in flight, correct inputs.
- Collision in second fragment node leaves the graph unchanged; concurrent
  overlapping fragments abort with no stranded owned tasks. Include distinct
  IDs sharing an output URI and existing-graph output collisions.
- Initial DAG with distinct IDs sharing an output URI: run rejects before
  any worker or synthesis call. The existing wire schema accepts this case.
- Deferral with independent X/Y and rejoin_after=[X]: release X while Y is
  blocked; parent resumes with X, synthesis still waits for Y.
- Parent input alias matches a new rejoin ID but points to its original
  dependency: reject atomically, preserve parent inputs/graph, clean up blocked
  sibling, and never synthesize.
- One failing worker with a blocked sibling: sibling observes cancellation,
  its finally finishes, original exception reaches caller, synthesis absent.
- Simultaneous batch success+failure: A succeeds and B fails in the same
  observed completion batch; C depends on A. Assert C never starts. Two
  simultaneous failures must propagate the first by graph insertion order
  and consume the other exception (capture event-loop exception reporting).
  Use controlled task completion/barriers so this is not a timing lottery.
- Cancel run externally with two blocked workers; both clean up; no leaked
  task warning. A cancelled child must also abort the run, not return a string.
- Existing remote retry/token/episode tests continue to pass unchanged.

## Validation and delivery

Run pytest tests/test_coordinator/ -q, ruff check and format --check on owned
files, and mypy src/turing/coordinator/. Use the project's installed environment
with this worktree's src on PYTHONPATH. Show baseline failure in disposable
scratch, never revert a shared checkout. Report exact commands and results.
This is scheduling correctness evidence, not a measured real-model speedup.
Review tier: fresh agent review unless final scope triggers a mandatory human
tier (>400 non-test lines, a wire/schema/dependency/CODEOWNERS change, etc.).
