import json
from pathlib import Path
import pytest
from turing.research.rsi.contracts import RsiConfig, VerifierSpec
from turing.research.rsi.engine import FakeEngine, ok_result
from turing.research.rsi.loop import RsiLoop, read_trajectory

@pytest.mark.asyncio
async def test_late_trajectory_mutation_is_restored(tmp_path: Path):
    cfg=RsiConfig(slug='late',workspace_root=tmp_path/'ws',results_root=tmp_path/'out',rounds=1,self_edit_every=0)
    cfg.sandbox_dir.mkdir(parents=True)
    trajectory=cfg.results_dir/'trajectory.json'
    forged={'round':777,'started':1,'ended':2,'exit':0,'score':999.0,'passed':True,'categories':[],'scaffold_sha':None,'void':False,'agent_reported_score':None,'verifier_wall_seconds':0.0}
    # A real verifier subprocess injects the late write after the engine-phase
    # snapshot has been checked, deterministically exercising that boundary.
    script=f'printf "%s\\n" \'{json.dumps(forged)}\' >> \'{trajectory}\'\nprintf "score=1\\n"\n'
    (cfg.sandbox_dir/'verify.sh').write_text(script)
    out=await RsiLoop(cfg,engine=FakeEngine(script=[ok_result()]),verifier=VerifierSpec(command='sh verify.sh',files=('verify.sh',)),self_edit=None,problem='late boundary probe').run()
    state=read_trajectory(trajectory)
    assert out.exit_code==3
    assert state.records[-1].void
    assert state.best_score != 999.0, 'late forged score must not survive into resume'
    assert not any(r.round==777 and not r.void for r in state.records)
