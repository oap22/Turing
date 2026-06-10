"""Bench cycle — two software-only turns of the research flywheel (#360).

The test-bench idiom from CONTEXT.md: every seam is real, only the edges are
synthetic. The runner wires the *production* components — `QuestionQueue`,
`NightlyDispatcher` over a `SignedTransport`, `InboxDraftWriter`,
`MorningCuration` (+ `VaultCommitter` git commits), `EpisodeStore` +
`EpisodeRewardsStore`, `SFTDatasetBuilder` / `DPODatasetBuilder`,
`TrainerPublisher` → `AdapterRegistry` (verify), `CanaryGateRunner` →
promotion / the full rejection chain — against fixture sandboxes (a throwaway
git vault, tmp output dirs). Exactly three edges are faked:

1. **Worker LLM** — canned, deterministic drafts grounded in real source
   fragments (:meth:`BenchCycle._handle_research`).
2. **Trainer** — no LoRA maths; a stub backend emits a deterministic blob the
   real `TrainerPublisher` signs and the real `AdapterRegistry` verifies
   (:meth:`BenchCycle._trainer_backend`).
3. **Operator** — scripted curation covering every verb: accept, edit
   (corrected answer becomes the SFT target), reject, plus frontier-review
   approve *and* decline.

Two cycles are mandatory: the ADR 0009 guardrails (accumulate-never-replace,
retrain-from-base, frontier dedup) are cross-cycle properties invisible in a
single pass. Cycle 2 seeds partly from cycle 1's worker-proposed follow-ups
and its adapter is rigged to fail canary, proving the rejection chain
(REJECTED → revert → ``hard_examples`` → next-build input) before it ever
runs with a real adapter at stake.

This is *not* a dry run — the side effects (vault git commits, file moves,
reward rows) are the point; they land entirely under ``output_dir``. Never
point it at the real vault.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from turing.coordinator.adapters.registry import AdapterRegistry, HashMismatchError
from turing.coordinator.dispatch import (
    SubtaskDispatchClient,
    SubtaskKind,
    TaskResult,
)
from turing.coordinator.dispatch.grounding_fragment import (
    grounding_to_fragment,
    merge_fragments,
)
from turing.coordinator.episode_rewards import EpisodeRewardsStore, RewardSource
from turing.coordinator.flywheel.frontier import (
    EvolutionAxis,
    QuestionFrontier,
)
from turing.coordinator.flywheel.frontier_review import (
    FrontierReview,
    promoted_question_id,
)
from turing.coordinator.flywheel.generalist import (
    AI_ML_GENERALIST,
    GeneralistFleet,
    replicate_to_fleet,
    specialty_eval_dir,
)
from turing.coordinator.flywheel.morning_curation import (
    ACCEPT_REWARD,
    EDIT_REWARD,
    REJECT_REWARD,
    MorningCuration,
    SFTCandidate,
)
from turing.coordinator.flywheel.nightly_dispatcher import NightlyDispatcher
from turing.coordinator.flywheel.proposed_queue import (
    ProposedQueue,
    proposals_to_fragment,
)
from turing.coordinator.flywheel.question_queue import QuestionQueue, ResearchQuestion
from turing.coordinator.lifecycle.episode_store import EpisodeStore
from turing.coordinator.lifecycle.lifecycle import SubtaskState
from turing.coordinator.promotion.canary_gate_runner import (
    CanaryGateOutcome,
    CanaryGateRunner,
)
from turing.coordinator.promotion.canary_pass_gate import CanaryPassGate
from turing.coordinator.promotion.canary_rejection import (
    REGRESSION_HORIZON_MS,
    RejectionLog,
    format_rejection_notice,
    make_canary_rejected_handler,
)
from turing.coordinator.promotion.canary_selector import CanarySelector
from turing.coordinator.promotion.rollout import RolloutCoordinator, RolloutState
from turing.coordinator.registry import CapabilityRegistry
from turing.coordinator.registry.manifest import (
    CURRENT_MANIFEST_VERSION,
    CapabilityManifest,
)
from turing.learning.eval_set.case import EvalCase
from turing.learning.trainer.cuda_lora_trainer import CudaLoraTrainer
from turing.learning.trainer.dpo_dataset_builder import DPODatasetBuilder
from turing.learning.trainer.job import TrainingJob
from turing.learning.trainer.job_builder import TrainingJobBuilder
from turing.learning.trainer.lora_recipe import LoraRecipe, RoundCapGuard
from turing.learning.trainer.object_store import InMemoryObjectStore
from turing.learning.trainer.publisher import TrainerPublisher
from turing.learning.trainer.sft_dataset_builder import SFTDatasetBuilder
from turing.transport.bus import InMemoryBus
from turing.transport.envelope import MeshMessage
from turing.transport.signed_transport import SignedTransport
from turing.transport.signer import MessageSigner
from turing.vault.committer import VaultCommitter
from turing.vault.inbox_writer import InboxDraftWriter

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from turing.coordinator.adapters.manifest import AdapterManifest
    from turing.learning.trainer.sft_dataset_builder import SFTDatasetInfo

logger = structlog.get_logger("turing.flywheel.bench_cycle")

BASE_MODEL = "qwen2.5:7b"
BENCH_FLEET: tuple[str, ...] = ("w1", "w2", "w3", "w4")
FACILITY = "bench-dgx"
IDENTITY = ("turing-bench", "bench@turing.local")

# Rigged canary scores (percentage points, the CanaryPassGate unit). Cycle 1
# has no incumbent so any score passes; cycle 2 lands 2pp below cycle 1 —
# well past the −0.5pp rule — forcing the full rejection chain.
CYCLE1_CANARY_SCORE = 81.0
CYCLE2_CANARY_SCORE = 79.0

# Real grounding fragments the canned worker drafts cite (ADR 0009: knowledge
# enters from outside the student model).
_SOURCES = [
    {"id": "src-1", "url": "https://arxiv.org/abs/2106.09685", "title": "LoRA"},
    {"id": "src-2", "url": "https://arxiv.org/abs/2305.14314", "title": "QLoRA"},
]


class BenchInvariantError(AssertionError):
    """A seam invariant the bench cycle exists to prove did not hold."""


@dataclass(frozen=True)
class BenchCycleReport:
    """What the bench run produced — every artifact is inspectable on disk."""

    output_dir: Path
    report_path: Path
    invariants: tuple[str, ...]
    summary: dict[str, Any]


class _Feed:
    """Records notifier traffic — the bench stand-in for the morning-review /
    webui feed the rejection notice must reach."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def notify(self, kind: str, payload: dict) -> None:
        self.events.append((kind, payload))

    def kinds(self) -> list[str]:
        return [k for k, _ in self.events]


