#!/bin/bash
# Infrastructure check derived from rosie-run/templates/job.sbatch.
# This is not a research experiment. No source checkout or model is needed.
#SBATCH --job-name=turing-ssh-smoke
#SBATCH --partition=teaching
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=128M
#SBATCH --time=00:02:00
#SBATCH --output=stdout.log
#SBATCH --error=stderr.log
set -euo pipefail
python3 - <<'PY'
import hashlib
import json
import os
import platform
import socket
from pathlib import Path

record = {
    "kind": "infrastructure-smoke",
    "research_result": False,
    "job_id": os.environ["SLURM_JOB_ID"],
    "hostname": socket.gethostname(),
    "python": platform.python_version(),
    "script_sha256": hashlib.sha256(Path("job.sbatch").read_bytes()).hexdigest(),
}
Path("metadata.json").write_text(json.dumps(record, indent=2) + "\n")
print(json.dumps(record))
PY
