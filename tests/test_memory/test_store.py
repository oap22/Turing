"""Tests for the MemoryStore using in-memory SQLite."""

from __future__ import annotations

import json

import pytest

from turing.memory.store import MemoryStore


@pytest.fixture
async def store() -> MemoryStore:
    """Create an in-memory MemoryStore for testing."""
    s = MemoryStore(":memory:")
    await s.initialize()
    yield s  # type: ignore[misc]
    await s.close()


# ── table creation ───────────────────────────────────────────────────


class TestInitialization:
    @pytest.mark.asyncio
    async def test_tables_created(self, store: MemoryStore) -> None:
        cursor = await store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        rows = await cursor.fetchall()
        table_names = sorted(row[0] for row in rows)
        expected = sorted([
            "conversations",
            "messages",
            "facts",
            "user_preferences",
            "task_outcomes",
            "audit_log",
        ])
        for name in expected:
            assert name in table_names, f"Missing table: {name}"

    @pytest.mark.asyncio
    async def test_double_initialize_is_safe(self, store: MemoryStore) -> None:
        """Calling initialize() again should not fail or lose data."""
        await store.add_message(
            conversation_id=(await store.create_conversation("ch1")),
            role="user",
            content="test",
        )
        await store.initialize()  # re-run
        # Data should still be accessible (tables use IF NOT EXISTS)
        msgs = await store.search_messages("test")
        assert len(msgs) >= 1


# ── conversations ────────────────────────────────────────────────────


class TestConversations:
    @pytest.mark.asyncio
    async def test_create_and_get(self, store: MemoryStore) -> None:
        conv_id = await store.create_conversation("general")
        conv = await store.get_conversation(conv_id)
        assert conv is not None
        assert conv["channel_id"] == "general"
        assert conv["id"] == conv_id

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, store: MemoryStore) -> None:
        assert await store.get_conversation("nonexistent") is None

    @pytest.mark.asyncio
    async def test_get_active_conversation(self, store: MemoryStore) -> None:
        await store.create_conversation("chan1")
        second = await store.create_conversation("chan1")
        active = await store.get_active_conversation("chan1")
        assert active is not None
        assert active["id"] == second

    @pytest.mark.asyncio
    async def test_update_summary(self, store: MemoryStore) -> None:
        conv_id = await store.create_conversation("ch")
        await store.update_conversation_summary(conv_id, "A summary.")
        conv = await store.get_conversation(conv_id)
        assert conv is not None
        assert conv["summary"] == "A summary."

    @pytest.mark.asyncio
    async def test_touch_conversation(self, store: MemoryStore) -> None:
        conv_id = await store.create_conversation("ch")
        conv_before = await store.get_conversation(conv_id)
        assert conv_before is not None
        await store.touch_conversation(conv_id)
        conv_after = await store.get_conversation(conv_id)
        assert conv_after is not None
        assert conv_after["last_message_at"] >= conv_before["last_message_at"]


# ── messages ─────────────────────────────────────────────────────────