class _BenchEvolver:
    """Canned Evol-Instruct evolver (part of synthetic edge #1, the worker LLM).

    Emits one useful in-depth and one useful in-breadth variant, plus two
    candidates the real gates must drop: a low-information evolution (explicit
    elimination) and a near-duplicate of a *cycle-1* question (cross-cycle
    frontier dedup).
    """

    def __init__(self, *, cycle1_prompt: str) -> None:
        self._cycle1_prompt = cycle1_prompt

    async def evolve(self, *, question: str, axis: EvolutionAxis) -> list[str]:
        if axis is EvolutionAxis.IN_DEPTH:
            return [
                f"{question} Quantify the trade-offs on a 7B decoder at 4-bit.",
                "why though",  # low-information → eliminated
            ]
        return [
            f"How does the answer to '{question}' change for mixture-of-experts models?",
            self._cycle1_prompt,  # near-duplicate of cycle 1 → deduped
        ]


def _now_ms_factory(start: int = 1_700_000_000_000) -> Callable[[], int]:
    counter = {"t": start}

    def now() -> int:
        counter["t"] += 1
        return counter["t"]

    return now


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": IDENTITY[0],
            "GIT_AUTHOR_EMAIL": IDENTITY[1],
            "GIT_COMMITTER_NAME": IDENTITY[0],
            "GIT_COMMITTER_EMAIL": IDENTITY[1],
        },
    )


def _capability_manifest(worker_id: str) -> CapabilityManifest:
    return CapabilityManifest(
        worker_id=worker_id,
        specialties=(AI_ML_GENERALIST,),
        base_model=BASE_MODEL,
        adapters=(),
        tools=("vault_query", "web_fetch"),
        hardware="bench-fixture",
        max_concurrent=1,
        eval_score=0.5,
        public_key=b"\x01" * 32,
        schema_version=CURRENT_MANIFEST_VERSION,
    )


