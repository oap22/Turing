# `research-summarize` eval set

Operator-curated cases that gate Phase B promotion of the `research-summarize`
specialty. Failure here blocks an adapter from being adopted, regardless of
training-loss numbers.

## Layout

```
evals/research-summarize/
  README.md                          (this file)
  cases/
    claim_preservation.jsonl
    voice_match.jsonl                (pending operator voice baseline)
    citation_correctness.jsonl       (pending citation format)

src/turing/evals/research_summarize/  (Python package — hyphen breaks imports)
  schema.py    EvalCase pydantic model
  scoring.py   deterministic scoring functions returning [0, 1]
  harness.py   CLI: loads cases/*.jsonl, runs a worker, emits aggregate score
```

## Coverage matrix

| Category              | Target | Sub-coverage                                                   |
|-----------------------|--------|----------------------------------------------------------------|
| claim_preservation    | 12     | drop-key-claim, hallucinate-fact, over-confident, numerical    |
| voice_match           | 10     | no-LLM-tells, vault-tone, length-discipline, hedge-calibration |
| citation_correctness  | 10     | single, multi-source merge, missing-citation, wrong-citation   |

## Scoring functions

- `score_claim_preservation_v1` — recall of `must_contain_claims` minus a
  leak penalty for any `must_not_contain` hit. Substring + token-overlap
  fallback today; swap to embedding similarity once we wire the worker's
  embedder in.
- `score_voice_match_v1` — pending. Will compare summary style metrics to
  a baseline computed once over `vault/curated/**`.
- `score_citation_correctness_v1` — pending. Will parse `[src_N]` markers
  and verify each cited source actually contains the claim.

## Aggregate

```
aggregate = 0.45 * claim_preservation_mean
          + 0.30 * citation_correctness_mean
          + 0.25 * voice_match_mean
```

Adjust weights in `harness.py:CATEGORY_WEIGHTS`. Promotion threshold is
operator-set per evaluation cycle, not hard-coded here.

## Running

```bash
python -m turing.evals.research_summarize.harness --worker fixture
```

`fixture` worker is a smoke test that echoes expected claims — useful for
verifying the harness loads and scores cleanly. Real workers plug in by
implementing `WorkerFn = Callable[[EvalCase], str]`.
