# Scaffold

Task: compact.py extracts facts from a synthetic transcript so that every
fact substring grade.py checks for still occurs verbatim (case-insensitive)
in the output, while minimizing output length. grade.py is frozen — the
fact families it checks cannot change.

## Scoring is noisy — don't overreact to one round's score
grade.py regenerates a fresh random transcript every run, and single-run
scores swing widely (observed range ~10.3–14.0 for the *same* compact.py
across different runs; one round's single score dropping vs. the previous
round does NOT mean the code got worse). Before concluding a change
helped or hurt, benchmark it yourself over multiple runs (~8+) or against
fixed-seed transcripts, and trust that aggregate over any single loop
score. Do not revert a change just because the round it landed in scored
lower than the prior round — check the multi-run mean first.

## What's implemented and measured to work (keep building on this, don't revert)
1. Whitelist fact extraction: single pass over lines, drop everything
   that isn't a known fact pattern (banners, debug/trace noise, stdout
   dumps, tool_call lines, decision rationale prose, intermediate
   traceback frames). For each fact family, emit only the minimal exact
   substring grade.py requires (e.g. `N tokens` not "budget for this
   task is N tokens", `DECISION: <d>` without the rationale, bare
   exception name + `path", line N` without `File "`/`, in run`/message).
   Dedup repeated facts (same decision text, exit-code clause, exception
   name+frame) via a seen-set — verifier only needs each fact present
   once.
2. Pack deduped facts with greedy shortest-common-superstring merging
   (repeatedly merge the pair with the longest suffix/prefix overlap,
   drop strings now fully contained in another) instead of newline-
   joining, then concatenate with no separator. Provably safe: every
   fact stays a contiguous substring of the merge result.
3. Lowercase all facts before packing (grade.py's match is
   case-insensitive) — frees more SCS merge opportunities. Nearly free,
   never harmful; keep it.
   Measured (fixed-seed, 10 transcripts, mean len(in)/len(out)): plain
   newline-join ~11.8 -> + SCS packing ~12.46 -> + SCS + lowercase
   ~12.46 (lowercasing alone contributes almost nothing but costs
   nothing). Real grade.py 8-run mean after both changes: ~12.84.

## Confirmed dead end — do not retry
Shared-*prefix* redundancy (e.g. many `exit code N after S s` facts
sharing "exit code ") is NOT exploitable: output is one linear string,
so a shared prefix can only be followed by one continuation — every
other fact with that prefix but a different suffix still needs its own
full occurrence elsewhere. Only real suffix(a)/prefix(b) overlaps
between *complete* facts count, which SCS packing already captures.

## Where remaining gains likely are (try, but expect only single-digit %)
- Better SCS heuristic: current is a standard greedy 2-approximation.
  n stays small (<60 facts, ~0.16s of a 20s budget used), so there's
  ample headroom to try a smarter merge order (build full overlap graph,
  local search / 2-opt over the merge sequence) for a possibly-better
  superstring. Verify with a multi-run benchmark before trusting it.
- Facts are already minimal per-fact substrings and prefix-sharing is a
  dead end (see above) — don't waste rounds re-deriving those; any
  further win has to come from SCS packing quality or catching an
  overlooked fact family in grade.py worth double-checking.
