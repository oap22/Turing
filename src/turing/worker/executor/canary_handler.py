"""Worker-side `canary_eval` kind handler (issue #118, ADR 0007).

The handler verifies a candidate adapter, loads it at the worker's quantization,
runs the held-out eval set, and returns a scored payload. Loader/evaluator are
injected so tests pass deterministic fakes; production wires the real
`AdapterRegistry.verify` + scorer pipeline.

Result shape (JSON-encoded into the SubtaskDispatch result.output):
  - PASS: {"status": "scored", "score": float, "failed_cases": [...], "error": null}
  - LOAD: {"status": "load_failed", "score": null, "failed_cases": [], "error": str}
  - EVAL: {"status": "eval_failed", "score": null, "failed_cases": [], "error": str}
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from turing.coordinator.dispatch import SubtaskDispatch

# Loader: (manifest_dict, eval_set_path) -> opaque adapter handle.
LoadFn = Callable[[dict[str, Any], str], Awaitable[Any]]
# Eval runner: (adapter_handle, eval_set_path) -> (score, failed_cases).
EvalFn = Callable[[Any, str], Awaitable[tuple[float, list[dict[str, Any]]]]]


def make_canary_handler(*, load_fn: LoadFn, eval_fn: EvalFn):
    """Build a kind handler that returns a JSON-encoded canary result."""

    async def handle(envelope: SubtaskDispatch) -> dict[str, object]:
        body = json.loads(envelope.prompt)
        manifest = body["adapter_manifest"]
        eval_path = body["eval_set_path"]

        try:
            adapter = await load_fn(manifest, eval_path)
        except Exception as exc:
            return _encoded(
                {
                    "status": "load_failed",
                    "score": None,
                    "failed_cases": [],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

        try:
            score, failed_cases = await eval_fn(adapter, eval_path)
        except Exception as exc:
            return _encoded(
                {
                    "status": "eval_failed",
                    "score": None,
                    "failed_cases": [],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

        return _encoded(
            {
                "status": "scored",
                "score": score,
                "failed_cases": list(failed_cases),
                "error": None,
            }
        )

    return handle


def _encoded(payload: dict[str, Any]) -> dict[str, object]:
    return {"status": "COMPLETED", "output": json.dumps(payload)}
