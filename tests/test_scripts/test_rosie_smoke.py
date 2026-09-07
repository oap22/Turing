import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/dev/rosie-smoke.py"
spec = importlib.util.spec_from_file_location("rosie_smoke", SCRIPT)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def record():
    return {
        "host": "ROSIE",
        "job_id": "123",
        "run_id": "a" * 32,
        "remote_dir": "/home/person/.turing-workflow-smoke/runs/" + "a" * 32,
        "script_sha256": "b" * 64,
    }


@pytest.mark.parametrize(
    "key,value",
    [
        ("job_id", "123; touch bad"),
        ("run_id", "../other"),
        ("remote_dir", "/home/person"),
        ("remote_dir", "/../.turing-workflow-smoke/runs/" + "a" * 32),
        ("script_sha256", "wrong"),
    ],
)
def test_rejects_foreign_or_malformed_record(key, value):
    state = record()
    state[key] = value
    with pytest.raises(ValueError):
        smoke.validate_record(state)


def test_accounting_requires_parent_job():
    with pytest.raises(ValueError):
        smoke.accounting_row("123.batch|COMPLETED|0:0|00:01|node1", "123")
    assert smoke.accounting_row("123|FAILED|1:0|00:01|node1", "123")[1] == "FAILED"


@pytest.mark.parametrize(
    "status,code", [("PENDING", "0:0"), ("FAILED", "1:0"), ("COMPLETED", "1:0")]
)
def test_does_not_pull_unfinished_or_failed_job(tmp_path, monkeypatch, status, code):
    (tmp_path / "submission.json").write_text(json.dumps(record()))
    monkeypatch.setattr(smoke, "ssh", lambda *_: f"123|{status}|{code}|00:01|node1")
    monkeypatch.setattr(smoke, "run", lambda *_: pytest.fail("must not pull"))
    with pytest.raises(ValueError):
        smoke.collect(tmp_path)
    assert not (tmp_path / "verified.json").exists()


def test_rejects_ssh_option_host():
    with pytest.raises(ValueError):
        smoke.ssh("-oProxyCommand=evil", "true")


@pytest.mark.parametrize("corrupt", [False, True])
def test_collection_publishes_only_verified_snapshot(tmp_path, monkeypatch, corrupt):
    script = b"test script\n"
    state = record()
    state["script_sha256"] = hashlib.sha256(script).hexdigest()
    (tmp_path / "submission.json").write_text(json.dumps(state))
    monkeypatch.setattr(smoke, "ssh", lambda *_: "123|COMPLETED|0:0|00:01|node1")

    def transfer(argv):
        assert argv[0] == "rsync"
        assert "--delete" not in argv
        path = Path(argv[-1])
        (path / "job.sbatch").write_bytes(script)
        (path / "metadata.json").write_text(
            json.dumps(
                {
                    "job_id": "other" if corrupt else "123",
                    "hostname": "node1",
                    "script_sha256": state["script_sha256"],
                    "kind": "infrastructure-smoke",
                    "research_result": False,
                }
            )
        )
        return ""

    monkeypatch.setattr(smoke, "run", transfer)
    if corrupt:
        with pytest.raises(ValueError, match="does not match"):
            smoke.collect(tmp_path)
        assert not (tmp_path / "collected").exists()
        assert not (tmp_path / "verified.json").exists()
    else:
        smoke.collect(tmp_path)
        assert json.loads((tmp_path / "verified.json").read_text())["state"] == "verified"
        assert (tmp_path / "collected/metadata.json").exists()
        with pytest.raises(FileExistsError):
            smoke.collect(tmp_path)


def test_submission_reuse_never_calls_ssh(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke, "ssh", lambda *_: pytest.fail("must not submit"))
    with pytest.raises(FileExistsError):
        smoke.submit(tmp_path, "ROSIE")


def test_collection_resumes_after_snapshot_publish_crash(tmp_path, monkeypatch):
    script = b"test script\n"
    state = record()
    state["script_sha256"] = hashlib.sha256(script).hexdigest()
    (tmp_path / "submission.json").write_text(json.dumps(state))
    monkeypatch.setattr(smoke, "ssh", lambda *_: "123|COMPLETED|0:0|00:01|node1")

    def transfer(argv):
        path = Path(argv[-1])
        (path / "job.sbatch").write_bytes(script)
        (path / "metadata.json").write_text(
            json.dumps(
                {
                    "job_id": "123",
                    "hostname": "node1",
                    "script_sha256": state["script_sha256"],
                    "kind": "infrastructure-smoke",
                    "research_result": False,
                }
            )
        )
        return ""

    monkeypatch.setattr(smoke, "run", transfer)
    original_publish = smoke._publish_verification_record

    def crash(*_):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(smoke, "_publish_verification_record", crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        smoke.collect(tmp_path)

    assert (tmp_path / "collected/verified.json").exists()
    assert not (tmp_path / "verified.json").exists()

    monkeypatch.setattr(smoke, "_publish_verification_record", original_publish)
    monkeypatch.setattr(smoke, "ssh", lambda *_: pytest.fail("resume must be local"))
    monkeypatch.setattr(smoke, "run", lambda *_: pytest.fail("resume must not pull again"))
    smoke.collect(tmp_path)
    assert json.loads((tmp_path / "verified.json").read_text())["state"] == "verified"


def test_collection_rejects_arbitrary_existing_snapshot(tmp_path, monkeypatch):
    (tmp_path / "submission.json").write_text(json.dumps(record()))
    (tmp_path / "collected").mkdir()
    (tmp_path / "collected/metadata.json").write_text("{}")
    monkeypatch.setattr(smoke, "ssh", lambda *_: pytest.fail("must reject locally"))
    with pytest.raises(ValueError, match="unexpected files"):
        smoke.collect(tmp_path)