class TestMessages:
    @pytest.mark.asyncio
    async def test_add_and_get_messages(self, store: MemoryStore) -> None:
        conv_id = await store.create_conversation("ch")
        msg_id = await store.add_message(conv_id, "user", "Hello", user_id="u1")
        assert isinstance(msg_id, int)

        msgs = await store.get_messages(conv_id)
        assert len(msgs) == 1
        assert msgs[0]["content"] == "Hello"
        assert msgs[0]["role"] == "user"
        assert msgs[0]["user_id"] == "u1"

    @pytest.mark.asyncio
    async def test_multiple_messages_ordering(self, store: MemoryStore) -> None:
        conv_id = await store.create_conversation("ch")
        await store.add_message(conv_id, "user", "first")
        await store.add_message(conv_id, "assistant", "second")
        await store.add_message(conv_id, "user", "third")

        msgs = await store.get_messages(conv_id)
        assert [m["content"] for m in msgs] == ["first", "second", "third"]

    @pytest.mark.asyncio
    async def test_get_message_by_id(self, store: MemoryStore) -> None:
        conv_id = await store.create_conversation("ch")
        msg_id = await store.add_message(conv_id, "user", "specific")
        msg = await store.get_message_by_id(msg_id)
        assert msg is not None
        assert msg["content"] == "specific"

    @pytest.mark.asyncio
    async def test_get_message_by_id_nonexistent(self, store: MemoryStore) -> None:
        assert await store.get_message_by_id(999999) is None

    @pytest.mark.asyncio
    async def test_get_recent_messages(self, store: MemoryStore) -> None:
        conv1 = await store.create_conversation("ch")
        conv2 = await store.create_conversation("ch")
        await store.add_message(conv1, "user", "old")
        await store.add_message(conv2, "user", "new")

        recent = await store.get_recent_messages("ch", limit=5)
        assert len(recent) == 2
        # Should be in chronological order
        assert recent[0]["content"] == "old"
        assert recent[1]["content"] == "new"

    @pytest.mark.asyncio
    async def test_get_recent_messages_limit(self, store: MemoryStore) -> None:
        conv_id = await store.create_conversation("ch")
        for i in range(10):
            await store.add_message(conv_id, "user", f"msg-{i}")
        recent = await store.get_recent_messages("ch", limit=3)
        assert len(recent) == 3

    @pytest.mark.asyncio
    async def test_search_messages(self, store: MemoryStore) -> None:
        conv_id = await store.create_conversation("ch")
        await store.add_message(conv_id, "user", "The weather is sunny")
        await store.add_message(conv_id, "user", "Install numpy please")
        results = await store.search_messages("sunny")
        assert len(results) == 1
        assert results[0]["content"] == "The weather is sunny"

    @pytest.mark.asyncio
    async def test_add_message_updates_conversation_timestamp(
        self, store: MemoryStore
    ) -> None:
        conv_id = await store.create_conversation("ch")
        conv_before = await store.get_conversation(conv_id)
        assert conv_before is not None
        await store.add_message(conv_id, "user", "bump")
        conv_after = await store.get_conversation(conv_id)
        assert conv_after is not None
        assert conv_after["last_message_at"] >= conv_before["last_message_at"]


# ── facts ────────────────────────────────────────────────────────────


class TestFacts:
    @pytest.mark.asyncio
    async def test_add_and_get_fact(self, store: MemoryStore) -> None:
        fact_id = await store.add_fact("Python", "is", "a programming language")
        fact = await store.get_fact(fact_id)
        assert fact is not None
        assert fact["subject"] == "Python"
        assert fact["predicate"] == "is"
        assert fact["object"] == "a programming language"
        assert fact["confidence"] == 1.0

    @pytest.mark.asyncio
    async def test_update_fact(self, store: MemoryStore) -> None:
        fact_id = await store.add_fact("Pi", "has_ram", "4GB")
        await store.update_fact(fact_id, obj="8GB", confidence=0.9)
        fact = await store.get_fact(fact_id)
        assert fact is not None
        assert fact["object"] == "8GB"
        assert fact["confidence"] == 0.9

    @pytest.mark.asyncio
    async def test_delete_fact(self, store: MemoryStore) -> None:
        fact_id = await store.add_fact("temp", "is", "temporary")
        await store.delete_fact(fact_id)
        assert await store.get_fact(fact_id) is None

    @pytest.mark.asyncio
    async def test_search_facts(self, store: MemoryStore) -> None:
        await store.add_fact("Python", "is", "interpreted")
        await store.add_fact("Rust", "is", "compiled")
        results = await store.search_facts("Python")
        assert len(results) == 1
        assert results[0]["subject"] == "Python"

    @pytest.mark.asyncio
    async def test_search_facts_by_object(self, store: MemoryStore) -> None:
        await store.add_fact("CPU", "temperature", "45C")
        results = await store.search_facts("45C")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_get_nonexistent_fact(self, store: MemoryStore) -> None:
        assert await store.get_fact(999999) is None


# ── user preferences ─────────────────────────────────────────────────


