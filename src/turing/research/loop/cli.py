"""``python -m turing.research.loop.cli`` — answer an open escalation.

The operator's entire vocabulary::

    python -m turing.research.loop.cli --loop-dir <dir> <request_id> continue
    python -m turing.research.loop.cli --loop-dir <dir> <request_id> abandon
    python -m turing.research.loop.cli --loop-dir <dir> <request_id> extend_cap \\
        --extra-steps 200 --extra-tokens 500000 --extra-wall-clock 3600

``--list`` shows the requests waiting in that directory.

There is no ``--note`` and there will never be one. Guidance ("try gradient
boosting on that one") would make the operator the improvement mechanism,
confound the next round's delta, and turn human-gate load into a measure of the
operator's ML knowledge rather than the scaffold's autonomy. The CLI writes
exactly the schema :func:`~turing.research.loop.escalation.decode_decision`
accepts, and that decoder rejects anything else — including a decision file
hand-edited to carry advice.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from turing.research.contracts import (
    CapExtension,
    ContractViolationError,
    EscalationDecision,
    EscalationVerdict,
)
from turing.research.loop.escalation import (
    DECISION_SUFFIX,
    REQUEST_SUFFIX,
    decision_filename,
    encode_decision,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["build_parser", "main", "write_decision"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="turing-research-decide",
        description="Answer a Turing research-loop escalation: continue | abandon | extend_cap",
        epilog="Free-form guidance is not part of the protocol and cannot be supplied.",
    )
    parser.add_argument(
        "--loop-dir",
        type=Path,
        required=True,
        help="directory the loop writes escalations to (…/round-NN/escalations)",
    )
    parser.add_argument("request_id", nargs="?", help="the escalation to answer")
    parser.add_argument(
        "verdict",
        nargs="?",
        choices=sorted(v.value for v in EscalationVerdict),
        help="the operator's decision",
    )
    parser.add_argument("--extra-steps", type=int, default=0)
    parser.add_argument("--extra-tokens", type=int, default=0)
    parser.add_argument("--extra-wall-clock", type=float, default=0.0, dest="extra_wall_clock")
    parser.add_argument(
        "--list",
        action="store_true",
        help="list unanswered escalations in --loop-dir and exit",
    )
    return parser


def write_decision(directory: Path, decision: EscalationDecision) -> Path:
    path = directory / decision_filename(decision.request_id)
    directory.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(
        json.dumps(encode_decision(decision), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)
    return path


def _open_requests(directory: Path) -> list[tuple[str, dict[str, object]]]:
    out: list[tuple[str, dict[str, object]]] = []
    for path in sorted(directory.glob(f"*{REQUEST_SUFFIX}")):
        request_id = path.name[: -len(REQUEST_SUFFIX)]
        if (directory / f"{request_id}{DECISION_SUFFIX}").exists():
            continue
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            body = {}
        out.append((request_id, body if isinstance(body, dict) else {}))
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    directory: Path = args.loop_dir

    if args.list:
        pending = _open_requests(directory)
        if not pending:
            sys.stdout.write(f"no unanswered escalations in {directory}\n")
            return 0
        for request_id, body in pending:
            sys.stdout.write(
                f"{request_id}  problem={body.get('problem_id')}  "
                f"reason={body.get('reason')}\n    {body.get('summary')}\n"
            )
        return 0

    if not args.request_id or not args.verdict:
        parser.error("request_id and verdict are required unless --list is given")

    verdict = EscalationVerdict(args.verdict)
    extension: CapExtension | None = None
    if verdict is EscalationVerdict.EXTEND_CAP:
        try:
            extension = CapExtension(
                extra_steps=args.extra_steps,
                extra_tokens=args.extra_tokens,
                extra_wall_clock_seconds=args.extra_wall_clock,
            )
        except ContractViolationError as exc:
            sys.stderr.write(f"{exc}\n")
            return 2
    try:
        decision = EscalationDecision(
            request_id=args.request_id,
            verdict=verdict,
            decided_at_ms=int(time.time() * 1000),
            cap_extension=extension,
        )
    except ContractViolationError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    path = write_decision(directory, decision)
    sys.stdout.write(f"wrote {verdict.value} for {args.request_id} -> {path}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
