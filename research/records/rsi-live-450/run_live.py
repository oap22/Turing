"""Bounded campaign launcher; invoked by log_run.py from a Turing terminal."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
STATE = Path('/private/tmp/turing-rsi450')
CANDIDATE = STATE / 'workspace/rsi-turing-rsi-450'
RESULTS = Path('/Users/owenpacetti/research-results')
RUN = Path(os.environ['RESEARCH_RUN_DIR'])
PYTHON = '/Users/owenpacetti/Developer/active/Turing/.venv/bin/python'
command = [str(ROOT / 'scripts/rsi-loop.sh'), '--slug', 'turing-rsi-450',
           '--results-root', str(RESULTS), '--workspace-root', str(STATE / 'workspace'),
           '--rounds', '2', '--engine', 'codex', '--self-edit-every', '0',
           '--round-timeout-seconds', '600', '--verifier-timeout-seconds', '120',
           '--problem', (STATE / 'problem.txt').read_text(),
           '--verifier', './verify-rsi-450.sh', '--verifier-file', 'verify-rsi-450.sh']
env = dict(os.environ, TURING_RSI_PYTHON=PYTHON, PYTHONPATH=str(ROOT / 'src'))
print('campaign_engine_source=' + str(ROOT / 'src'), flush=True)
print('candidate_source=' + str(CANDIDATE / 'src'), flush=True)
print('engine=codex model=gpt-5.6-luna reasoning=xhigh rounds=2 timeout_per_round=600', flush=True)
trajectory = RESULTS / 'loop-rsi-turing-rsi-450/trajectory.json'
prior_rows = [json.loads(line) for line in trajectory.read_text().splitlines() if line.strip()] if trajectory.exists() else []
prior_round_count = sum('event' not in row for row in prior_rows)
engine_sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
started = time.monotonic()
completed = subprocess.run(command, cwd=ROOT, env=env, check=False)
rows = []
trajectory = RESULTS / 'loop-rsi-turing-rsi-450/trajectory.json'
if trajectory.exists():
    rows = [json.loads(line) for line in trajectory.read_text().splitlines() if line.strip()]
rounds = [row for row in rows if 'event' not in row][prior_round_count:]
passes = sum(row.get('passed') is True and row.get('void') is False for row in rounds)
latest_accepted = bool(rounds and rounds[-1].get('exit') == 0 and rounds[-1].get('passed') is True and rounds[-1].get('void') is False)
source_diff = subprocess.check_output(['git', 'diff', '--binary', 'HEAD', '--', 'src', 'tests'], cwd=CANDIDATE)
source_identity = hashlib.sha256(source_diff)
untracked = subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard', '--', 'src', 'tests'], cwd=CANDIDATE, text=True).splitlines()
for rel in sorted(untracked):
    source_identity.update(rel.encode()); source_identity.update((CANDIDATE / rel).read_bytes())
checker = ROOT / 'research/records/rsi-live-450/test_external_trajectory.py'
metrics = {'completed_rounds': len(rounds), 'verifier_passes': passes,
           'latest_round_accepted': latest_accepted, 'engine_sha': engine_sha,
           'candidate_source_diff_sha256': source_identity.hexdigest(),
           'void_rounds': sum(row.get('void') is True for row in rounds),
           'wall_seconds': time.monotonic() - started,
           'seed': 'not-applicable: provider seed unavailable', 'n_examples': 2,
           'eval_set': 'rsi-trajectory-integrity-external-v1',
           'eval_set_sha256': hashlib.sha256(checker.read_bytes()).hexdigest(),
           'model': 'gpt-5.6-luna', 'reasoning_effort': 'xhigh',
           'cost_usd': 'unknown: subscription usage not metered by harness',
           'candidate_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=CANDIDATE, text=True).strip()}
(RUN / 'metrics.json').write_text(json.dumps(metrics, indent=2) + '\n')
(RUN / 'notes.md').write_text('# Turing live RSI source improvement\n\n'
    'Question: can a real Codex/Luna RSI round repair trajectory evidence corruption in Turing?\n\n'
    f'Observed verifier passes: {passes}; process exit: {completed.returncode}. '
    'Independent post-run review and holdout probes are still required. '
    'This is a bounded engineering test, not evidence of causal scaffold self-improvement.\n')
print(json.dumps(metrics), flush=True)
sys.exit(completed.returncode if completed.returncode else (0 if latest_accepted else 1))
