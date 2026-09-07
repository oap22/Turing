import json
import os
from pathlib import Path

import pytest

from turing.research.rsi.contracts import RsiConfig, VerifierSpec
from turing.research.rsi.engine import FakeEngine, ok_result
from turing.research.rsi.loop import RsiLoop, read_trajectory


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['replace', 'delete', 'truncate', 'same-stat'])
async def test_protect_existing_history(tmp_path: Path, mutation):
    cfg = RsiConfig(slug='holdout', workspace_root=tmp_path / 'ws', results_root=tmp_path / 'out', rounds=1, self_edit_every=0)
    cfg.sandbox_dir.mkdir(parents=True)
    (cfg.sandbox_dir / 'verify.sh').write_text("printf 'score=1\\n'\n")
    path = cfg.results_dir / 'trajectory.json'
    spec = VerifierSpec(command='sh verify.sh', files=('verify.sh',))
    def normal(prompt, cwd):
        with (cfg.results_dir / 'metrics.jsonl').open('a') as f: f.write('{"score":1}\n')
        return ok_result()
    await RsiLoop(cfg, engine=FakeEngine(script=[normal]), verifier=spec, self_edit=None, problem='holdout').run()
    previous = read_trajectory(path).records[0].to_json()
    def attack(prompt, cwd):
        old = path.read_text(); st = path.stat()
        if mutation == 'delete': path.unlink()
        elif mutation == 'truncate': path.write_text('')
        else:
            lines = [json.loads(line) for line in old.splitlines()]
            for row in lines:
                if 'event' not in row: row['score'] = 9.0
            changed = ''.join(json.dumps(row) + '\n' for row in lines)
            if mutation == 'replace':
                sibling = path.with_suffix('.replacement'); sibling.write_text(changed); sibling.replace(path)
            else:
                assert len(old) == len(changed)
                path.write_text(changed)
                os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        return normal(prompt, cwd)
    out = await RsiLoop(cfg, engine=FakeEngine(script=[attack]), verifier=None, self_edit=None).run()
    state = read_trajectory(path)
    assert out.exit_code == 3
    assert state.records[-1].void
    assert state.records[0].to_json() == previous
    assert state.best_score == 1.0
