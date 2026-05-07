import { useEffect, useState } from "react";
import { connectGatewayWS, type Frame } from "./ws";
import CallGraphCanvas from "./CallGraphCanvas";

export default function App() {
  const [frames, setFrames] = useState<Frame[]>([]);

  useEffect(() => {
    const stop = connectGatewayWS({
      url: window.location.origin.replace(/^http/, "ws") + "/ws",
      onFrame: (frame) =>
        setFrames((prev) => [...prev.slice(-499), frame]),
    });
    return stop;
  }, []);

  return (
    <div className="flex h-full flex-col">
      <header className="border-b border-neutral-800 px-4 py-2 text-sm font-semibold">
        turing — fleet observability (debug pane)
      </header>
      <main className="flex flex-1 overflow-hidden">
        <section className="flex-1 border-r border-neutral-800 p-2">
          <CallGraphCanvas />
        </section>
        <aside className="w-[480px] overflow-auto bg-neutral-950 p-2 font-mono text-xs">
          <h2 className="mb-2 text-neutral-400">incoming frames</h2>
          <ol className="space-y-1">
            {frames.map((f, i) => (
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
      </main>
    </div>
  );
}
