# Local workers and ROSIE

Use a hardware-independent coordinator with a bounded pool of local inference
workers now. Jetsons can be added later through the network fleet. ROSIE is a
Slurm compute target, not another always-running agent on a login node.

```text
Operator / Codex (one coding issue + worktree per session)
  ├─ python -m turing.local_fleet
  │    ├─ coordinator: explicit tasks, round-robin dispatch, result collection
  │    └─ local-1 … local-N: signed WorkerLoop → local Ollama endpoint
  └─ SSH ROSIE → sbatch → compute node → scoped metadata pull → local files
```

The local runner uses Turing's existing `SubtaskDispatchClient`, signed
transport, and `WorkerLoop`. Each worker receives one task at a time; different
workers run concurrently, up to eight. The coordinator sends direct worker
subjects, avoiding broadcast duplication. Deadlines start when a worker takes
a task, so waiting tasks do not expire before they start. Failure and timeout
statuses are returned and make the command exit nonzero.

This runner is a single process with separate worker identities and an
in-process bus. It is not process isolation, a durable queue, or an autonomous
planner. A process interruption retains completed records but does not resume
unfinished tasks. No shell tools, cloud fallback, or source edits are enabled.
Multiple workers sharing one inference server share its compute and memory;
increasing worker count does not guarantee faster inference.

Some reasoning models spend a small token cap entirely on internal reasoning,
returning no final text. That is reported as a failed task. Increase
`--max-tokens` within your budget if this occurs; do not count an empty response
as completed work. Token usage depends on what the provider reports and is not
a billing or resource-accounting record.

## Run locally

Use the project Python environment from `CLAUDE.md`. From a fresh worktree,
either install a venv there or use an existing compatible interpreter with
`PYTHONPATH=src` so imports resolve to this checkout.

First exercise the real dispatch/worker path with synthetic responses:

```bash
python -m turing.local_fleet --smoke --workers 2 \
  --prompt 'first task' --prompt 'second task' \
  --output ~/research-results/local-fleet-smoke-01
```

For real local inference, start your Ollama-compatible server and select an
installed model (`ollama list`, or its `/api/tags` endpoint). No model is
downloaded automatically. Example, after installing the named model:

```bash
python -m turing.local_fleet --workers 2 --model gemma3:1b \
  --host http://localhost:11434 --timeout 120 --max-tokens 256 \
  --prompt 'Explain a bounded task queue in two sentences.' \
  --prompt 'List three reasons a worker might time out.' \
  --output ~/research-results/local-fleet-01
```

Choose a new output directory each time. `workflow.json` identifies the mode
and worker pool; `results.jsonl` preserves each task's result and worker ID;
`metrics.jsonl` exposes progress in the desktop. These are operational records,
not validated research claims. Actual experiments still use an agreed brief
and `research-loop`'s `log_run.py` contract. The watched root defaults to
`~/research-results`; check desktop settings if it has been changed.

The older `scripts/dev/fleet-up.sh` / `docker-compose.yml` path remains a
four-container peer-presence simulation. Its `python -m turing` entrypoint does
not wire `WorkerLoop` into the dispatch bus. Seeing four peers there is not
proof of four executing agents. The local runner explicitly wires that path.
Issue #434 separately owns fleet model discovery and routing.

## Verify the ROSIE round trip

Connect to MSOE VPN off campus. SSH uses the existing `ROSIE` alias; no keys,
VPN settings, remote agents, or shared environments are installed here.
Check the installed `rosie-run` skill for current cluster guidance.

The bundled infrastructure check requests one node, one CPU, 128 MiB,
two minutes, and no GPU on `teaching` (at most 0.034 node-hours). It writes
only a tiny metadata record and logs. Source transfer is limited to this
unlogged diagnostic harness; it is not an experiment deployment method.

```bash
python3 scripts/dev/rosie-smoke.py submit \
  --output ~/research-results/rosie-workflow-smoke-01
# Run after the job has finished; pending/accounting-delay is not success.
python3 scripts/dev/rosie-smoke.py collect \
  --output ~/research-results/rosie-workflow-smoke-01
```

Submission captures storage/partition/queue preflight, creates a unique remote
directory, and records the Slurm job ID. Slurm opens log files in the already
created directory. Collection requires `sacct` to report `COMPLETED` and
`0:0`, downloads only four named files, verifies both job identity and script
SHA-256, then publishes an atomic snapshot. `verified.json` records the
accounting and metadata. Repeated collection refuses to overwrite that snapshot.
Nothing is deleted remotely. Hostnames, Python version, and job IDs come from
the compute node, not reconstructed from the login node.

Every SSH command and rsync operation has a timeout. If submission is uncertain,
do not rerun it in a new directory: read `submission.json`'s exact remote path,
inspect its `submission.txt` and `squeue`/`sacct`, and reconcile the job ID first.
A missing accounting entry can be a delay; collect again later. A failed job
is not a verified result; inspect `accounting.txt` and its remote logs.

For actual research jobs: commit and push code with git, inspect the remote
checkout before touching it, create a separate worktree at that exact SHA,
and verify it. Start from `rosie-run/templates/job.sbatch`, fill the bounded
resource request, create `logs/` before `sbatch`, and invoke `log_run.py` inside
the job. Record the returned job ID. Pull only owned run IDs and metadata
(`run.json`, metrics, logs, notes, trajectory, requested plots), excluding large
artifacts by default. Validate with `log_run.py check` before citing a result.
Use the same per-run local directory for atomic snapshots, never `tail -n +1 |
tee -a` across reconnects. Keep checkpoints remote until requested.

## Codex follows the same contribution rules

`AGENTS.md` is the repository entry point, and `.agents/skills` exposes the
canonical workflow skill. `.codex/config.toml` sets workspace-write sandboxing
and on-request approval without pinning a personal model or installing secrets.
The current checkout must be trusted in user configuration for project settings
to apply. Managed policy can further restrict them.

Verify locally without launching a model task:

```bash
codex --strict-config doctor --summary
codex debug prompt-input 'Check repository workflow discovery' > /tmp/turing-prompt.json
```

Inspect the rendered prompt for this repository's `AGENTS.md` guidance and
`multi-agent-workflow` skill. A successful TOML parse alone does not prove the
rules reached the agent. The prompt can include personal context; keep it local.
Use one issue/branch/worktree per coding agent, preserve concurrent edits,
self-classify the PR review tier, and satisfy CI and review before merging.
