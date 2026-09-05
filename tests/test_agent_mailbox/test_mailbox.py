from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from turing.agent_mailbox import Mailbox, MailboxError

if TYPE_CHECKING:
    from pathlib import Path


def _register(path: str, agent: str) -> None:
    mailbox = Mailbox(path, "workflow", agent)
    mailbox.register(provider="test")


def test_send_reply_poll_ack_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "mailbox.db"
    alice = Mailbox(path, "workflow", "alice")
    bob = Mailbox(path, "workflow", "bob")
    alice.register(provider="codex")
    bob.register(provider="claude")

    sent = alice.send(
        "bob",
        "Please inspect this result",
        kind="finding",
        data={"nested": {"answer": [1, True, None]}},
        idempotency_key="result-1",
    )
    assert sent["sequence"] == 1
    assert bob.inbox() == [sent]
    assert bob.inbox() == [sent]
    assert bob.ack(sent["message_id"])["acknowledged"] is True
    assert bob.inbox() == []

    restarted = Mailbox(path, "workflow", "bob")
    reply = restarted.send("alice", "Done", reply_to=sent["message_id"])
    assert reply["sequence"] == 2
    assert Mailbox(path, "workflow", "alice").inbox()[0]["reply_to"] == sent["message_id"]


def test_idempotency_is_sender_scoped_and_conflicts_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "mailbox.db"
    alice = Mailbox(path, "workflow", "alice")
    bob = Mailbox(path, "workflow", "bob")
    alice.register()
    bob.register()
    first = alice.send("bob", "same", idempotency_key="retry")
    assert alice.send("bob", "same", idempotency_key="retry") == first
    with pytest.raises(MailboxError, match="different content"):
        alice.send("bob", "changed", idempotency_key="retry")
    # A different sender may use the same key.
    assert bob.send("alice", "same", idempotency_key="retry")["sequence"] == 2


def test_workflows_and_reply_ownership_are_isolated(tmp_path: Path) -> None:
    path = tmp_path / "mailbox.db"
    alice = Mailbox(path, "one", "alice")
    bob = Mailbox(path, "one", "bob")
    other = Mailbox(path, "two", "alice")
    alice.register()
    bob.register()
    other.register()
    message = alice.send("bob", "hello")
    with pytest.raises(MailboxError, match="does not exist"):
        other.ack(message["message_id"])
    with pytest.raises(MailboxError, match="original message sender"):
        bob.send("bob", "bad reply", reply_to=message["message_id"])
    with pytest.raises(MailboxError, match="addressed to this agent"):
        alice.send("bob", "forged reply", reply_to=message["message_id"])


def test_nested_non_json_values_are_rejected(tmp_path: Path) -> None:
    mailbox = Mailbox(tmp_path / "mailbox.db", "workflow", "alice")
    mailbox.register()
    with pytest.raises(MailboxError, match="keys must be strings"):
        mailbox.send("alice", "x", data={"nested": {1: "lossy"}})
    with pytest.raises(MailboxError, match="non-finite"):
        mailbox.send("alice", "x", data={"value": float("nan")})


def test_dedicated_store_rejects_existing_application_database(tmp_path: Path) -> None:
    path = tmp_path / "application.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE application_state (value TEXT)")
    connection.commit()
    connection.close()
    with pytest.raises(MailboxError, match="dedicated mailbox"):
        Mailbox(path, "workflow", "alice")


@pytest.mark.parametrize("statement", ["PRAGMA user_version=7", "PRAGMA application_id=1933"])
def test_rejects_sqlite_metadata_without_mutating_existing_file(
    tmp_path: Path, statement: str
) -> None:
    path = tmp_path / "metadata.db"
    with sqlite3.connect(path) as connection:
        connection.execute(statement)
    before = path.read_bytes()

    with pytest.raises(MailboxError, match="separate mailbox database"):
        Mailbox(path, "workflow", "alice")

    assert path.read_bytes() == before


