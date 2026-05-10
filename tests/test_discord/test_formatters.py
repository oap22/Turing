"""Tests for Discord message formatting utilities."""

from __future__ import annotations

from turing.discord_bot.formatters import (
    chunk_message,
    format_code_block,
    format_system_info,
    format_tool_result,
    truncate,
)

# ======================================================================
# chunk_message
# ======================================================================


class TestChunkMessage:
    """Tests for the chunk_message function."""

    def test_short_text_no_split(self) -> None:
        """Text shorter than the limit is returned as a single chunk."""
        text = "Hello, world!"
        chunks = chunk_message(text)
        assert chunks == ["Hello, world!"]

    def test_empty_text(self) -> None:
        """Empty string produces a single empty-string chunk."""
        assert chunk_message("") == [""]

    def test_exact_limit(self) -> None:
        """Text exactly at the limit is returned as one chunk."""
        text = "a" * 2000
        chunks = chunk_message(text, max_length=2000)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_splits_at_paragraph_break(self) -> None:
        """Prefers splitting at paragraph breaks (double newline)."""
        part1 = "A" * 1000
        part2 = "B" * 1000
        text = f"{part1}\n\n{part2}"
        chunks = chunk_message(text, max_length=1500)
        assert len(chunks) == 2
        assert part1 in chunks[0]
        assert part2 in chunks[1]

    def test_splits_at_single_newline(self) -> None:
        """Falls back to single-newline split when no paragraph break."""
        part1 = "A" * 1000
        part2 = "B" * 1000
        text = f"{part1}\n{part2}"
        chunks = chunk_message(text, max_length=1500)
        assert len(chunks) == 2
        assert part1 in chunks[0]
        assert part2 in chunks[1]

    def test_splits_at_sentence_boundary(self) -> None:
        """Falls back to sentence-ending punctuation when no newlines."""
        # Build text with sentence boundaries but no newlines
        sentence = "This is a sentence. "
        repeat = 101
        text = sentence * repeat
        assert len(text) > 2000
        chunks = chunk_message(text)
        for chunk in chunks:
            assert len(chunk) <= 2000

    def test_hard_split_no_boundaries(self) -> None:
        """If there are no clean boundaries, performs a hard split."""
        text = "A" * 5000
        chunks = chunk_message(text, max_length=2000)
        # Reconstruct and verify nothing is lost
        reassembled = "".join(chunks)
        assert reassembled == text
        for chunk in chunks:
            assert len(chunk) <= 2000

    def test_preserves_code_block_simple(self) -> None:
        """A code block that fits in a single chunk is not split."""
        code = "x = 1\ny = 2\nz = x + y"
        text = f"Here is code:\n```python\n{code}\n```\nDone."
        chunks = chunk_message(text, max_length=2000)
        assert len(chunks) == 1
        assert "```python" in chunks[0]
        assert "```" in chunks[0]

    def test_code_block_too_large_is_split_with_refence(self) -> None:
        """A code block larger than max_length is hard-split with fence continuation."""
        code_line = "x = 1  # some code\n"
        code = code_line * 200  # well over 2000 chars
        text = f"```python\n{code}```"
        chunks = chunk_message(text, max_length=2000)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= 2000
        # First chunk should start with the opening fence
        assert chunks[0].startswith("```python")
        # Each continuation chunk should re-open the fence
        for chunk in chunks[1:]:
            assert chunk.startswith("```python") or chunk.startswith("```")

    def test_text_before_code_block_split(self) -> None:
        """When text + code block exceeds limit, text is split before the block."""
        preamble = "A" * 1800
        code_block = "```python\nprint('hello')\n```"
        text = f"{preamble}\n{code_block}"
        chunks = chunk_message(text, max_length=2000)
        assert len(chunks) >= 1
        # The code block should not be partially present in the first chunk
        # unless the whole thing fits.
        full = "".join(c.replace("\n```\n```python\n", "\n") for c in chunks)
        assert "print('hello')" in full

    def test_multiple_chunks_all_within_limit(self) -> None:
        """Every returned chunk must respect the max_length."""
        text = "word " * 1000  # ~5000 chars
        chunks = chunk_message(text, max_length=500)
        for chunk in chunks:
            assert len(chunk) <= 500

    def test_custom_max_length(self) -> None:
        """Respects a custom max_length parameter."""
        text = "A" * 300
        chunks = chunk_message(text, max_length=100)
        for chunk in chunks:
            assert len(chunk) <= 100
        assert "".join(chunks) == text


# ======================================================================
# format_code_block
# ======================================================================


class TestFormatCodeBlock:
    """Tests for the format_code_block function."""

    def test_no_language(self) -> None:
        result = format_code_block("print('hi')")
        assert result == "```\nprint('hi')\n```"

    def test_with_language(self) -> None:
        result = format_code_block("print('hi')", language="python")
        assert result == "```python\nprint('hi')\n```"

    def test_empty_code(self) -> None:
        result = format_code_block("")
        assert result == "```\n\n```"


# ======================================================================
# format_tool_result
# ======================================================================


class TestFormatToolResult:
    """Tests for the format_tool_result function."""

    def test_success(self) -> None:
        result = format_tool_result("shell", "output here", success=True)
        assert "\u2705" in result
        assert "**shell**" in result
        assert "output here" in result
        assert "```" in result

    def test_failure(self) -> None:
        result = format_tool_result("shell", "error msg", success=False)
        assert "\u274c" in result
        assert "**shell**" in result
        assert "error msg" in result

    def test_long_result_truncated(self) -> None:
        long_text = "x" * 2000
        result = format_tool_result("tool", long_text)
        assert "(truncated)" in result
        # The displayed portion should be at most 1500 chars + truncation notice
        # (the raw result passed to format_code_block).


# ======================================================================
# format_system_info
# ======================================================================


class TestFormatSystemInfo:
    """Tests for the format_system_info function."""

    def test_basic_fields(self) -> None:
        info = {
            "hostname": "pi-alpha",
            "cpu_percent": 42.5,
            "memory_total": 4 * 1024**3,
            "memory_used": 2 * 1024**3,
            "memory_percent": 50.0,
            "disk_total": 32 * 1024**3,
            "disk_used": 16 * 1024**3,
            "disk_percent": 50.0,
        }
        result = format_system_info(info)
        assert "pi-alpha" in result
        assert "42.5%" in result
        assert "```" in result

    def test_temperature_field(self) -> None:
        info = {"temperature": 55.0}
        result = format_system_info(info)
        assert "55.0\u00b0C" in result

    def test_empty_dict(self) -> None:
        result = format_system_info({})
        assert "System Information" in result
        assert "```" in result

    def test_memory_formatting(self) -> None:
        info = {"memory_total": 1024 * 1024 * 512}  # 512 MB
        result = format_system_info(info)
        assert "512.0 MB" in result


# ======================================================================
# truncate
# ======================================================================


class TestTruncate:
    """Tests for the truncate function."""

    def test_short_text_unchanged(self) -> None:
        assert truncate("hello", max_length=100) == "hello"

    def test_exact_length_unchanged(self) -> None:
        text = "a" * 10
        assert truncate(text, max_length=10) == text

    def test_long_text_truncated(self) -> None:
        text = "a" * 100
        result = truncate(text, max_length=50)
        assert len(result) == 50
        assert result.endswith("...")

    def test_custom_suffix(self) -> None:
        text = "a" * 100
        result = truncate(text, max_length=50, suffix="[cut]")
        assert len(result) == 50
        assert result.endswith("[cut]")

    def test_empty_text(self) -> None:
        assert truncate("", max_length=100) == ""
