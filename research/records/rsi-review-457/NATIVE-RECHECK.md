# Native PTY regression verification after resume

Test bundle: /Users/owenpacetti/Developer/active/Turing/desktop/src-tauri/target/release/bundle/macos/turing.app
Source fix: b4f56ab, integrated a999855. Rebuilt before the pause; build log is pause/native-build.txt.

Computer use reopened RSI Codex verified 450, exited the right scratch terminal, and switched workspace 1 -> 2 -> 1. No `terminal resize failed: no such pty` diagnostic appeared. Because the native canvas retained its previously documented blank-render behavior, the lead created a fresh scratch terminal and executed a marker write to /tmp/turing-457-native-exit followed by exit. The marker was read successfully from disk. Process inspection showed the test app PID 59445 retained only its original left shell PID 59750; the two scratch shells had exited. Another workspace 2 -> 1 switch produced no diagnostic in the native accessibility tree.

The same clean-exit/workspace-switch sequence produced the exact false resize diagnostic before the fix. Mounted component regressions separately prove clean exit, early exit before spawn resolves, and preservation of real active resize errors. Full frontend suite: 733 passed in 43 files. This check validates the stale-PTY fix; it does not claim the older canvas/scrollback behavior is resolved.
