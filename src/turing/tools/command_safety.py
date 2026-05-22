"""Shared command-safety primitives for the shell tool and the safety gate.

This module is the single source of truth for two things that used to be
duplicated and drifting across ``tools/shell.py`` and ``agent/safety.py``:

* the **deny-list** of catastrophic command patterns (issue #240, finding 4 —
  the two former lists are consolidated here), and
* **risk classification** for shell commands (issue #237).

Risk is judged on the *parsed* command, never on a string prefix. A command
is only downgraded from HIGH to MEDIUM when every segment of the pipeline is
a known read-only builtin — so a shell metacharacter (``;`` ``&&`` ``|`` ...)
can no longer smuggle a destructive command past the confirmation gate.

Stdlib-only by design, so any module can import it without a cycle.
"""

from __future__ import annotations

import re
import shlex

# ── Deny-list ────────────────────────────────────────────────────────────
# Catastrophic patterns that are hard-blocked regardless of risk tier. This
# is the union of the former ``shell.py`` and ``safety.py`` lists; a match is
# a denial, not a confirmation prompt. It is a pre-filter, not a boundary —
# the parse-based classifier below is what actually gates approvals.
DENY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"rm\s+-rf\s+/(?!\w)", re.IGNORECASE),  # rm -rf /
    re.compile(r"mkfs", re.IGNORECASE),  # format a filesystem
    re.compile(r"dd\s+.*of=/dev/", re.IGNORECASE),  # raw disk write
    re.compile(r":\(\)\s*\{.*\|.*&", re.IGNORECASE),  # fork bomb
    re.compile(r"chmod\s+-R\s+777\s+/", re.IGNORECASE),  # world-writable root
    re.compile(r">\s*/dev/sd", re.IGNORECASE),  # overwrite a disk
    re.compile(r"curl.*\|\s*(?:bash|sh)\b", re.IGNORECASE),  # curl … | sh
    re.compile(r"wget.*\|\s*(?:bash|sh)\b", re.IGNORECASE),  # wget … | sh
    re.compile(r"\b(?:shutdown|reboot|halt|poweroff)\b", re.IGNORECASE),  # power
    re.compile(r"\b(?:userdel|useradd|passwd)\b", re.IGNORECASE),  # user mgmt
]


def check_denylist(command: str) -> tuple[bool, str]:
    """Return ``(True, reason)`` when ``command`` matches a hard-blocked pattern."""
    for pattern in DENY_PATTERNS:
        if pattern.search(command):
            return True, f"Command blocked by deny pattern: {pattern.pattern}"
    return False, ""


# ── Risk classification ──────────────────────────────────────────────────
# Read-only builtins that, on their own, do not modify state. A command is
# downgraded to MEDIUM only when *every* pipeline segment starts with one of
# these. Anything else — including ``find -exec``/``-delete``, output
# redirects, command substitution, and unparseable input — stays HIGH.
#
# Deliberately excluded: ``env`` (it execs its argument — a generic command
# runner), and ``sort`` / ``uniq`` (both can write a file via ``-o`` / an
# output-path argument). A command runner or a file writer is not read-only.
SAFE_COMMANDS: frozenset[str] = frozenset(
    {
        "echo", "cat", "ls", "pwd", "whoami", "date", "uptime", "hostname",
        "uname", "df", "du", "free", "head", "tail", "wc", "grep", "egrep",
        "fgrep", "which", "printenv", "id", "ps", "lsblk", "lscpu", "lsusb",
        "ip", "ifconfig", "ss", "netstat", "true",
    }
)  # fmt: skip

# ``find`` is read-only *unless* it is told to execute or delete; these
# primaries turn it into arbitrary code execution / destruction.
_FIND_DANGEROUS: frozenset[str] = frozenset(
    {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
)

# Output-redirect operators — a redirect turns a read-only command into a write.
_REDIRECTS: frozenset[str] = frozenset({">", ">>", ">|", ">&", "&>", ">&1", ">&2"})


def _is_separator(token: str) -> bool:
    """True for a token made up only of pipeline / list operators (``;`` ``&&`` ``|``)."""
    return token != "" and all(ch in ";&|\n" for ch in token)


def classify_command_risk(command: str) -> str:
    """Classify a shell command as ``"medium"`` or ``"high"`` risk.

    MEDIUM is returned only when the command is genuinely read-only: every
    pipeline segment must start with a known safe builtin, with no command
    substitution, no output redirection, and no ``find -exec`` / ``-delete``.
    Everything else — including any input that cannot be parsed — is HIGH,
    so the confirmation gate cannot be bypassed by a shell metacharacter.
    """
    if not command.strip():
        return "high"

    # Command substitution / process substitution cannot be vetted statically.
    if "`" in command or "$(" in command or "<(" in command or ">(" in command:
        return "high"

    # A newline runs the next line as its own command — shlex would otherwise
    # swallow it as whitespace, hiding a second segment. Normalise it (and a
    # bare carriage return) to an explicit ``;`` separator before tokenising.
    normalized = command.replace("\n", ";").replace("\r", ";")

    try:
        lexer = shlex.shlex(normalized, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""  # never silently drop a "# ..." tail
        tokens = list(lexer)
    except ValueError:
        # Unbalanced quotes and similar — if we cannot parse it, we cannot
        # vouch for it.
        return "high"

    # An output redirect makes even a "safe" reader command write to disk.
    if any(tok in _REDIRECTS for tok in tokens):
        return "high"

    # Split the token stream into pipeline / list segments.
    segments: list[list[str]] = [[]]
    for tok in tokens:
        if _is_separator(tok):
            segments.append([])
        else:
            segments[-1].append(tok)

    for segment in segments:
        if not segment:
            continue  # empty segment from a trailing operator
        base = segment[0].rsplit("/", 1)[-1]  # /usr/bin/ls -> ls
        if base == "find":
            if any(arg in _FIND_DANGEROUS for arg in segment[1:]):
                return "high"
            continue  # plain find is a read-only search
        if base not in SAFE_COMMANDS:
            return "high"
    return "medium"