class BenchCycle:
    """Runs the whole flywheel twice against fixture sandboxes (#360).

    Construct with an ``output_dir`` that is private to this run (the CLI and
    the e2e test both hand it a fresh directory) and ``await run()``. Every
    invariant is a hard assertion — a seam bug raises
    :class:`BenchInvariantError` — and everything the run touched (inbox
    trees, curated vault + git log, datasets, adapter manifests, reward rows,
    the rejection feed) is written under ``output_dir`` for inspection.
    """

    def __init__(self, *, output_dir: Path) -> None:
        self._out = Path(output_dir).resolve()
        self._invariants: list[str] = []
        self._summary: dict[str, Any] = {}

        # Deterministic clock shared by every component.
        self._now = _now_ms_factory()

        # Sandboxes.
        self._vault_root = self._out / "vault-repo"
        self._artifacts = self._out / "artifacts"
        self._eval_dir = specialty_eval_dir(AI_ML_GENERALIST, root=self._out / "evals")
        self._eval_path = self._eval_dir / "heldout.jsonl"

        # Transport: real signed bus, in-memory.
        bus = InMemoryBus()
        coord_signer = MessageSigner.generate()
        self._worker_signer = MessageSigner.generate()
        self._coord_transport = SignedTransport(
            bus=bus,
            signer=coord_signer,
            trusted_keys={wid: self._worker_signer.public_key for wid in BENCH_FLEET},
            now_ms=self._now,
        )
        self._worker_transport = SignedTransport(
            bus=bus,
            signer=self._worker_signer,
            trusted_keys={"coordinator": coord_signer.public_key},
            now_ms=self._now,
        )
        self._client = SubtaskDispatchClient(
            transport=self._coord_transport, sender_id="coordinator", now_ms=self._now
        )

        # Coordinator state stores — all the real in-memory deep modules.
        self._capabilities = CapabilityRegistry(now_ms=lambda: 0, heartbeat_ttl_ms=10_000)
        for wid in BENCH_FLEET:
            self._capabilities.register(_capability_manifest(wid))
        self._queue = QuestionQueue()
        self._proposed = ProposedQueue()
        self._episodes = EpisodeStore()
        self._rewards = EpisodeRewardsStore()
        self._feed = _Feed()
        self._rejection_log = RejectionLog()

        # Training-side seams.
        self._facility_signer = MessageSigner.generate()
        self._publisher = TrainerPublisher(
            signer=self._facility_signer,
            object_store=InMemoryObjectStore(),
            facility_name=FACILITY,
        )
        self._adapters = AdapterRegistry(
            worker_base_model=BASE_MODEL,
            trusted_issuers=[self._facility_signer.public_key],
        )
        self._recipe = LoraRecipe()
        self._round_guard = RoundCapGuard()
        self._sft_builder = SFTDatasetBuilder(
            specialty=AI_ML_GENERALIST, seeds=self._seed_candidates()
        )
        self._curated_offset = 0  # cursor into MorningCuration.sft_candidates()

        # Synthetic-edge scripting.
        self._scripted_proposals: dict[str, list[tuple[str, str]]] = {}
        self._rigged_canary_scores: dict[str, float] = {}
        self._dispatched_prompts: list[str] = []
        self._trainer_jobs: list[dict[str, str]] = []
        # Set during cycle 1; used by the on_rejected closure in cycle 2.
        self._cycle1_manifest: AdapterManifest | None = None

    # ── public entry point ───────────────────────────────────────────────────

    async def run(self) -> BenchCycleReport:
        self._prepare_sandboxes()
        await self._attach_workers()

        curation = MorningCuration(
            vault_root=self._vault_root,
            episode_rewards=self._rewards,
            committer=VaultCommitter(vault_root=self._vault_root, identity=IDENTITY),
        )
        dispatcher = NightlyDispatcher(
            queue=self._queue,
            dispatch_client=self._client,
            episode_store=self._episodes,
            registry=self._capabilities,
            now_ms=self._now,
            deadline_ms=60_000,
            proposed_queue=self._proposed,
        )
        canary_runner = CanaryGateRunner(
            registry=self._adapters,
            selector=CanarySelector(),
            pass_gate=CanaryPassGate(epsilon_pp=0.5),
            dispatch_client=self._client,
            now_ms=self._now,
            on_rejected=self._make_on_rejected(),
        )

        cycle1 = await self._run_cycle_one(curation, dispatcher, canary_runner)
        cycle2 = await self._run_cycle_two(curation, dispatcher, canary_runner, cycle1)
        self._check_rejection_chain(cycle1, cycle2)
        self._check_next_build_input(cycle2)

        self._summary["cycle1"] = cycle1["public"]
        self._summary["cycle2"] = cycle2["public"]
        report_path = self._write_report()
        logger.info(
            "bench_cycle_passed",
            invariants=len(self._invariants),
            output_dir=str(self._out),
        )
        return BenchCycleReport(
            output_dir=self._out,
            report_path=report_path,
            invariants=tuple(self._invariants),
            summary=dict(self._summary),
        )

    # ── cycle 1: seed → night → morning → train → canary pass → fleet ───────

    async def _run_cycle_one(
        self,
        curation: MorningCuration,
        dispatcher: NightlyDispatcher,
        canary_runner: CanaryGateRunner,
    ) -> dict[str, Any]:
        questions = {
            "q-c1-accept": "What is LoRA fine-tuning and when is it preferable to full FT?",
            "q-c1-edit": "How does QLoRA reduce memory use during fine-tuning?",
            "q-c1-reject": "Can a 7B model self-improve indefinitely without new data?",
        }
        for i, (qid, prompt) in enumerate(questions.items()):
            self._queue.add(
                ResearchQuestion(qid, prompt, AI_ML_GENERALIST, created_at_ms=self._now() + i)
            )
            self._queue.approve(qid)
        # The worker answering q-c1-accept proposes two follow-ups; the morning
        # frontier review approves the first and declines the second.
        self._scripted_proposals["q-c1-accept"] = [
            ("How do LoRA rank choices interact with quantization error?", AI_ML_GENERALIST),
            ("What colour should the H100 chassis be?", AI_ML_GENERALIST),
        ]

        night = await dispatcher.run_nightly(batch_id="night-1")
        self._check(
            "cycle1: every approved question dispatched and answered",
            set(night.succeeded) == set(questions),
            f"succeeded={night.succeeded}",
        )
        for qid in questions:
            episode = self._episodes.get(qid)
            self._check(
                f"cycle1: episode for {qid} is reasoning-bearing",
                episode.outcome is SubtaskState.COMPLETED and len(episode.trajectory) > 0,
            )

        drafts = curation.list_inbox()
        self._check("cycle1: three drafts landed in vault/inbox", len(drafts) == 3)
        by_episode = {d.frontmatter["episode_id"]: d for d in drafts}

        accepted_path, _ = curation.accept(
            by_episode["q-c1-accept"],
            episode_id="q-c1-accept",
            question=questions["q-c1-accept"],
            recorded_at_ms=self._now(),
        )
        corrected = "LoRA trains low-rank deltas on frozen weights; QLoRA adds 4-bit NF4."
        original_answer = by_episode["q-c1-edit"].answer
        _, edited_candidate = curation.edit(
            by_episode["q-c1-edit"],
            episode_id="q-c1-edit",
            question=questions["q-c1-edit"],
            corrected_answer=corrected,
            recorded_at_ms=self._now(),
        )
        curation.reject(
            by_episode["q-c1-reject"], episode_id="q-c1-reject", recorded_at_ms=self._now()
        )

        self._check_curation_rewards(
            {"q-c1-accept": ACCEPT_REWARD, "q-c1-edit": EDIT_REWARD, "q-c1-reject": REJECT_REWARD},
            cycle="cycle1",
        )
        self._check(
            "cycle1: edited draft's SFT target is the corrected answer, reasoning kept",
            edited_candidate.answer == corrected
            and edited_candidate.answer != original_answer
            and edited_candidate.reasoning.strip() != "",
        )
        self._check(
            "cycle1: accept/edit promotions are vault git commits",
            accepted_path.exists()
            and all(
                r.commit_sha for r in curation.decisions() if r.decision.value in ("accept", "edit")
            ),
        )

        # Morning frontier review: approve one proposal, decline the other.
        review = FrontierReview(proposed=self._proposed, queue=self._queue)
        pending = self._proposed.pending()
        self._check("cycle1: worker proposals landed in the holding queue", len(pending) == 2)
        promoted = review.approve(pending[0].proposal_id, now_ms=self._now())
        review.decline(pending[1].proposal_id)
        declined = self._proposed.get(pending[1].proposal_id)
        self._check(
            "cycle1: approved proposal promoted with origin provenance",
            promoted.approved
            and promoted.origin_question_id == "q-c1-accept"
            and self._queue.get(promoted.question_id).is_runnable,
        )
        self._check(
            "cycle1: declined proposal never reaches the runnable queue",
            promoted_question_id(declined) not in {q.question_id for q in self._queue.all()},
        )

        dataset = self._build_sft_dataset(curation, version="v1")
        manifest = self._train_and_register(dataset, version="v1")
        self._cycle1_manifest = manifest

        self._write_eval_set()
        self._rigged_canary_scores["v1"] = CYCLE1_CANARY_SCORE
        outcome = await canary_runner.run(
            name=manifest.name,
            version="v1",
            specialty=AI_ML_GENERALIST,
            fleet=BENCH_FLEET,
            adapter_manifest=manifest.to_dict(),
            eval_set_path=str(self._eval_path),
        )
        self._check(
            "cycle1: stub adapter passes canary and goes LIVE",
            outcome.promoted
            and self._adapters.state_of(name=manifest.name, version="v1").value == "LIVE"
            and self._adapters.prior_live_canary_score(name=manifest.name) == CYCLE1_CANARY_SCORE,
        )

        rollout = RolloutCoordinator(
            fleet=BENCH_FLEET,
            baseline_score=CYCLE1_CANARY_SCORE,
            candidate_version="v1",
            notifier=self._feed,
        )
        replicate_to_fleet(
            rollout,
            fleet=GeneralistFleet(workers=BENCH_FLEET),
            version="v1",
            score=CYCLE1_CANARY_SCORE,
        )
        self._check(
            "cycle1: adapter replicated to the whole fleet",
            rollout.state is RolloutState.COMPLETED,
        )

        return {
            "questions": questions,
            "promoted_question_id": promoted.question_id,
            "declined_prompt": declined.prompt,
            "dataset": dataset,
            "manifest": manifest,
            "public": {
                "night": dataclasses.asdict(night),
                "dataset_examples": dataset.example_count,
                "dataset_sha256": dataset.dataset_sha256,
                "adapter": f"{manifest.name}@v1",
                "canary": dataclasses.asdict(outcome),
                "rollout_state": rollout.state.value,
            },
        }

    # ── cycle 2: frontier → night → morning → train-from-base → canary fail ─

    async def _run_cycle_two(
        self,
        curation: MorningCuration,
        dispatcher: NightlyDispatcher,
        canary_runner: CanaryGateRunner,
        cycle1: dict[str, Any],
    ) -> dict[str, Any]:
        # Frontier expansion over a fresh seed; the canned evolver also emits a
        # near-duplicate of a cycle-1 question and a low-information variant so
        # the real elimination + cross-cycle dedup gates have work to do.
        cycle1_prompt = cycle1["questions"]["q-c1-accept"]
        frontier = QuestionFrontier(evolver=_BenchEvolver(cycle1_prompt=cycle1_prompt))
        existing = [q.prompt for q in self._queue.all()]
        result = await frontier.process(
            seeds=["Compare LoRA adapters with full fine-tuning for small fleets."],
            existing_frontier=existing,
        )
        self._check(
            "cycle2: duplicate question across cycles deduped in the frontier",
            any(q.prompt == cycle1_prompt for q, _ in result.deduped),
            f"deduped={[q.prompt for q, _ in result.deduped]}",
        )
        self._check(
            "cycle2: low-information evolution explicitly eliminated",
            len(result.eliminated) >= 1 and len(result.admitted) == 2,
        )

        frontier_ids = []
        for i, evolved in enumerate(result.admitted):
            qid = f"q-c2-frontier-{i}"
            frontier_ids.append(qid)
            self._queue.add(
                ResearchQuestion(qid, evolved.prompt, AI_ML_GENERALIST, created_at_ms=self._now())
            )
            self._queue.approve(qid)
        # A/B preference probe: the same prompt dispatched twice (distinct ids,
        # consecutive → round-robin lands them on different workers). The
        # scripted operator prefers one answer; that preference becomes the
        # DPO pair.
        probe_prompt = "Summarise the evidence that self-training loops saturate in 1-3 rounds."
        for qid in ("q-c2-probe-a", "q-c2-probe-b"):
            self._queue.add(
                ResearchQuestion(qid, probe_prompt, AI_ML_GENERALIST, created_at_ms=self._now())
            )
            self._queue.approve(qid)

        night = await dispatcher.run_nightly(batch_id="night-2")
        promoted_id = cycle1["promoted_question_id"]
        self._check(
            "cycle2: cycle-1's approved follow-up dispatched with provenance intact",
            promoted_id in night.succeeded
            and self._episodes.get(promoted_id).task_id == "night-2"
            and self._queue.get(promoted_id).origin_question_id == "q-c1-accept",
        )
        self._check(
            "cycle2: declined proposal was never dispatched in any cycle",
            cycle1["declined_prompt"] not in self._dispatched_prompts,
        )

        drafts = {d.frontmatter["episode_id"]: d for d in curation.list_inbox()}
        self._check(
            "cycle2: a draft per dispatched question reached the inbox",
            set(drafts) == {promoted_id, *frontier_ids, "q-c2-probe-a", "q-c2-probe-b"},
        )

        def _question_of(eid: str) -> str:
            return self._episodes.get(eid).input_text

        curation.accept(
            drafts[promoted_id],
            episode_id=promoted_id,
            question=_question_of(promoted_id),
            recorded_at_ms=self._now(),
        )
        curation.edit(
            drafts[frontier_ids[0]],
            episode_id=frontier_ids[0],
            question=_question_of(frontier_ids[0]),
            corrected_answer="Corrected: adapter placement matters more than rank (QLoRA).",
            recorded_at_ms=self._now(),
        )
        curation.reject(
            drafts[frontier_ids[1]], episode_id=frontier_ids[1], recorded_at_ms=self._now()
        )
        curation.accept(
            drafts["q-c2-probe-a"],
            episode_id="q-c2-probe-a",
            question=probe_prompt,
            recorded_at_ms=self._now(),
        )
        curation.reject(
            drafts["q-c2-probe-b"], episode_id="q-c2-probe-b", recorded_at_ms=self._now()
        )
        self._check_curation_rewards(
            {
                promoted_id: ACCEPT_REWARD,
                frontier_ids[0]: EDIT_REWARD,
                frontier_ids[1]: REJECT_REWARD,
                "q-c2-probe-a": ACCEPT_REWARD,
                "q-c2-probe-b": REJECT_REWARD,
            },
            cycle="cycle2",
        )

        # The operator's probe preference, written back through the episode
        # store's real post-hoc scoring API, is what the DPO builder pairs on.
        self._episodes.update_critic_score(subtask_id="q-c2-probe-a", critic_score=0.9)
        self._episodes.update_critic_score(subtask_id="q-c2-probe-b", critic_score=0.2)
        dpo = DPODatasetBuilder(episode_store=self._episodes).build(
            specialty=AI_ML_GENERALIST, out_path=self._artifacts / "dataset-dpo-v2.jsonl"
        )
        dpo_rows = [
            json.loads(line)
            for line in dpo.out_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        self._check(
            "cycle2: operator preference probe yields a DPO (chosen, rejected) pair",
            any(
                r["prompt"] == probe_prompt
                and r["chosen"] == self._episodes.get("q-c2-probe-a").output_text
                and r["rejected"] == self._episodes.get("q-c2-probe-b").output_text
                for r in dpo_rows
            ),
            f"pair_count={dpo.pair_count}",
        )

        dataset = self._build_sft_dataset(curation, version="v2")
        c1_bytes = cycle1["dataset"].out_path.read_bytes()
        c2_bytes = dataset.out_path.read_bytes()
        self._check(
            "cycle2: dataset strictly grows; cycle-1 pairs byte-identical in cycle-2 build",
            dataset.example_count > cycle1["dataset"].example_count
            and c2_bytes.startswith(c1_bytes),
            f"{cycle1['dataset'].example_count} -> {dataset.example_count}",
        )

        manifest = self._train_and_register(dataset, version="v2")
        self._check(
            "cycle2: adapter built from base, not from the cycle-1 adapter",
            manifest.base_model == BASE_MODEL
            and all(j["base_model"] == BASE_MODEL for j in self._trainer_jobs)
            and self._recipe.to_hyperparameters()["train_from_base"] is True,
        )

        self._rigged_canary_scores["v2"] = CYCLE2_CANARY_SCORE
        outcome = await canary_runner.run(
            name=manifest.name,
            version="v2",
            specialty=AI_ML_GENERALIST,
            fleet=BENCH_FLEET,
            adapter_manifest=manifest.to_dict(),
            eval_set_path=str(self._eval_path),
        )

        return {
            "manifest": manifest,
            "canary_outcome": outcome,
            "dataset": dataset,
            "public": {
                "night": dataclasses.asdict(night),
                "frontier_deduped": [q.prompt for q, _ in result.deduped],
                "frontier_eliminated": [q.prompt for q, _ in result.eliminated],
                "dataset_examples": dataset.example_count,
                "dataset_sha256": dataset.dataset_sha256,
                "dpo_pairs": dpo.pair_count,
                "adapter": f"{manifest.name}@v2",
                "canary": dataclasses.asdict(outcome),
            },
        }

    # ── the rejection chain & next-cycle feed-forward ────────────────────────

    def _check_rejection_chain(self, cycle1: dict[str, Any], cycle2: dict[str, Any]) -> None:
        name = cycle2["manifest"].name
        outcome: CanaryGateOutcome = cycle2["canary_outcome"]
        self._check(
            "rejection: cycle-2 canary fails the -0.5pp rule",
            not outcome.promoted
            and outcome.status == "regression"
            and outcome.delta_pp is not None
            and outcome.delta_pp < -0.5,
            f"delta_pp={outcome.delta_pp}",
        )
        self._check(
            "rejection: cycle-2 adapter is permanently REJECTED",
            self._adapters.state_of(name=name, version="v2").value == "REJECTED"
            and self._raises_value_error(lambda: self._adapters.promote(name=name, version="v2")),
        )
        self._check(
            "rejection: canary reverts to the cycle-1 adapter",
            self._adapters.state_of(name=name, version="v1").value == "LIVE"
            and self._adapters.prior_live_canary_score(name=name) == CYCLE1_CANARY_SCORE,
        )
        hard_ids = [h.subtask_id for h in outcome.hard_examples]
        # Guard against make_canary_rejected_handler no longer registering these
        # episodes: a missing episode would crash with AttributeError rather than
        # raising a clear BenchInvariantError.
        missing = [hid for hid in hard_ids if self._episodes.get(hid) is None]
        self._check(
            "rejection: hard_example episodes were registered in EpisodeStore",
            len(missing) == 0,
            f"missing={missing}",
        )
        self._check(
            "rejection: worst-failed eval cases archived as hard_examples",
            len(hard_ids) > 0
            and all(
                self._episodes.get(hid).outcome is SubtaskState.FAILED
                and self._episodes.get(hid).task_id == f"canary-{name}-v2"
                for hid in hard_ids
            ),
        )
        notices = [p for k, p in self._feed.events if k == "canary_rejection_notice"]
        self._check(
            "rejection: notice reaches the morning-review/webui feed",
            "training_failed" in self._feed.kinds()
            and len(notices) == 1
            and "failed canary" in notices[0]["body"],
        )
        self._summary["rejection_notice"] = notices[0]["body"] if notices else None
        self._summary["hard_example_ids"] = hard_ids

    def _check_next_build_input(self, cycle2: dict[str, Any]) -> None:
        """Hard examples must be in the *next* dataset build's input corpus —
        and the positive-only filter must keep them out of its positive rows."""
        outcome: CanaryGateOutcome = cycle2["canary_outcome"]
        hard_ids = {h.subtask_id for h in outcome.hard_examples}
        next_input_ids = {ep.subtask_id for ep in self._episodes.all_episodes()}
        self._check(
            "feed-forward: hard_examples present in the next dataset build input",
            hard_ids <= next_input_ids,
        )
        positives = TrainingJobBuilder(episode_store=self._episodes).build(
            specialty=AI_ML_GENERALIST,
            top_k=100,
            out_path=self._artifacts / "dataset-next-positive.jsonl",
        )
        hard_inputs = {h.input_text for h in outcome.hard_examples}
        rows = [
            json.loads(line)
            for line in positives.out_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        self._check(
            "feed-forward: hard_examples never promoted into positive training rows",
            all(r["input"] not in hard_inputs for r in rows),
        )

    # ── seams: dataset / trainer / registry ─────────────────────────────────

    def _build_sft_dataset(self, curation: MorningCuration, *, version: str) -> SFTDatasetInfo:
        candidates = curation.sft_candidates()
        fresh = candidates[self._curated_offset :]
        self._curated_offset = len(candidates)
        previous_size = self._sft_builder.corpus_size
        self._sft_builder.accumulate(fresh)
        self._sft_builder.assert_grew(previous_size=previous_size)
        return self._sft_builder.build(out_path=self._artifacts / f"dataset-sft-{version}.jsonl")

    def _train_and_register(self, dataset: SFTDatasetInfo, *, version: str) -> AdapterManifest:
        round_number = self._round_guard.check_and_increment(AI_ML_GENERALIST)
        job = TrainingJob(
            job_id=f"bench-{version}",
            dataset_url=f"file://{dataset.out_path}",
            dataset_sha256=dataset.dataset_sha256,
            base_model=BASE_MODEL,
            method="sft",
            hyperparameters=self._recipe.to_hyperparameters(),
        )
        trainer = CudaLoraTrainer(backend=self._trainer_backend, dataset_path=dataset.out_path)
        result = trainer.train(job, self._artifacts)
        manifest = self._publisher.publish(job, result, version=version)
        manifest_path = self._artifacts / f"manifest-{version}.json"
        manifest_path.write_text(json.dumps(manifest.to_dict(), indent=2), encoding="utf-8")

        blob = result.adapter_path.read_bytes()
        self._check(
            f"{version}: tampered adapter blob fails AdapterRegistry.verify",
            self._raises_hash_mismatch(lambda: self._adapters.verify(manifest, blob + b"x")),
        )
        self._adapters.register(manifest, blob)  # verify → STAGED
        self._check(
            f"{version}: signed stub adapter verifies and lands STAGED (round {round_number})",
            self._adapters.state_of(name=manifest.name, version=version).value == "STAGED",
        )
        return manifest

    def _trainer_backend(self, *, job: TrainingJob, dataset_path: Path, out_path: Path) -> bytes:
        """Synthetic edge #2 — the blob is derived from the base model and the
        dataset only; a prior adapter cannot leak in (no stacking)."""
        dataset_sha = hashlib.sha256(dataset_path.read_bytes()).hexdigest()
        blob = f"bench-stub-adapter|base={job.base_model}|dataset={dataset_sha}".encode()
        out_path.write_bytes(blob)
        self._trainer_jobs.append({"job_id": job.job_id, "base_model": job.base_model})
        return blob

    # ── synthetic edge #1: the worker LLM ────────────────────────────────────

    async def _attach_workers(self) -> None:
        for worker_id in BENCH_FLEET:
            await self._attach_worker(worker_id)

    async def _attach_worker(self, worker_id: str) -> None:
        transport = self._worker_transport

        async def handle(msg: MeshMessage) -> None:
            payload = json.loads(msg.payload.decode("utf-8"))
            if payload.get("kind") == SubtaskKind.CANARY_EVAL.value:
                result = self._handle_canary_eval(payload, worker_id)
            else:
                result = self._handle_research(payload, worker_id)
            reply = MeshMessage(
                request_id=f"r-{payload['subtask_id']}",
                sender_id=worker_id,
                subject=f"subtasks.{payload['subtask_id']}.result",
                payload=json.dumps(result.to_dict()).encode("utf-8"),
                timestamp_ms=self._now(),
            )
            await transport.publish(reply)

        await transport.subscribe(f"subtasks.workers.{worker_id}", handle)

    def _handle_research(self, payload: dict[str, Any], worker_id: str) -> TaskResult:
        subtask_id = payload["subtask_id"]
        prompt = payload["prompt"]
        self._dispatched_prompts.append(prompt)
        answer = (
            f"Grounded answer from {worker_id}: {prompt} "
            f"In short — low-rank adapters on frozen weights, per [src-1] and [src-2]."
        )
        reasoning = [
            f"Fetched {len(_SOURCES)} sources for: {prompt}",
            "Cross-checked the claims against the LoRA and QLoRA papers.",
            "Drafted the answer with citations.",
        ]
        writer = InboxDraftWriter(vault_root=self._vault_root)
        writer.write(
            frontmatter={
                "source": worker_id,
                "task_id": payload["task_id"],
                "specialty": payload["specialty"],
                "confidence": 0.8,
                "critic_score": 0.0,
                "episode_id": subtask_id,
            },
            sources=_SOURCES,
            body=(
                f"# Answer\n\n{answer}\n\n"
                f"## Reasoning\n\n"
                + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(reasoning))
                + "\n\n## Sources\n\n- [src-1] LoRA\n- [src-2] QLoRA"
            ),
            slug=subtask_id,
        )
        fragment = merge_fragments(
            grounding_to_fragment(reasoning=reasoning, confidence=0.8, source_count=len(_SOURCES)),
            proposals_to_fragment(self._scripted_proposals.get(subtask_id, []))
            if subtask_id in self._scripted_proposals
            else None,
        )
        return TaskResult(
            subtask_id=subtask_id,
            worker_id=worker_id,
            status="COMPLETED",
            output=answer,
            tokens_used=128,
            latency_ms=42,
            model=BASE_MODEL,
            fragment=fragment,
        )

    def _handle_canary_eval(self, payload: dict[str, Any], worker_id: str) -> TaskResult:
        body = json.loads(payload["prompt"])
        eval_cases = [
            json.loads(line)
            for line in Path(body["eval_set_path"]).read_text(encoding="utf-8").splitlines()
            if line
        ]
        version = body["adapter_manifest"].get("version")
        if version not in self._rigged_canary_scores:
            raise BenchInvariantError(
                f"_handle_canary_eval: no rigged score for version={version!r}; "
                f"known={list(self._rigged_canary_scores)}"
            )
        score = self._rigged_canary_scores[version]
        # Worst-failed cases ride home with the score; the gate only archives
        # them on rejection. Real eval-case content, synthetic failure.
        failed_cases = [
            {
                "subtask_id": f"hard-{case['id']}-{version}",
                "input_text": case["input"],
                "expected_text": case["expected_claims"][0],
                "actual_output": "(degraded answer at quantization)",
                "failure_reason": "claim_preservation regression",
            }
            for case in eval_cases[-2:]
        ]
        output = json.dumps({"status": "scored", "score": score, "failed_cases": failed_cases})
        return TaskResult(
            subtask_id=payload["subtask_id"],
            worker_id=worker_id,
            status="COMPLETED",
            output=output,
            tokens_used=0,
            latency_ms=5,
            model=BASE_MODEL,
        )

    # ── synthetic edge #3 support: the scripted operator's reward checks ─────

    def _check_curation_rewards(self, expected: dict[str, float], *, cycle: str) -> None:
        for episode_id, value in expected.items():
            events = [
                e
                for e in self._rewards.events_for(episode_id)
                if e.source is RewardSource.MORNING_CURATION
            ]
            self._check(
                f"{cycle}: curation verb wrote its MORNING_CURATION reward row ({episode_id})",
                len(events) == 1 and events[0].value == value,
                f"events={[(e.source.value, e.value) for e in events]}",
            )
            self._check(
                f"{cycle}: effective reward is SUM(value) ({episode_id})",
                self._rewards.effective_reward(episode_id)
                == sum(e.value for e in self._rewards.events_for(episode_id)),
            )
            self._summary.setdefault("reward_rows", {})[episode_id] = [
                (e.source.value, e.value) for e in self._rewards.events_for(episode_id)
            ]

    # ── plumbing ─────────────────────────────────────────────────────────────

    def _prepare_sandboxes(self) -> None:
        for d in (self._vault_root, self._artifacts, self._eval_dir):
            d.mkdir(parents=True, exist_ok=True)
        _git(self._vault_root, "init", "-q")
        _git(self._vault_root, "commit", "-q", "--allow-empty", "-m", "vault: bench seed")

    def _seed_candidates(self) -> list[SFTCandidate]:
        """The frozen real seed set ADR 0009 keeps in every training run."""
        return [
            SFTCandidate(
                question="What does LoRA decompose a weight update into?",
                reasoning="From the LoRA paper: dW = B @ A with rank r << d.",
                answer="Two low-rank matrices whose product approximates the update.",
                specialty=AI_ML_GENERALIST,
            ),
            SFTCandidate(
                question="Why retrain adapters from base each cycle?",
                reasoning="ReST-EM regressed after iteration 1 when stacking.",
                answer="Iterative stacking overfits; from-base retraining bounds drift.",
                specialty=AI_ML_GENERALIST,
            ),
        ]

    def _write_eval_set(self) -> None:
        cases = [
            EvalCase(
                id=f"bench-eval-{i}",
                input=f"Held-out question {i}: state one guardrail from ADR 0009.",
                source_refs=["adr-0009"],
                expected_claims=[f"guardrail-{i}: accumulate, never replace"],
                expected_citations=["adr-0009"],
                voice_ref_id=None,
                axes=["claim_preservation", "citation_correctness"],
            )
            for i in range(5)
        ]
        body = "\n".join(c.model_dump_json() for c in cases) + "\n"
        self._eval_path.write_text(body, encoding="utf-8")

    def _make_on_rejected(
        self,
    ) -> Callable[[CanaryGateOutcome, str, str, str], Awaitable[None]]:
        base_handler = make_canary_rejected_handler(
            episode_store=self._episodes,
            rejection_log=self._rejection_log,
            notifier=self._feed,
            now_ms=self._now,
        )

        async def on_rejected(
            outcome: CanaryGateOutcome, name: str, version: str, specialty: str
        ) -> None:
            await base_handler(outcome, name, version, specialty)
            assert self._cycle1_manifest is not None, "on_rejected called before cycle-1 completed"
            notice = format_rejection_notice(
                name=name,
                version=version,
                specialty=specialty,
                outcome=outcome,
                prior_name=name,
                prior_version=self._cycle1_manifest.version,
                regressions_30d=self._rejection_log.count_within(
                    specialty=specialty,
                    now_ms=self._now(),
                    horizon_ms=REGRESSION_HORIZON_MS,
                ),
            )
            self._feed.notify("canary_rejection_notice", {"body": notice})

        return on_rejected

    def _write_report(self) -> Path:
        self._summary["invariants"] = list(self._invariants)
        self._summary["feed"] = [{"kind": k, "payload": p} for k, p in self._feed.events]
        report_path = self._out / "report.json"
        report_path.write_text(
            json.dumps(self._summary, indent=2, default=str) + "\n", encoding="utf-8"
        )
        return report_path

    def _check(self, name: str, condition: bool, detail: str = "") -> None:
        if not condition:
            raise BenchInvariantError(f"{name}{f' — {detail}' if detail else ''}")
        self._invariants.append(name)
        logger.debug("bench_invariant_held", name=name)

    @staticmethod
    def _raises_value_error(fn: Callable[[], object]) -> bool:
        try:
            fn()
        except ValueError:
            return True
        return False

    @staticmethod
    def _raises_hash_mismatch(fn: Callable[[], object]) -> bool:
        try:
            fn()
        except HashMismatchError:
            return True
        return False
