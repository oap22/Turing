"""The local backend is a documented seam, and the documentation is the point.

Nothing here tests behaviour beyond "it raises". What it does test is that the
research the seam was chosen against survives in the code — a refactor that
drops the model name, the runtime version, or the tool-parser gotcha turns a
recorded decision back into an open question.
"""

from __future__ import annotations

import pytest

from turing.research.backends import (
    GenerationRequest,
    LocalBackend,
    ModelBackend,
    ModelMessage,
    ModelTier,
)
from turing.research.backends import local as local_module
from turing.research.backends.errors import BackendNotImplementedError
from turing.research.backends.local import (
    EXPECTED_TOKENS_PER_SECOND,
    RECOMMENDED_MODEL,
    RECOMMENDED_RUNTIME,
    TOOL_CALL_PARSER,
)


def request() -> GenerationRequest:
    return GenerationRequest(tier=ModelTier.SUBSTEP, messages=(ModelMessage.user("go"),))


class TestStubBehaviour:
    def test_can_be_constructed_so_wiring_type_checks(self) -> None:
        assert isinstance(LocalBackend(), ModelBackend)

    async def test_generate_raises(self) -> None:
        with pytest.raises(BackendNotImplementedError):
            await LocalBackend().generate(request())

    async def test_the_failure_is_also_a_not_implemented_error(self) -> None:
        """Catchable either as a backend failure or as the stdlib marker."""
        with pytest.raises(NotImplementedError):
            await LocalBackend().generate(request())

    async def test_raising_does_not_charge_the_cap(self) -> None:
        """No call was made, so charging for one would eat real budget.

        This is the one place where over-counting is not the safe direction:
        every other estimate errs high because the tokens were genuinely sent.
        """
        backend = LocalBackend()
        with pytest.raises(BackendNotImplementedError):
            await backend.generate(request())
        assert backend.ledger.consumption.steps == 0
        assert backend.ledger.consumption.tokens == 0


class TestRecordedResearch:
    def test_names_the_selected_model_and_its_runtime(self) -> None:
        assert RECOMMENDED_MODEL == "mlx-community/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-4bit"
        assert RECOMMENDED_RUNTIME == "mlx-lm 0.31.3"

    def test_records_the_non_obvious_tool_parser(self) -> None:
        """Wiring an OpenAI-style JSON parser to it fails like model
        incompetence rather than like a parsing bug, which is why it is
        recorded rather than left to be rediscovered."""
        assert TOOL_CALL_PARSER == "qwen3_coder"

    def test_throughput_is_stated_as_a_range_and_flagged_unmeasured(self) -> None:
        assert EXPECTED_TOKENS_PER_SECOND == (80, 90)
        doc = local_module.__doc__
        assert doc is not None
        lowered = doc.lower()
        assert "inference" in lowered
        assert "not a measurement" in lowered

    def test_the_docstring_keeps_the_gotchas_that_would_otherwise_be_relearned(self) -> None:
        doc = local_module.__doc__
        assert doc is not None
        lowered = doc.lower()
        assert "disable thinking" in lowered
        assert "mamba-2" in lowered
        assert "30b total" in lowered
        assert "17.8 gb" in lowered
        assert "qwen3-coder" in lowered
