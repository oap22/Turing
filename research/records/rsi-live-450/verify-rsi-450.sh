#!/bin/sh
set -eu
export PYTHONPATH="$PWD/src"
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_SYSTEM=/dev/null
/Users/owenpacetti/Developer/active/Turing/.venv/bin/python -c 'import turing; print("candidate_source=" + turing.__file__)'
/Users/owenpacetti/Developer/active/Turing/.venv/bin/python -m pytest -c /Users/owenpacetti/Developer/active/Turing/.worktrees/450-verifier-control/pyproject.toml /Users/owenpacetti/Developer/active/Turing/.worktrees/450-rsi-live-test/research/records/rsi-live-450/test_external_trajectory.py /Users/owenpacetti/Developer/active/Turing/.worktrees/450-verifier-control/tests/test_research/test_rsi -q --disable-warnings
printf 'score=1\n'