def test_rejects_sqlite_sequence_residue_without_mutating_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "residue.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE old(id INTEGER PRIMARY KEY AUTOINCREMENT)")
        connection.execute("DROP TABLE old")
    before = path.read_bytes()

    with pytest.raises(MailboxError, match="empty dedicated mailbox store"):
        Mailbox(path, "workflow", "alice")

    assert path.read_bytes() == before


def test_rejects_incomplete_mailbox_marker_without_repairing_file(tmp_path: Path) -> None:
    path = tmp_path / "marker-only.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE mailbox_schema "
            "(id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL)"
        )
        connection.execute("INSERT INTO mailbox_schema VALUES (1, 1)")
    before = path.read_bytes()

    with pytest.raises(MailboxError, match="incomplete"):
        Mailbox(path, "workflow", "alice")

    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall() == [("mailbox_schema",)]


def test_rejects_extra_schema_constraint_without_mutating_file(tmp_path: Path) -> None:
    path = tmp_path / "extra-constraint.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE mailbox_schema
                (id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL);
            CREATE TABLE mailbox_workflows (
                workflow TEXT PRIMARY KEY, created_at TEXT NOT NULL
            );
            CREATE TABLE mailbox_registrations (
                workflow TEXT NOT NULL, agent TEXT NOT NULL, provider TEXT NOT NULL,
                registered_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (workflow, agent),
                FOREIGN KEY (workflow) REFERENCES mailbox_workflows(workflow)
            );
            CREATE TABLE mailbox_sequences (
                workflow TEXT PRIMARY KEY,
                next_sequence INTEGER NOT NULL CHECK (next_sequence >= 1)
                    CHECK (next_sequence <> 2),
                FOREIGN KEY (workflow) REFERENCES mailbox_workflows(workflow)
            );
            CREATE TABLE mailbox_messages (
                message_id TEXT PRIMARY KEY, workflow TEXT NOT NULL, sequence INTEGER NOT NULL,
                sender TEXT NOT NULL, recipient TEXT NOT NULL, created_at TEXT NOT NULL,
                kind TEXT NOT NULL, text TEXT NOT NULL, data_json TEXT, reply_to TEXT,
                idempotency_key TEXT, acknowledged_at TEXT,
                UNIQUE (workflow, sequence), UNIQUE (workflow, sender, idempotency_key),
                FOREIGN KEY (workflow, sender) REFERENCES mailbox_registrations(workflow, agent),
                FOREIGN KEY (workflow, recipient) REFERENCES mailbox_registrations(workflow, agent)
            );
            CREATE INDEX mailbox_messages_inbox_idx
              ON mailbox_messages(workflow, recipient, acknowledged_at, sequence);
            INSERT INTO mailbox_schema VALUES (1, 1);
            """
        )
    before = path.read_bytes()

    with pytest.raises(MailboxError, match="unsupported schema"):
        Mailbox(path, "workflow", "alice")

    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "version", [pytest.param(1.9, id="fractional"), pytest.param(float("inf"), id="infinite")]
)
def test_schema_version_requires_exact_integer(tmp_path: Path, version: float) -> None:
    path = tmp_path / "version.db"
    Mailbox(path, "workflow", "alice")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE mailbox_schema SET version = ? WHERE id = 1", (version,))
    before = path.read_bytes()

    with pytest.raises(MailboxError, match="schema version"):
        Mailbox(path, "workflow", "alice")

    assert path.read_bytes() == before


def test_in_memory_database_is_rejected() -> None:
    with pytest.raises(MailboxError, match="durable file"):
        Mailbox(":memory:", "workflow", "alice")


def test_database_path_is_bound_across_working_directory_changes(
    tmp_path: Path, monkeypatch
) -> None:
    original = tmp_path / "original"
    alternate = tmp_path / "alternate"
    original.mkdir()
    alternate.mkdir()
    monkeypatch.chdir(original)
    mailbox = Mailbox("mailbox.db", "workflow", "alice")
    mailbox.register()
    monkeypatch.chdir(alternate)
    assert mailbox.peers()[0]["agent"] == "alice"
    assert (original / "mailbox.db").exists()
    assert not (alternate / "mailbox.db").exists()


def test_concurrent_processes_keep_unique_sequences(tmp_path: Path) -> None:
    path = str(tmp_path / "mailbox.db")
    _register(path, "alice")
    _register(path, "bob")
    context = multiprocessing.get_context("spawn")
    processes = [context.Process(target=_send_many, args=(path, "alice", 20))]
    processes += [context.Process(target=_send_many, args=(path, "bob", 20))]
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
        assert process.exitcode == 0
    messages = Mailbox(path, "workflow", "alice").inbox(100)
    messages += Mailbox(path, "workflow", "bob").inbox(100)
    assert len(messages) == 40
    assert sorted(message["sequence"] for message in messages) == list(range(1, 41))


def test_concurrent_first_initialization_is_safe(tmp_path: Path) -> None:
    path = str(tmp_path / "mailbox.db")
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_register, args=(path, f"agent-{index}")) for index in range(4)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
        assert process.exitcode == 0
    assert [peer["agent"] for peer in Mailbox(path, "workflow", "agent-0").peers()] == [
        "agent-0",
        "agent-1",
        "agent-2",
        "agent-3",
    ]


def _send_many(path: str, sender: str, count: int) -> None:
    mailbox = Mailbox(path, "workflow", sender)
    recipient = "bob" if sender == "alice" else "alice"
    for index in range(count):
        mailbox.send(recipient, f"{sender}-{index}")


def test_cli_round_trip_and_json_errors(tmp_path: Path) -> None:
    path = str(tmp_path / "mailbox.db")
    env = {**os.environ, "PYTHONPATH": "src"}

    def run(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "turing.agent_mailbox",
                "--db",
                path,
                "--workflow",
                "workflow",
                "--agent",
                "alice",
                *args,
            ],
            input=input_text,
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    assert run("register", "--provider", "codex").returncode == 0
    bob_command = [
        sys.executable,
        "-m",
        "turing.agent_mailbox",
        "--db",
        path,
        "--workflow",
        "workflow",
        "--agent",
        "bob",
        "register",
    ]
    assert subprocess.run(bob_command, text=True, capture_output=True, env=env).returncode == 0
    sent = run("send", "--to", "bob", "--text", "hi", "--data-stdin", input_text='{"x":[1,true]}')
    assert sent.returncode == 0
    payload = json.loads(sent.stdout)
    assert payload["data"] == {"x": [1, True]}
    assert run("send", "--to", "bob", "--text", "bad", "--data", "[]").returncode == 1
    error = json.loads(run("send", "--to", "bob", "--text", "bad", "--data", "{").stdout)
    assert error["error_type"] == "MailboxError"
    deeply_nested = "{" + '"x":{' * 1100 + "null" + "}" * 1100 + "}"
    deep_error = run("send", "--to", "bob", "--text", "bad", "--data", deeply_nested)
    assert deep_error.returncode == 1
    assert json.loads(deep_error.stdout)["error_type"] == "MailboxError"

    duplicate = run(
        "send",
        "--to",
        "bob",
        "--text",
        "duplicate",
        "--data",
        '{"x":1,"x":2}',
    )
    assert duplicate.returncode == 1
    assert "duplicate object key" in json.loads(duplicate.stdout)["error"]
    nested_duplicate = run(
        "send",
        "--to",
        "bob",
        "--text",
        "duplicate",
        "--data-stdin",
        input_text='{"outer":{"x":1,"x":2}}',
    )
    assert nested_duplicate.returncode == 1
    assert "duplicate object key" in json.loads(nested_duplicate.stdout)["error"]
    assert len(Mailbox(path, "workflow", "bob").inbox()) == 1