class TestUserPreferences:
    @pytest.mark.asyncio
    async def test_set_and_get_preference(self, store: MemoryStore) -> None:
        await store.set_user_preference("u1", "theme", "dark")
        prefs = await store.get_user_preferences("u1")
        assert prefs == {"theme": "dark"}

    @pytest.mark.asyncio
    async def test_multiple_preferences(self, store: MemoryStore) -> None:
        await store.set_user_preference("u1", "theme", "dark")
        await store.set_user_preference("u1", "language", "en")
        prefs = await store.get_user_preferences("u1")
        assert prefs == {"theme": "dark", "language": "en"}

    @pytest.mark.asyncio
    async def test_upsert_preference(self, store: MemoryStore) -> None:
        await store.set_user_preference("u1", "theme", "light")
        await store.set_user_preference("u1", "theme", "dark")
        prefs = await store.get_user_preferences("u1")
        assert prefs == {"theme": "dark"}

    @pytest.mark.asyncio
    async def test_separate_users(self, store: MemoryStore) -> None:
        await store.set_user_preference("u1", "theme", "dark")
        await store.set_user_preference("u2", "theme", "light")
        assert (await store.get_user_preferences("u1"))["theme"] == "dark"
        assert (await store.get_user_preferences("u2"))["theme"] == "light"

    @pytest.mark.asyncio
    async def test_delete_preference(self, store: MemoryStore) -> None:
        await store.set_user_preference("u1", "theme", "dark")
        await store.delete_user_preference("u1", "theme")
        prefs = await store.get_user_preferences("u1")
        assert prefs == {}

    @pytest.mark.asyncio
    async def test_get_preferences_empty(self, store: MemoryStore) -> None:
        prefs = await store.get_user_preferences("nobody")
        assert prefs == {}


# ── task outcomes ────────────────────────────────────────────────────


class TestTaskOutcomes:
    @pytest.mark.asyncio
    async def test_add_and_query(self, store: MemoryStore) -> None:
        tid = await store.add_task_outcome(
            "deploy app", tool_used="shell", success=True, duration_ms=500
        )
        outcomes = await store.get_task_outcomes()
        assert len(outcomes) >= 1
        assert any(o["task_description"] == "deploy app" for o in outcomes)

    @pytest.mark.asyncio
    async def test_filter_by_tool(self, store: MemoryStore) -> None:
        await store.add_task_outcome("a", tool_used="shell")
        await store.add_task_outcome("b", tool_used="http")
        outcomes = await store.get_task_outcomes(tool_used="shell")
        assert all(o["tool_used"] == "shell" for o in outcomes)

    @pytest.mark.asyncio
    async def test_filter_by_success(self, store: MemoryStore) -> None:
        await store.add_task_outcome("ok", success=True)
        await store.add_task_outcome("fail", success=False, error_message="boom")
        failures = await store.get_task_outcomes(success=False)
        assert len(failures) == 1
        assert failures[0]["error_message"] == "boom"


# ── audit log ────────────────────────────────────────────────────────


class TestAuditLog:
    @pytest.mark.asyncio
    async def test_log_and_query(self, store: MemoryStore) -> None:
        aid = await store.log_audit(
            action="tool_call",
            user_id="u1",
            tool_name="shell",
            arguments={"cmd": "ls"},
            result="success",
            risk_level="medium",
            approved=True,
        )
        assert isinstance(aid, int)
        entries = await store.get_audit_log(user_id="u1")
        assert len(entries) == 1
        assert entries[0]["tool_name"] == "shell"
        assert entries[0]["risk_level"] == "medium"
        assert json.loads(entries[0]["arguments"]) == {"cmd": "ls"}

    @pytest.mark.asyncio
    async def test_filter_by_action(self, store: MemoryStore) -> None:
        await store.log_audit(action="tool_call", user_id="u1")
        await store.log_audit(action="config_change", user_id="u1")
        entries = await store.get_audit_log(action="config_change")
        assert len(entries) == 1
        assert entries[0]["action"] == "config_change"

    @pytest.mark.asyncio
    async def test_audit_log_ordering(self, store: MemoryStore) -> None:
        await store.log_audit(action="first")
        await store.log_audit(action="second")
        entries = await store.get_audit_log()
        # Most recent first
        assert entries[0]["action"] == "second"
        assert entries[1]["action"] == "first"
