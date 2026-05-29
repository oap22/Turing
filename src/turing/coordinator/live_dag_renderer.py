"""Live-DAG text renderer (slice 15/26, #17).

Pure rendering: produces the message body that mirrors a DAG's progress as
the underlying state changes. ADR 0010 retired the Discord surface that
originally consumed this; the renderer itself has no surface coupling and is
preserved for the webui DAG view. It is unit-testable without any network
dependency.

The renderer is intentionally deep but pure — given a :class:`LiveDAGView`
snapshot it returns a string. Callers diff/replace the message body each
time the underlying state changes.
"""

from __future__ import annotations

from dataclasses import dataclass

from turing.coordinator.lifecycle.lifecycle import SubtaskState

_PROMPT_PREVIEW_CHARS = 200

_STATE_EMOJI: dict[SubtaskState, str] = {
    SubtaskState.PENDING: "⬜",
    SubtaskState.DISPATCHED: "⬜",
    SubtaskState.RUNNING: "🔄",
    SubtaskState.NEEDS_SUBTASK: "🧩",
    SubtaskState.COMPLETED: "✅",
    SubtaskState.FAILED: "❌",
    SubtaskState.TIMED_OUT: "❌",
    SubtaskState.REJECTED: "❌",
}


@dataclass(frozen=True)
class SubtaskView:
    """Renderable snapshot of one subtask's status."""

    id: str
    title: str
    state: SubtaskState
    worker_id: str = ""
    adapter_version: str = ""


@dataclass(frozen=True)
class LiveDAGView:
    """Renderable snapshot of the whole DAG plus its synthesis."""

    task_id: str
    user_prompt: str
    subtasks: tuple[SubtaskView, ...]
    synthesis_state: SubtaskState | None = None
    synthesis_text: str = ""


def _emoji(state: SubtaskState) -> str:
    return _STATE_EMOJI.get(state, "⬜")


def _format_subtask(sv: SubtaskView) -> str:
    parts = [sv.title]
    if sv.worker_id:
        parts.append(sv.worker_id)
    if sv.adapter_version:
        parts.append(f"adapter@{sv.adapter_version}")
    detail = " · ".join(parts)
    return f"- {_emoji(sv.state)} `{sv.id}` ({detail})"


def render_live_dag(view: LiveDAGView) -> str:
    """Render a :class:`LiveDAGView` to a message body."""
    prompt = view.user_prompt.strip()
    if len(prompt) > _PROMPT_PREVIEW_CHARS:
        prompt = prompt[: _PROMPT_PREVIEW_CHARS - 1] + "…"

    lines: list[str] = [
        f"**Task** `{view.task_id}` — *{prompt}*",
        "",
    ]
    lines.extend(_format_subtask(sv) for sv in view.subtasks)
    lines.append("")
    lines.append("---")

    if view.synthesis_state is SubtaskState.COMPLETED and view.synthesis_text:
        lines.append(view.synthesis_text)
    else:
        lines.append("_synthesis pending…_")

    return "\n".join(lines)
