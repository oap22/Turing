"""JSON command-line interface for :mod:`turing.agent_mailbox`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, NoReturn

from turing.agent_mailbox import Mailbox, MailboxError


class _UsageError(Exception):
    """An argparse failure that should be rendered as a JSON error."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for key, value in pairs:
        if key in parsed:
            raise MailboxError(f"--data contains duplicate object key {key!r}")
        parsed[key] = value
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """Build the parser used by both the installed command and ``-m``."""

    parser = _ArgumentParser(
        prog="turing-agent-mailbox",
        description="Exchange durable JSON messages between local coding agents.",
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("TURING_AGENT_MAILBOX_DB"),
        help="dedicated SQLite store (or TURING_AGENT_MAILBOX_DB)",
    )
    parser.add_argument(
        "--workflow",
        default=os.environ.get("TURING_AGENT_MAILBOX_WORKFLOW"),
        help="workflow namespace (or TURING_AGENT_MAILBOX_WORKFLOW)",
    )
    parser.add_argument(
        "--agent",
        default=os.environ.get("TURING_AGENT_MAILBOX_AGENT"),
        help="fixed local agent identity (or TURING_AGENT_MAILBOX_AGENT)",
    )
    actions = parser.add_subparsers(dest="action", required=True)

    register = actions.add_parser("register", help="register this agent")
    register.add_argument("--provider", default="", help="optional provider label")

    actions.add_parser("peers", help="list registrations in this workflow")

    send = actions.add_parser("send", help="send one message")
    send.add_argument("--to", required=True, dest="recipient", help="registered recipient")
    send.add_argument("--text", required=True, help="message text")
    send.add_argument("--kind", default="message", help="message kind")
    send.add_argument("--reply-to", help="message ID being answered")
    send.add_argument("--idempotency-key", help="sender-scoped retry key")
    data_group = send.add_mutually_exclusive_group()
    data_group.add_argument("--data", help="JSON object metadata")
    data_group.add_argument(
        "--data-stdin",
        action="store_true",
        help="read the JSON object metadata from stdin",
    )

    inbox = actions.add_parser("inbox", help="read unacknowledged messages")
    inbox.add_argument("--limit", type=int, default=50)

    ack = actions.add_parser("ack", help="acknowledge one message")
    ack.add_argument("message_id")

    return parser


def _parse_data(args: argparse.Namespace) -> dict[str, Any] | None:
    if args.data_stdin:
        raw = sys.stdin.read()
    elif args.data is not None:
        raw = args.data
    else:
        return None
    try:
        parsed = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except RecursionError as exc:
        raise MailboxError("--data is too deeply nested for JSON") from exc
    except json.JSONDecodeError as exc:
        raise MailboxError(f"--data must contain valid JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise MailboxError("--data must contain a JSON object")
    return parsed


def _run(args: argparse.Namespace) -> Any:
    if args.db is None or args.workflow is None or args.agent is None:
        missing = [
            name
            for name, value in (
                ("--db/TURING_AGENT_MAILBOX_DB", args.db),
                ("--workflow/TURING_AGENT_MAILBOX_WORKFLOW", args.workflow),
                ("--agent/TURING_AGENT_MAILBOX_AGENT", args.agent),
            )
            if value is None
        ]
        raise MailboxError("missing mailbox binding: " + ", ".join(missing))

    mailbox = Mailbox(args.db, args.workflow, args.agent)
    if args.action == "register":
        return mailbox.register(provider=args.provider)
    if args.action == "peers":
        return mailbox.peers()
    if args.action == "send":
        return mailbox.send(
            args.recipient,
            args.text,
            kind=args.kind,
            data=_parse_data(args),
            reply_to=args.reply_to,
            idempotency_key=args.idempotency_key,
        )
    if args.action == "inbox":
        return mailbox.inbox(limit=args.limit)
    if args.action == "ack":
        return mailbox.ack(args.message_id)
    raise _UsageError(f"unknown action: {args.action}")  # pragma: no cover


def _emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")))


def main(argv: list[str] | None = None) -> int:
    """Run the mailbox CLI and return a process exit status."""

    try:
        args = build_parser().parse_args(argv)
        _emit(_run(args))
    except (_UsageError, MailboxError) as exc:
        _emit({"error": str(exc), "error_type": type(exc).__name__})
        return 2 if isinstance(exc, _UsageError) else 1
    except (OSError, ValueError, TypeError) as exc:
        # Keep malformed stdin and platform errors machine-readable even when
        # they occur outside the mailbox's own validation layer.
        _emit({"error": str(exc), "error_type": type(exc).__name__})
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
