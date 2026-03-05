"""Message formatting utilities for Discord output.

Handles message chunking to respect Discord's 2000-character limit,
code block formatting, tool result display, and system info rendering.
"""

from __future__ import annotations

import re


def chunk_message(text: str, max_length: int = 2000) -> list[str]:
    """Split a message into chunks that fit Discord's character limit.

    Splits at clean boundaries in priority order:
    1. Paragraph breaks (double newline)
    2. Single newlines
    3. Sentence boundaries (period/exclamation/question followed by space)
    4. Hard split at ``max_length`` as last resort

    Code blocks (triple-backtick fences) are never split mid-block. If a
    code block alone exceeds ``max_length`` it is hard-split and the fence
    is re-opened in the continuation chunk.
    """
    if not text:
        return [""]

    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break

        # Determine if we are inside any unclosed code blocks at the
        # point where we would need to split.  We scan the candidate
        # window to decide whether naive splitting would break a fence.
        candidate = remaining[:max_length]

        # Count open/close fences in the candidate
        fence_pattern = re.compile(r"```(\w*)")
        raw_fences = list(re.finditer(r"```", candidate))

        # Track whether we are inside a code block at each fence
        open_fence = False
        open_fence_lang = ""
        last_open_pos = -1
        for rf in raw_fences:
            if not open_fence:
                # Opening fence
                open_fence = True
                last_open_pos = rf.start()
                # Try to capture the language tag
                m = fence_pattern.match(remaining, rf.start())
                open_fence_lang = m.group(1) if m else ""
            else:
                # Closing fence
                open_fence = False
                last_open_pos = -1
                open_fence_lang = ""

        if open_fence and last_open_pos >= 0:
            # The candidate ends inside an unclosed code block.
            # Try to find the closing fence beyond the candidate window.
            close_pos = remaining.find("```", last_open_pos + 3)
            if close_pos != -1:
                block_end = close_pos + 3
                # Skip past any trailing newline on the closing fence line
                if block_end < len(remaining) and remaining[block_end] == "\n":
                    block_end += 1

                if block_end <= max_length:
                    # The full block actually fits -- this shouldn't happen
                    # because we only reach here if candidate is shorter, but
                    # handle gracefully.
                    split_at = _find_split_point(remaining, max_length)
                    chunk = remaining[:split_at].rstrip()
                    remaining = remaining[split_at:].lstrip("\n")
                    if chunk:
                        chunks.append(chunk)
                    continue

                # If the entire code block (from its opening fence) fits
                # within max_length, output everything before the block as
                # one chunk, then handle the block in the next iteration.
                if last_open_pos > 0:
                    pre_block = remaining[:last_open_pos].rstrip()
                    if pre_block:
                        chunks.append(pre_block)
                    remaining = remaining[last_open_pos:]
                    continue

                # The code block itself is too large. We must hard-split it.
                # Close the fence in this chunk and re-open in the next.
                # Reserve space for the closing fence.
                split_at = max_length - 4  # room for "\n```"
                chunk = remaining[:split_at] + "\n```"
                remaining = f"```{open_fence_lang}\n" + remaining[split_at:]
                chunks.append(chunk)
                continue

        # No code-block issue -- find a clean split point.
        split_at = _find_split_point(remaining, max_length)
        chunk = remaining[:split_at].rstrip()
        remaining = remaining[split_at:].lstrip("\n")
        if chunk:
            chunks.append(chunk)

    return chunks if chunks else [""]


def _find_split_point(text: str, max_length: int) -> int:
    """Find the best position to split ``text`` at or before ``max_length``.

    Tries split strategies in priority order:
    1. Last paragraph break (double newline)
    2. Last single newline
    3. Last sentence-ending punctuation followed by a space
    4. Last space
    5. Hard split at ``max_length``
    """
    window = text[:max_length]

    # 1. Paragraph break
    pos = window.rfind("\n\n")
    if pos > 0:
        return pos + 2  # include the double-newline in the first chunk

    # 2. Single newline
    pos = window.rfind("\n")
    if pos > 0:
        return pos + 1

    # 3. Sentence boundary (. ! ? followed by space)
    sentence_end = -1
    for pattern in (". ", "! ", "? "):
        idx = window.rfind(pattern)
        if idx > sentence_end:
            sentence_end = idx
    if sentence_end > 0:
        return sentence_end + 2  # include the punctuation and space

    # 4. Last space
    pos = window.rfind(" ")
    if pos > 0:
        return pos + 1

    # 5. Hard split
    return max_length


def format_code_block(code: str, language: str = "") -> str:
    """Wrap code in a Discord markdown code block."""
    return f"```{language}\n{code}\n```"


def format_tool_result(tool_name: str, result: str, success: bool = True) -> str:
    """Format a tool execution result for Discord display.

    Shows a status indicator, the tool name in bold, and the result
    in a code block.  Long results are truncated to 1500 characters.
    """
    status = "\u2705" if success else "\u274c"
    header = f"{status} **{tool_name}**"
    if len(result) > 1500:
        result = result[:1500] + "\n... (truncated)"
    return f"{header}\n{format_code_block(result)}"


def format_system_info(info: dict) -> str:
    """Format system information as a clean embed-like Discord message.

    Expects a dictionary with keys such as ``cpu_percent``, ``memory_total``,
    ``memory_used``, ``memory_percent``, ``disk_total``, ``disk_used``,
    ``disk_percent``, ``temperature``, ``uptime``, and ``hostname``.
    Missing keys are silently skipped.
    """
    lines: list[str] = []
    lines.append("**System Information**")
    lines.append("```")

    field_labels = {
        "hostname": "Hostname",
        "uptime": "Uptime",
        "cpu_percent": "CPU Usage",
        "memory_total": "Memory Total",
        "memory_used": "Memory Used",
        "memory_percent": "Memory Usage",
        "disk_total": "Disk Total",
        "disk_used": "Disk Used",
        "disk_percent": "Disk Usage",
        "temperature": "Temperature",
        "load_avg": "Load Average",
        "platform": "Platform",
    }

    # Determine the longest label for alignment
    present_labels = [label for key, label in field_labels.items() if key in info]
    max_label_len = max((len(label) for label in present_labels), default=0)

    for key, label in field_labels.items():
        if key not in info:
            continue
        value = info[key]

        # Add units or formatting based on field type
        if key == "cpu_percent":
            value = f"{value}%"
        elif key in ("memory_percent", "disk_percent"):
            value = f"{value}%"
        elif key in ("memory_total", "memory_used", "disk_total", "disk_used"):
            value = _format_bytes(value) if isinstance(value, (int, float)) else str(value)
        elif key == "temperature":
            value = f"{value}\u00b0C"

        lines.append(f"  {label:<{max_label_len}}  {value}")

    lines.append("```")
    return "\n".join(lines)


def _format_bytes(num_bytes: int | float) -> str:
    """Format a byte count as a human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} PB"


def truncate(text: str, max_length: int = 1500, suffix: str = "...") -> str:
    """Truncate text to ``max_length``, appending ``suffix`` if truncated."""
    if len(text) <= max_length:
        return text
    return text[: max_length - len(suffix)] + suffix
