# Desktop latency engineering verification (#428)

status: agreed

Authorization: Owen requested latency implementation and permitted merging once
concrete speedups are proven. This records that existing authorization.

Question: Can terminal isolation and live chart delivery/rendering be improved
without losing output, sample order, generation resets, or graph extrema?

Control: main at 207f9ea; identical generated points through the actual before
and after Chart/metrics modules. Native watcher control retains the historical
300 ms filter and uses identical real file writes. Native PTY regression drives
an actual blocked child, another terminal, and kill/reap.

Measures: median/p95 JS work, SVG bytes, long-history rendering, native sample
visibility and write-to-notification time. Three deterministic repeat runs.
Falsifiers: lost/reordered data, lost extrema, stale generation/verdict, no gain
outside repeat variation, or unrelated terminal blocked by another writer.
Budget: local CPU only, under two minutes per measured run, no paid APIs or
cluster jobs. Dependency installation and isolated test-app build are normal
engineering verification under the task authorization. Stop when correctness,
repeat measurements, fresh-agent review, and applicable CI pass.

Limits: server-rendered Chart timings exclude the compositor; native file
measurements exclude Tauri IPC and paint. No WAN/ROSIE latency claim. A held-open
writer produced no timely FSEvents in an exploratory probe; the native baseline
uses MetricsWriter's real open/append/close pattern. Buffered external producers
must flush; long-lived handles need additional delivery investigation.
