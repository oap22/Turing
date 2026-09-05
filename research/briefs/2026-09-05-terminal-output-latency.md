# Terminal output engineering verification (#430)

status: agreed

Existing authorization: implement latency improvements and merge after proof.
Question: does bounded 1 ms batching reduce PTY event overhead without losing
bytes or making interactive output materially slower?
Control: the former 4 KiB read/decode/JSON path. Treatment: production bounded
reader/coalescer. Real PTY runs /bin/cat on the same 4.8 MB Unicode stream;
three alternating control/treatment pairs encode/decode actual event payloads.
Falsifiers: changed bytes/order, unbounded producer buffering, timer starvation,
missing EOF tail, or no measured improvement. An isolated chunk may pay 1 ms
batching delay; compare initial delivery and total runtime, not events alone.
Local CPU only; under two minutes, no paid services. Native Rust suite and fresh
agent review guard correctness. Benchmark excludes browser IPC and paint; event
reduction is established separately from end-to-end screen latency.

## Results

Clean-source run b31fe2a: `~/research-results/2026-09-05-terminal-output-430/`;
reproduce with `python3 scripts/benchmarks/terminal-output.py` from the repo root.
All six transfers preserve 4,800,000 bytes exactly. Events: 4,688 → 74 (98.42%
fewer). Median completion: 157.375 → 140.313 ms (10.84% lower). Median first
output: 2.855 → 3.030 ms (0.175 ms higher); bounded sparse-output delay is the
explicit tradeoff. Independent reviewer found comparable 10.38% completion
improvement and 0.427 ms first-output increase, with no correctness findings.
Native tests: 31 passed, 2 opt-in benchmarks ignored. Independent stress checks
confirmed at most17 reads ahead of a blocked consumer and delivery during a
continuous trickle, not just at EOF. Estimated6 seconds, actual2.1 seconds.
