"""Consistency guards for the research-agent scaffolding, ADR, and setup scripts.

These are not unit tests of behaviour — they are guards against a class of defect
that unit tests structurally cannot catch, because it lives *between* artifacts:

    a document asserts a guarantee, and the code, the settings default, or the
    other setup script does not provide it.

Every test here corresponds to a real defect found by adversarial review on
2026-08-12 and would have failed before it was fixed. The pattern each time was
the same: an artifact existed, so the prerequisite read as done, while the
property it was supposed to guarantee did not hold.

They deliberately assert on *text* in the docs. That is unusual and it is the
point: the operator guide is the thing an operator reads to decide whether the
ruler is safe, so a claim in it is a load-bearing artifact and gets pinned like
one. Where a claim is qualified rather than removed, these tests pin the
qualification, so a future edit that quietly restores the overclaim fails.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from turing.research.loop.settings import ResearchLoopSettings

REPO_ROOT = Path(__file__).resolve().parents[1]
GUIDE = REPO_ROOT / "docs" / "research-agent.md"
ADR = REPO_ROOT / "docs" / "adr" / "0011-autonomous-research-agent-retarget.md"
SANDBOX_SCRIPT = REPO_ROOT / "scripts" / "setup-research-sandbox.sh"
BOOTSTRAP_SCRIPT = REPO_ROOT / "scripts" / "bootstrap-turing-skills.sh"
OPEN_QUESTIONS = REPO_ROOT / "research" / "OPEN-QUESTIONS.md"
JOURNAL = REPO_ROOT / "research" / "JOURNAL.md"

WORKSPACE_ENV_VAR = "TURING_RESEARCH_WORKSPACE_ROOT"
FORK_ENV_VAR = "TURING_SKILLS_DEST"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing: {path}"
    return path.read_text(encoding="utf-8")


def _norm(text: str) -> str:
    """Collapse whitespace, so a claim is found however markdown wrapped it."""
    return " ".join(text.split())


def _shell_var(script: str, name: str, _depth: int = 0) -> str:
    """Value of a top-level ``NAME="…"`` assignment, resolving ``${OTHER}`` refs."""
    match = re.search(rf'^{name}="([^"]*)"', script, flags=re.MULTILINE)
    assert match, f"no top-level {name}= assignment found"
    value = match.group(1)
    assert _depth < 5, f"{name} expands cyclically"
    return re.sub(
        r"\$\{(\w+)\}",
        lambda m: _shell_var(script, m.group(1), _depth + 1),
        value,
    )


# --------------------------------------------------------------------------- #
# The sandbox boundary: provisioned is not the same as used
# --------------------------------------------------------------------------- #


def test_sandbox_workspace_differs_from_settings_default_so_docs_must_say_so():
    """The gap that made ADR 0011 §8's boundary inert, pinned from both ends.

    ``research_workspace_root`` defaults to ``$HOME/turing-workspace``, which is
    the *operator's* home when the loop runs as the operator, while the script
    provisions ``/Users/turing/turing-workspace``. Whenever those two disagree,
    the operator guide must name the env var that closes the gap — otherwise an
    operator follows the guide exactly and gets an agent running unconfined.
    """
    script = _read(SANDBOX_SCRIPT)
    sandbox_workspace = _shell_var(script, "SANDBOX_HOME") + "/turing-workspace"
    default_root = ResearchLoopSettings(_env_file=None).research_workspace_root

    if str(default_root) == sandbox_workspace:  # pragma: no cover - future state
        pytest.skip("settings default now points at the sandbox; the gap is closed in code")

    guide = _read(GUIDE)
    assert WORKSPACE_ENV_VAR in guide, (
        f"the loop defaults to {default_root} but the sandbox is {sandbox_workspace}; "
        f"the operator guide must tell the operator to set {WORKSPACE_ENV_VAR}"
    )
    assert sandbox_workspace in guide, (
        f"{WORKSPACE_ENV_VAR} is mentioned but {sandbox_workspace} is not — the "
        "operator has to be told the value, not just the variable"
    )


def test_verify_mode_checks_the_loop_uses_the_sandbox_not_only_that_it_exists():
    """``--verify`` printed "Boundary intact" over an unused sandbox.

    Existence checks are not boundary checks. Verify must inspect the configured
    workspace root, and must not claim more than it checked.
    """
    script = _read(SANDBOX_SCRIPT)
    assert WORKSPACE_ENV_VAR in script, "--verify must check the configured workspace root"
    assert "configured_workspace_root" in script, (
        "--verify must resolve the workspace root the loop would actually use"
    )
    assert "Boundary intact" not in script, (
        "'Boundary intact' overclaims: the script can assert the sandbox is sealed, "
        "not that the agent runs inside it"
    )


def test_docs_do_not_claim_the_privilege_drop_that_does_not_exist():
    """No code under ``src/turing/research/`` drops privileges to ``turing``.

    Until it does, both the guide and the ADR must say so where they describe the
    sandbox, or a green ``--verify`` reads as a safety guarantee it is not.
    """
    src = REPO_ROOT / "src" / "turing" / "research"
    sources = "\n".join(p.read_text(encoding="utf-8") for p in src.rglob("*.py"))
    drops_privileges = any(
        token in sources for token in ("setuid", "seteuid", "getpwnam", "sudo -u")
    )
    if drops_privileges:  # pragma: no cover - future state
        pytest.skip("the loop now drops privileges; the caveat may be removed")

    # The caveat has to be a *negated* statement about privilege dropping. "The
    # sandbox is provisioned" and "the sandbox is used" read identically to an
    # operator otherwise, and only the second one is a boundary.
    negated = re.compile(r"(?:[Nn]othing|[Nn]o code)[^.]{0,160}drops? privileges")
    for path in (GUIDE, ADR, SANDBOX_SCRIPT):
        text = _read(path)
        assert negated.search(text), (
            f"{path.name} describes the sandbox without recording that nothing "
            "drops privileges into it — a green --verify then reads as a guarantee"
        )


def test_setup_scripts_default_destinations_do_not_conflict():
    """The two scripts were mutually incompatible as documented.

    The sandbox script tightens the operator home to 750 and keeps ``turing`` out
    of ``staff``; the bootstrap script defaults the fork to a path *inside* the
    operator home. Either the boundary holds and the agent cannot reach its own
    scaffold, or the loop runs as the operator. The guide must therefore direct
    the fork somewhere reachable from both sides.
    """
    sandbox = _read(SANDBOX_SCRIPT)
    bootstrap = _read(BOOTSTRAP_SCRIPT)
    guide = _read(GUIDE)

    assert "chmod 750 ${OPERATOR_HOME}" in sandbox or "chmod 750" in sandbox, (
        "expected the operator-home tightening step; this test's premise moved"
    )
    fork_default_is_in_home = "${HOME}/Developer" in bootstrap
    if not fork_default_is_in_home:  # pragma: no cover - future state
        pytest.skip("bootstrap no longer defaults the fork inside the operator home")

    exchange = _shell_var(sandbox, "EXCHANGE_DIR")
    assert exchange.startswith("/Users/Shared/"), (
        "the shared location must sit outside both homes to be reachable from both"
    )
    assert FORK_ENV_VAR in guide and exchange in guide, (
        f"the guide must direct the fork to {exchange} via {FORK_ENV_VAR}; with the "
        "defaults the agent cannot reach its own scaffold"
    )
    assert FORK_ENV_VAR in sandbox, "--verify must check the fork is reachable by the sandbox user"


# --------------------------------------------------------------------------- #
# Invariants the docs are not allowed to overstate
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("claim", "why"),
    [
        (
            "structurally prevented from accepting writes",
            "the freeze is two one-shot checks; a post-hoc __setattr__ assignment "
            "defeats both and the mutated verifier re-binds silently",
        ),
        (
            "can arrive only through an operator `extend_cap` decision",
            "Attempt.evolve forwards arbitrary field replacement, so the cap can be "
            "swapped wholesale with extension_count unchanged",
        ),
    ],
)
def test_adr_does_not_restate_a_guarantee_the_contracts_do_not_provide(claim: str, why: str):
    assert claim not in _norm(_read(ADR)), f"ADR overclaims — {why}"


def test_adr_records_where_the_contracts_stop_short():
    """Weakening a claim is only half the fix; the gap needs somewhere to close."""
    adr = _norm(_read(ADR))
    assert "### 17." in adr, "expected a section listing the contract gaps"
    for gap in ("evolve", "ABANDONED", "extension_count", "consumed"):
        assert gap in adr, f"§17 does not mention the {gap} gap"


def test_the_agent_may_not_quit_is_described_as_a_call_site_convention():
    """`ABANDONED` is reachable via ``Attempt.evolve`` without any escalation.

    The current writers all gate on an operator verdict, so the docs may say the
    solver cannot self-quit — but not that the type prevents it.
    """
    guide = _norm(_read(GUIDE))
    assert "reachable *only* through an operator `abandon` verdict" not in guide, (
        "that states a structural guarantee; the guarantee lives in two call sites"
    )
    assert "no self-quit transition anywhere in the solver" in guide, (
        "the true half of the claim should survive"
    )


def test_write_surface_contradiction_is_surfaced_not_silently_widened():
    """ADR §6 stated two incompatible write surfaces in one section.

    The wide reading ("the whole Turing repo ... writable") voids three
    invariants at once: this repo holds the frozen-verifier enforcement, the cap
    accounting, and the held-out score cells under ``research/results/``.
    """
    adr = _norm(_read(ADR))
    assert "whole Turing repo plus `turing-skills` are writable" not in adr, (
        "the wide reading may not be stated as settled fact"
    )
    assert "never Turing's source" in adr, "the narrow reading must survive"
    assert "R1" in adr, "the unresolved question must be registered"


# --------------------------------------------------------------------------- #
# The register stays a faithful mirror, and gates do not drift
# --------------------------------------------------------------------------- #


def test_brief_q_numbers_are_not_reused_by_questions_raised_later():
    """New questions take ``R`` numbers; the brief cites its own Q-numbers."""
    text = _read(OPEN_QUESTIONS)
    brief_numbers = set(re.findall(r"\*\*(Q\d+b?)\*\*", text))
    assert {"Q2", "Q11", "Q12", "Q15"} <= brief_numbers, "the brief's numbering drifted"
    assert "Q16" not in text, "a post-brief question must not take the next free Q number"
    assert set(re.findall(r"\*\*(R\d)\*\*", text)) >= {"R1", "R2", "R3"}


def test_error_taxonomy_is_filed_as_a_before_round_zero_prerequisite():
    """The brief gates it twice; the looser gate makes round 0 incomparable.

    Round 0 produces the failures the first self-edit reads. If they are
    categorised ad hoc, round 0 and round 1 are not comparable, and no later
    rigour recovers it without re-running round 0. So the taxonomy must appear on
    the loop-1 side of the ADR's prerequisite split and nowhere on the loop-2
    side — the two lists are the thing that actually gates it.
    """
    adr = _read(ADR)
    loop1 = re.search(r"Loop-1 prerequisites before round 0:(.+?)\n\n", adr, re.DOTALL)
    loop2 = re.search(r"Loop-2 prerequisites before the first self-edit:(.+?)\n\n", adr, re.DOTALL)
    assert loop1 and loop2, "the ADR's two prerequisite lists moved; this guard needs updating"
    # Markdown wraps these lists, so a phrase can straddle a newline.
    loop1_text = " ".join(loop1.group(1).split())
    loop2_text = " ".join(loop2.group(1).split())
    assert "error taxonomy" in loop1_text, (
        "the frozen error taxonomy is not on the before-round-0 list; round 0's "
        "failures would be categorised ad hoc and round 0/round 1 become incomparable"
    )
    assert "error taxonomy" not in loop2_text, (
        "the taxonomy is still filed as a loop-2 item; that is the looser of the "
        "brief's two gates and it defeats the stricter one's purpose"
    )

    # Q15 in the register must carry the same gate, or the two drift apart.
    questions = _read(OPEN_QUESTIONS)
    q15 = questions[questions.find("**Q15**") :][:900]
    assert "closes before round 0" in q15, "Q15 still closes at the looser gate"

    # And the guide must not list it as something blocking only the first
    # self-edit, which is where it used to sit.
    guide = _read(GUIDE)
    assert "The first self-edit summary" not in guide, (
        "the guide's 'not built yet' table still gates the taxonomy at the first "
        "self-edit summary rather than at round 0"
    )


def test_h2_falsifier_versus_stopping_rule_is_recorded_as_unresolved():
    """H2 needs two below-noise rounds; the stopping rule stops after one."""
    questions = _read(OPEN_QUESTIONS)
    assert "R5" in questions, "the H2/stopping-rule interaction must be registered"
    window = questions[questions.find("**R5**") :][:1200]
    assert "H2" in window and "stopping" in window.lower()


def test_journal_corrects_by_addition_and_never_by_edit():
    """The pivot entry stays intact; corrections arrive as a newer entry above."""
    journal = _read(JOURNAL)
    entries = [line for line in journal.splitlines() if line.startswith("## ")]
    assert len(entries) >= 2, "a correction must be a new entry, not an edit"
    assert "Pivot: Turing becomes an autonomous research agent" in entries[-1], (
        "the original pivot entry must remain, and remain last"
    )
    pivot = journal[journal.find("## 2026-08-12 — Pivot") :]
    assert "**Result:** none. No run has been executed." in pivot, (
        "the pivot entry was edited in place; corrections go in a new entry"
    )
