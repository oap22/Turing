import os
from pathlib import Path
from turing.research.rsi import loop, cheat

def test_pytest_uses_candidate_source():
    expected=Path(os.environ['RSI_CANDIDATE_SOURCE']).resolve()
    assert Path(loop.__file__).resolve().is_relative_to(expected)
    assert Path(cheat.__file__).resolve().is_relative_to(expected)
