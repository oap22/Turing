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

## Evidence (2026-09-05, macOS arm64)

Three clean-source runs at d6bb315: `~/research-results/2026-09-05-desktop-latency-428-{a,b,c}/`.
All pass `log_run.py check`. Median ranges across repeats:

| Workload | Before | After |
|---|---:|---:|
| 50k records + 100 appended, series work | 27.59–30.91 ms | 0.077–0.090 ms |
| Actual Chart server rendering | 10.05–10.25 ms | 1.53–1.77 ms |
| Serialized SVG | 1,825,504 bytes | 46,866 bytes |
| Actual Chart at 200k records | RangeError | renders |

Native watcher command: `cargo test --manifest-path desktop/src-tauri/Cargo.toml measure_native_file_delivery -- --ignored --nocapture`.
Two independent native runs: old 14/20 samples, final lost, p95 299.55–306.35 ms;
new 20/20, final present, p95 31.99–32.92 ms. This includes the OS watcher.
Native blocked-paste regression: `cargo test --manifest-path desktop/src-tauri/Cargo.toml blocked_paste -- --nocapture`.
A blocked 4 MB paste leaves the registry free and another terminal writable
in 0.038–0.045 ms. Restoring the shared-lock bug makes the invariant test fail.
Initial timeout-only regression survived that mutation; the test was strengthened.

Validation: 722 frontend tests, TypeScript/Vite production build, 29 Rust tests
(1 opt-in benchmark), fresh independent review and re-review. Rust suite requires
unsetting LANG/LC_ALL/LC_CTYPE for a pre-existing host-sensitive locale assertion.
Native isolated app smoke: real Unicode terminal output, live curve reset and
updates during a 50 Hz job, final 500th sample `loss=-0.2624` visibly rendered.
Estimated 6 seconds/run; actual 1.1–1.3 seconds. No paid compute used.
