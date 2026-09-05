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
