# Selected graph freshness engineering verification (#431)

status: agreed

Authorization: implement latency improvements and merge only with evidence.
Question: can selected graphs recover from withheld native file notifications
without adding tail reads to a healthy high-frequency event stream?
Treatment: check selected runs every 50 ms and reconcile only after 50 ms
without a completed tail. Existing per-run serialization prevents overlapping
reads. Unchanged tails do not render. The timer is removed on unmount or
selection change. The existing SSH tail stream remains unchanged; only the
remote asset rsync interval changes from 30 seconds to one second.

## Verification

A silent final append now reaches the rendered graph. Removing the watchdog
makes that regression test fail (last step remains 4 instead of 21).
Ten native events spaced 20 ms apart cause exactly ten tail calls; removing
the freshness condition makes the test fail with fourteen. The first version
of that test incorrectly enabled fake timers after mounting the component;
fresh review found the weakness and the corrected test catches the mutation.
Unmount stops polling. Full frontend suite: 724 passed; production build passed.
Independent review found no remaining production defect.

Native macOS smoke test used an isolated app identity and a synthetic 2,000-step
producer that flushes every 20 ms but keeps its output file open. Before the
watchdog, the graph remained at step 2 while the file had reached step 1,759;
it caught up after close. The candidate visibly advanced during the same
held-open producer and handled a subsequent run reset. This establishes the
missing-notification failure and recovery, not millisecond-accurate paint timing.

The watchdog bounds its scheduling delay to roughly 50–100 ms, plus IPC,
filesystem reads, parsing, and rendering. It is not a measured photon latency
bound. Idle selected runs incur up to 20 tail and 20 verdict requests per second;
no recursive directory walk is added. Remote assets now wait at most one
second between completed sync attempts instead of thirty; transfer time,
SSH setup, and network latency remain additional costs.

## Rejected experiment

Clean-source run `~/research-results/2026-09-05-remote-metrics-431-rejected/`
from retained unmerged commit `49d2e7c` compared the existing local tail stream
with a proposed framed, polling mirror. All records were preserved, but the
actual existing tail was already event-driven: median delivery 1.10–1.35 ms,
p95 1.73–1.78 ms. The prototype regressed to median 28.66–31.06 ms and p95
49.77–53.61 ms. Three alternating pairs, 60 flushed records per transfer.
Estimated 12 seconds, actual approximately 7.7 seconds; run validation passed.
The protocol/helper is rejected and is not in this PR. Revisit only with a
measured transport defect and an event-driven candidate. This is engineering
verification, not an RSI trajectory; no SSH/WAN or webview paint was timed.
