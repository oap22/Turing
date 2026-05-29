"""Regression test for #68: SafetyGate.log_action must persist to the audit log.

Pre-fix, ``log_action`` called ``MemoryStore.log_audit(timestamp=…)`` with a
keyword the store didn't accept; the resulting ``TypeError`` was caught and
logged silently, so every tool execution dropped its audit row.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from turing.agent.safety import SafetyGate
from turing.memory.store import MemoryStore


@pytest.mark.asyncio
async def test_log_action_persists_audit_row(temp_db) -> None:
    store = MemoryStore(str(temp_db))
    await store.initialize()
    try:
        gate = SafetyGate(config=MagicMock(admin_user_ids=[]), audit_store=store)
        await gate.log_action(
            user_id="u-1",
            tool_name="shell",
            arguments={"command": "echo hi"},
            result="hi\n",
            risk_level="medium",
            approved=True,
        )

        rows = await store.get_audit_log(user_id="u-1")
        assert len(rows) == 1
        row = rows[0]
        assert row["tool_name"] == "shell"
        assert row["risk_level"] == "medium"
        assert row["approved"] in (1, True)
    finally:
        await store.db.close()


@pytest.mark.asyncio
async def test_log_action_does_not_emit_audit_store_error(
    temp_db, caplog: pytest.LogCaptureFixture
) -> None:
    """The structlog ``audit_store_error`` line was the bug's smoking gun."""
    store = MemoryStore(str(temp_db))
    await store.initialize()
    try:
        gate = SafetyGate(config=MagicMock(admin_user_ids=[]), audit_store=store)
        with caplog.at_level("ERROR"):
            await gate.log_action(
                user_id="u-2",
                tool_name="filesystem",
                arguments={"path": "/tmp/x"},
                result="ok",
                risk_level="low",
                approved=True,
            )
        for record in caplog.records:
            assert "audit_store_error" not in str(record.msg)
    finally:
        await store.db.close()
