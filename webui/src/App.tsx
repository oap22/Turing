import { useEffect, useMemo, useReducer, useState } from "react";
import CallGraphCanvas from "./CallGraphCanvas";
import { emptyState, markStale, reduce, type Frame } from "./graph/reducer";
import { connectGatewayWS } from "./ws";

const STALE_TICK_MS = 5_000;

export default function App() {
  const [state, dispatch] = useReducer(
    (s: ReturnType<typeof emptyState>, f: Frame) => reduce(s, f),
    emptyState(),
  );
  const [debugFrames, setDebugFrames] = useState<Frame[]>([]);
  const [showDebug, setShowDebug] = useState(false);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const stop = connectGatewayWS({
      url: window.location.origin.replace(/^http/, "ws") + "/ws",
      onFrame: (frame) => {
        dispatch(frame as Frame);
        setDebugFrames((prev) => [...prev.slice(-499), frame as Frame]);
      },
    });
    return stop;
  }, []);

  // Periodic re-render so quiet nodes fade to stale without a frame trigger.
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), STALE_TICK_MS);
    return () => clearInterval(id);
  }, []);
  const visibleState = useMemo(() => markStale(state, now), [state, now]);

  // Backtick toggles the debug pane (hidden by default in slice 6).
  useEffect(() => {
    function onKey(ev: KeyboardEvent) {
      if (ev.key === "~" || ev.key === "`") {
        setShowDebug((s) => !s);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="flex h-full flex-col">
      <header className="border-b border-neutral-800 px-4 py-2 text-sm font-semibold">
        turing — fleet observability
        <span className="ml-2 text-xs font-normal text-neutral-500">
          press ` to toggle debug stream
        </span>
      </header>
      <main className="flex flex-1 overflow-hidden">
        <section className="flex-1 border-r border-neutral-800">
          <CallGraphCanvas state={visibleState} />
        </section>
        {showDebug && (
          <aside className="w-[480px] overflow-auto bg-neutral-950 p-2 font-mono text-xs">
            <h2 className="mb-2 text-neutral-400">incoming frames (debug)</h2>
            <ol className="space-y-1">
              {debugFrames.map((f, i) => (
                <li
                  key={i}
                  className="rounded border border-neutral-800 bg-neutral-900 p-2 text-neutral-300"
                >
                  <pre className="whitespace-pre-wrap break-words">
                    {JSON.stringify(f, null, 2)}
                  </pre>
                </li>
              ))}
            </ol>
          </aside>
        )}
      </main>
    </div>
  );
}
