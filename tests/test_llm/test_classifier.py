"""Tests for the rules-based complexity classifier."""

from __future__ import annotations

import pytest

from turing.llm.classifier import Complexity, ComplexityClassifier


@pytest.fixture
def classifier() -> ComplexityClassifier:
    return ComplexityClassifier()


# ── simple messages ───────────────────────────────────────────────────


class TestSimpleMessages:
    """Messages that should be classified as SIMPLE."""

    @pytest.mark.parametrize(
        "message",
        [
            "hello",
            "hi there",
            "hey",
            "good morning",
            "thanks",
            "ok",
            "yes",
            "no",
            "what time is it?",
            "how are you?",
            "goodbye",
            "what is your name?",
            "tell me a joke",
        ],
    )
    def test_greetings_and_short_messages(
        self, classifier: ComplexityClassifier, message: str
    ) -> None:
        assert classifier.classify(message) == Complexity.SIMPLE

    def test_simple_question(self, classifier: ComplexityClassifier) -> None:
        assert classifier.classify("What is the capital of France?") == Complexity.SIMPLE

    def test_simple_statement(self, classifier: ComplexityClassifier) -> None:
        assert classifier.classify("The weather is nice today.") == Complexity.SIMPLE


# ── complex messages ──────────────────────────────────────────────────


class TestComplexMessages:
    """Messages that should be classified as COMPLEX."""

    @pytest.mark.parametrize(
        "keyword",
        [
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
        ],
    )
    def test_complex_keywords(self, classifier: ComplexityClassifier, keyword: str) -> None:
        message = f"Please {keyword} the system performance"
        assert classifier.classify(message) == Complexity.COMPLEX

    def test_long_message(self, classifier: ComplexityClassifier) -> None:
        message = " ".join(["word"] * 150)
        assert classifier.classify(message) == Complexity.COMPLEX

    def test_code_generation_request(self, classifier: ComplexityClassifier) -> None:
        assert classifier.classify("Write a script to parse JSON files") == Complexity.COMPLEX

    def test_code_block_present(self, classifier: ComplexityClassifier) -> None:
        msg = "Fix this:\n```python\nprint('hello')\n```"
        assert classifier.classify(msg) == Complexity.COMPLEX

    def test_fix_code_request(self, classifier: ComplexityClassifier) -> None:
        assert classifier.classify("Fix the bug in the authentication module") == Complexity.COMPLEX

    def test_multiple_questions(self, classifier: ComplexityClassifier) -> None:
        assert classifier.classify("What is X? And how does Y relate?") == Complexity.COMPLEX

    def test_numbered_list(self, classifier: ComplexityClassifier) -> None:
        msg = "Do the following:\n1. First thing\n2. Second thing\n3. Third thing"
        assert classifier.classify(msg) == Complexity.COMPLEX

    def test_bulleted_list(self, classifier: ComplexityClassifier) -> None:
        msg = "Check these:\n- item one\n- item two\n- item three"
        assert classifier.classify(msg) == Complexity.COMPLEX


# ── tool indicators ──────────────────────────────────────────────────


class TestToolIndicators:
    """Messages that reference tool actions should be COMPLEX when tools are available."""

    @pytest.mark.parametrize(
        "message",
        [
            "run the test suite",
            "execute the deployment script",
            "check the disk space",
            "install numpy",
            "create file config.yaml",
            "delete the temp directory",
            "restart the service",
            "deploy to production",
            "status of the web server",
        ],
    )
    def test_tool_indicators_with_tools(
        self, classifier: ComplexityClassifier, message: str
    ) -> None:
        assert classifier.classify(message, has_tool_definitions=True) == Complexity.COMPLEX

    def test_tool_indicators_without_tools(
        self,
        classifier: ComplexityClassifier,
    ) -> None:
        # Without tool definitions, a simple tool phrase should remain SIMPLE
        # (unless it triggers other complexity rules).
        result = classifier.classify("check the weather", has_tool_definitions=False)
        assert result == Complexity.SIMPLE


# ── edge cases ───────────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_string(self, classifier: ComplexityClassifier) -> None:
        assert classifier.classify("") == Complexity.SIMPLE

    def test_whitespace_only(self, classifier: ComplexityClassifier) -> None:
        assert classifier.classify("   ") == Complexity.SIMPLE

    def test_keyword_boundary(self, classifier: ComplexityClassifier) -> None:
        # "plan" should match, but "planet" should not trigger the keyword
        assert classifier.classify("Tell me about the planet Mars") == Complexity.SIMPLE
        assert classifier.classify("Plan a trip to Mars") == Complexity.COMPLEX

    def test_case_insensitive(self, classifier: ComplexityClassifier) -> None:
        assert classifier.classify("ANALYZE the data") == Complexity.COMPLEX
        assert classifier.classify("Explain quantum physics") == Complexity.COMPLEX

    def test_import_statement_is_complex(self, classifier: ComplexityClassifier) -> None:
        assert classifier.classify("import numpy as np") == Complexity.COMPLEX

    def test_class_definition_is_complex(self, classifier: ComplexityClassifier) -> None:
        assert classifier.classify("class MyHandler:") == Complexity.COMPLEX
