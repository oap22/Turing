"""Rules-based complexity classifier for LLM routing."""

from __future__ import annotations

import re
from enum import Enum


class Complexity(str, Enum):
    """Complexity level determines which LLM backend handles a request."""

    SIMPLE = "simple"  # Route to local LLM
    COMPLEX = "complex"  # Route to cloud LLM


class ComplexityClassifier:
    """Classifies message complexity to route to the appropriate LLM.

    The classifier uses keyword matching, message length, and structural
    heuristics to decide whether a request needs the more capable cloud
    model or can be handled locally.
    """

    COMPLEX_KEYWORDS: list[str] = [
        "analyze",
        "explain",
        "debug",
        "refactor",
        "plan",
        "design",
        "compare",
        "evaluate",
        "implement",
        "architect",
        "summarize",
        "translate",
        "optimise",
        "optimize",
        "review",
        "critique",
        "synthesize",
        "elaborate",
        "investigate",
        "diagnose",
    ]

    TOOL_INDICATORS: list[str] = [
        "run",
        "execute",
        "check",
        "install",
        "create file",
        "delete",
        "restart",
        "deploy",
        "status of",
        "update",
        "configure",
        "set up",
        "setup",
        "start",
        "stop",
        "list files",
        "show logs",
        "monitor",
    ]

    CODE_PATTERNS: list[re.Pattern[str]] = [
        re.compile(r"```"),  # fenced code blocks
        re.compile(r"def\s+\w+"),  # Python function definitions
        re.compile(r"class\s+\w+"),  # class definitions
        re.compile(r"import\s+\w+"),  # import statements
        re.compile(r"write\s+(a\s+)?(script|program|function|code)", re.IGNORECASE),
        re.compile(r"(fix|patch|modify)\s+(the\s+)?(code|bug|error|issue)", re.IGNORECASE),
    ]

    WORD_THRESHOLD: int = 100

    def classify(
        self,
        message: str,
        has_tool_definitions: bool = False,
    ) -> Complexity:
        """Classify a user message as SIMPLE or COMPLEX.

        Parameters
        ----------
        message:
            The raw user text to classify.
        has_tool_definitions:
            Whether tool definitions are available for the current request.
            When tools are present and the message appears to need them the
            request is always classified as COMPLEX because the cloud model
            handles tool use more reliably.
        """
        lowered = message.lower().strip()

        # ── tool usage signals ──────────────────────────────────────────
        if has_tool_definitions and self._has_tool_indicators(lowered):
            return Complexity.COMPLEX

        # ── message length ──────────────────────────────────────────────
        word_count = len(message.split())
        if word_count > self.WORD_THRESHOLD:
            return Complexity.COMPLEX

        # ── complex keyword match ──────────────────────────────────────
        if self._has_complex_keywords(lowered):
            return Complexity.COMPLEX

        # ── code generation / manipulation ─────────────────────────────
        if self._has_code_patterns(message):
            return Complexity.COMPLEX

        # ── multi-part questions (contains multiple question marks) ────
        if message.count("?") >= 2:
            return Complexity.COMPLEX

        # ── numbered/bulleted lists in the request ─────────────────────
        if re.search(r"(?m)^\s*(?:\d+[\.\)]\s|[-*]\s)", message):
            return Complexity.COMPLEX

        return Complexity.SIMPLE

    # ── private helpers ────────────────────────────────────────────────

    def _has_complex_keywords(self, lowered: str) -> bool:
        """Return True if any complex keyword appears as a whole word."""
        for keyword in self.COMPLEX_KEYWORDS:
            # Use word-boundary matching so "plan" doesn't match "planet"
            if re.search(rf"\b{re.escape(keyword)}\b", lowered):
                return True
        return False

    def _has_tool_indicators(self, lowered: str) -> bool:
        """Return True if the message looks like it wants a tool action."""
        for indicator in self.TOOL_INDICATORS:
            if indicator in lowered:
                return True
        return False

    def _has_code_patterns(self, message: str) -> bool:
        """Return True if the message contains code or asks for code."""
        for pattern in self.CODE_PATTERNS:
            if pattern.search(message):
                return True
        return False
