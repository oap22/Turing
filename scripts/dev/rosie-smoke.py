#!/usr/bin/env python3
"""Submit or collect one bounded ROSIE infrastructure check; retain remote files."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import uuid
from pathlib import Path


def run(argv: list[str], *, content: str | None = None) -> str:
    return subprocess.run(
        argv,
        input=content,
        text=True,
        capture_output=True,
        check=True,
        timeout=90,
    ).stdout.strip()


def ssh(host: str, command: str, content: str | None = None) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", host):
        raise ValueError("host must be an SSH config alias")
    return run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "ServerAliveInterval=10",
            "-o",
            "ServerAliveCountMax=2",
            host,
            command,
        ],
        content=content,
    )


def validate_record(record: dict) -> None:
    if not re.fullmatch(r"[0-9]+", str(record.get("job_id", ""))):
        raise ValueError("invalid job ID; reconcile submission before collecting")
    if not re.fullmatch(r"[0-9a-f]{32}", record.get("run_id", "")):
        raise ValueError("invalid run ID")
    if not re.fullmatch(r"/[A-Za-z0-9_./@-]+", record.get("remote_dir", "")):
        raise ValueError("invalid remote directory")
    path = Path(record["remote_dir"])
    if ".." in path.parts or path.parts[-3:] != (
        ".turing-workflow-smoke",
        "runs",
        record["run_id"],
    ):
        raise ValueError("remote directory is outside this smoke run")
    if not re.fullmatch(r"[0-9a-f]{64}", record.get("script_sha256", "")):
        raise ValueError("invalid script digest")


def submit(output: Path, host: str) -> None:
    # Refuse reuse, including a submission whose SSH reply was lost.
    output.mkdir(parents=True, exist_ok=False)
    preflight = ssh(
        host,
        "set -e; command -v sbatch sacct rsync python3; hostname; whoami; "
        'df -h /home /data; sinfo -s; squeue -u "$USER"',
    )
    (output / "preflight.txt").write_text(preflight + "\n")
    remote_home = ssh(host, 'printf "%s" "$HOME"')
    if not re.fullmatch(r"/[A-Za-z0-9_./@-]+", remote_home) or ".." in Path(remote_home).parts:
        raise ValueError("unexpected remote home path")
    run_id = uuid.uuid4().hex
    remote = f"{remote_home}/.turing-workflow-smoke/runs/{run_id}"
    script = Path(__file__).with_suffix(".sbatch").read_text()
    record = {
        "host": host,
        "run_id": run_id,
        "remote_dir": remote,
        "script_sha256": hashlib.sha256(script.encode()).hexdigest(),
        "job_id": None,
        "state": "submission-unconfirmed",
        "research_result": False,
    }
    state = output / "submission.json"
    state.write_text(json.dumps(record, indent=2) + "\n")
    quoted = shlex.quote(remote)
    # mkdir without -p on the leaf prevents accidental reuse. Store sbatch's
    # reply remotely too so a broken connection can be reconciled without
    # submitting a duplicate job.
    command = (
        f"mkdir -p {shlex.quote(str(Path(remote).parent))} && mkdir {quoted} && "
        f"cd {quoted} && cat > job.sbatch && "
        "sbatch --parsable job.sbatch > submission.txt && cat submission.txt"
    )
    job_id = ssh(host, command, script).split(";")[0]
    if not re.fullmatch(r"[0-9]+", job_id):
        raise ValueError("unexpected sbatch reply; inspect remote submission.txt before retrying")
    record.update(job_id=job_id, state="submitted")
    state.write_text(json.dumps(record, indent=2) + "\n")
    print(f"Submitted CPU-only job {job_id}; maximum 2 minutes, 0 GPUs, 0.034 node-hours.")
    print(
        f"Collect: python3 {Path(__file__).resolve()} collect --output {shlex.quote(str(output))}"
    )


def accounting_row(text: str, job_id: str) -> list[str]:
    for line in text.splitlines():
        row = line.split("|")
        if len(row) >= 5 and row[0] == job_id:
            return row
    raise ValueError("job is not in accounting yet; collect again later")


_SNAPSHOT_FILES = frozenset({"metadata.json", "stdout.log", "stderr.log", "job.sbatch"})
_REQUIRED_SNAPSHOT_FILES = frozenset({"metadata.json", "job.sbatch"})


def _read_json_object(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value


def _validate_snapshot_files(destination: Path, *, require_marker: bool = True) -> None:
    if destination.is_symlink() or not destination.is_dir():
        raise ValueError("existing collected snapshot is not a directory")
    names = {entry.name for entry in destination.iterdir()}
    expected = _SNAPSHOT_FILES | ({"verified.json"} if require_marker else set())
    if not names.issuperset(_REQUIRED_SNAPSHOT_FILES) or not names.issubset(expected):
        raise ValueError("existing collected snapshot has unexpected files")
    for name in names:
        path = destination / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"collected snapshot file is not a regular file: {name}")


def _validate_metadata(metadata: dict, job_id: str, script_sha256: str) -> None:
    if (
        metadata.get("job_id") != job_id
        or metadata.get("script_sha256") != script_sha256
        or metadata.get("kind") != "infrastructure-smoke"
        or metadata.get("research_result") is not False
        or not isinstance(metadata.get("hostname"), str)
        or not metadata["hostname"]
    ):
        raise ValueError("pulled metadata does not match this submission")


def _verification_record(record: dict, row: list[str], metadata: dict) -> dict:
    return {**record, "state": "verified", "accounting": row, "metadata": metadata}


def _read_verified_snapshot(destination: Path, record: dict) -> tuple[list[str], dict]:
    """Validate the complete marker and files of a previously published snapshot."""
    _validate_snapshot_files(destination)
    marker = _read_json_object(destination / "verified.json", "collected verification marker")
    for key, value in record.items():
        if key != "state" and marker.get(key) != value:
            raise ValueError("existing collected snapshot does not match this submission")
    if marker.get("state") != "verified":
        raise ValueError("existing collected snapshot is not verified")
    required = set(record) | {"state", "accounting", "metadata"}
    if set(marker) != required:
        raise ValueError("existing collected verification marker has unexpected fields")

    row = marker["accounting"]
    if (
        not isinstance(row, list)
        or len(row) < 5
        or row[0] != record["job_id"]
        or row[1] != "COMPLETED"
        or row[2] != "0:0"
    ):
        raise ValueError("existing collected accounting does not verify this submission")

    metadata = marker["metadata"]
    if not isinstance(metadata, dict):
        raise ValueError("existing collected metadata is not an object")
    _validate_metadata(metadata, record["job_id"], record["script_sha256"])
    file_metadata = _read_json_object(destination / "metadata.json", "collected metadata")
    if file_metadata != metadata:
        raise ValueError("collected metadata does not match its verification marker")
    digest = hashlib.sha256((destination / "job.sbatch").read_bytes()).hexdigest()
    if digest != record["script_sha256"]:
        raise ValueError("collected script does not match this submission")
    return row, metadata


def _publish_verification_record(output: Path, verification: dict) -> None:
    """Atomically publish the user-facing marker after the snapshot is safe."""
    rendered = json.dumps(verification, indent=2, allow_nan=False) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=".verified-", dir=output, text=True)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output / "verified.json")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def collect(output: Path) -> None:
    record = json.loads((output / "submission.json").read_text())
    validate_record(record)
    destination = output / "collected"
    if destination.exists():
        row, metadata = _read_verified_snapshot(destination, record)
        if (output / "verified.json").exists():
            raise FileExistsError(f"already collected: {destination}")
        _publish_verification_record(output, _verification_record(record, row, metadata))
        print(f"Verified job {record['job_id']} on {metadata['hostname']}; metadata: {destination}")
        return

    host, remote, job_id = record["host"], record["remote_dir"], record["job_id"]
    accounting = ssh(
        host, f"sacct -j {job_id} --format=JobIDRaw,State,ExitCode,Elapsed,NodeList -Pn"
    )
    (output / "accounting.txt").write_text(accounting + "\n")
    row = accounting_row(accounting, job_id)
    if row[1] != "COMPLETED" or row[2] != "0:0":
        raise ValueError(f"job is {row[1]}, exit {row[2]}; not a verified round trip")
    # Only small named files cross. No deletion, recursive home sync,
    # checkpoint transfer, symlink following, or append-on-reconnect.
    with tempfile.TemporaryDirectory(prefix=".pull-", dir=output) as staging:
        run(
            [
                "rsync",
                "-rt",
                "--timeout=60",
                "-e",
                "ssh -o BatchMode=yes -o ConnectTimeout=5 -o ServerAliveInterval=10 -o ServerAliveCountMax=2",
                "--include=/metadata.json",
                "--include=/stdout.log",
                "--include=/stderr.log",
                "--include=/job.sbatch",
                "--exclude=*",
                f"{host}:{remote}/",
                f"{staging}/",
            ]
        )
        pulled = Path(staging)
        _validate_snapshot_files(pulled, require_marker=False)
        metadata = _read_json_object(pulled / "metadata.json", "pulled metadata")
        digest = hashlib.sha256((pulled / "job.sbatch").read_bytes()).hexdigest()
        _validate_metadata(metadata, job_id, record["script_sha256"])
        if digest != record["script_sha256"]:
            raise ValueError("pulled script does not match this submission")
        verification = _verification_record(record, row, metadata)
        (pulled / "verified.json").write_text(
            json.dumps(verification, indent=2, allow_nan=False) + "\n"
        )
        # An atomic directory rename publishes only the validated snapshot.
        pulled.rename(destination)
    _publish_verification_record(output, verification)
    print(f"Verified job {job_id} on {metadata['hostname']}; metadata: {destination}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["submit", "collect"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--host", default="ROSIE", help="SSH config alias (submit only)")
    args = parser.parse_args()
    try:
        if args.action == "submit":
            submit(args.output.resolve(), args.host)
        else:
            collect(args.output.resolve())
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        details = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else ""
        parser.exit(
            1,
            f"{exc}\n{details or ''}"
            "Retained local/remote state; do not resubmit an uncertain job.\n",
        )


if __name__ == "__main__":
    main()
