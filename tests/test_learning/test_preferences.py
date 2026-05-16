"""Unit tests for turing.learning.preferences."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from turing.learning.preferences import PreferenceTracker


@pytest.fixture()
def memory_store() -> AsyncMock:
    store = AsyncMock()
    store.get_user_preferences = AsyncMock(return_value={})
    store.set_user_preference = AsyncMock()
    store.delete_user_preference = AsyncMock()
    return store


@pytest.fixture()
def tracker(memory_store: AsyncMock) -> PreferenceTracker:
    return PreferenceTracker(memory_store)


@pytest.mark.asyncio
async def test_get_user_context_empty(tracker: PreferenceTracker, memory_store: AsyncMock) -> None:
    memory_store.get_user_preferences.return_value = {}
    result = await tracker.get_user_context("user-1")
    assert result == ""


@pytest.mark.asyncio
async def test_get_user_context_formats_preferences(
    tracker: PreferenceTracker, memory_store: AsyncMock
) -> None:
    memory_store.get_user_preferences.return_value = {
        "language": "Python",
        "timezone": "UTC",
    }
    result = await tracker.get_user_context("user-1")
    assert result.startswith("User preferences:")
    assert "  - language: Python" in result
    assert "  - timezone: UTC" in result


@pytest.mark.asyncio
async def test_update_preference(tracker: PreferenceTracker, memory_store: AsyncMock) -> None:
    await tracker.update_preference(user_id="user-2", key="lang", value="Rust", user_name="alice")
    memory_store.set_user_preference.assert_awaited_once_with(
        user_id="user-2", key="lang", value="Rust", user_name="alice"
    )


@pytest.mark.asyncio
async def test_update_preference_default_user_name(
    tracker: PreferenceTracker, memory_store: AsyncMock
) -> None:
    await tracker.update_preference(user_id="user-3", key="theme", value="dark")
    memory_store.set_user_preference.assert_awaited_once_with(
        user_id="user-3", key="theme", value="dark", user_name=""
    )


@pytest.mark.asyncio
async def test_delete_preference(tracker: PreferenceTracker, memory_store: AsyncMock) -> None:
    await tracker.delete_preference("user-4", "lang")
    memory_store.delete_user_preference.assert_awaited_once_with("user-4", "lang")


@pytest.mark.asyncio
async def test_get_all_preferences(tracker: PreferenceTracker, memory_store: AsyncMock) -> None:
    memory_store.get_user_preferences.return_value = {"k": "v"}
    result = await tracker.get_all_preferences("user-5")
    assert result == {"k": "v"}
    memory_store.get_user_preferences.assert_awaited_once_with("user-5")
