// Flywheel pane: parses the round timeline out of `loop-*/trajectory.json`
// under the `results` root, newest loop selected by default with a dropdown to pin
// an older one. Falls back to a raw tail view when the file doesn't parse.

import { useEffect, useMemo, useState } from "react";
import { inv, subscribe } from "../tauri";
import { parseTrajectory, type Round } from "./flywheel";

interface Entry {
  rel_path: string;
  is_dir: boolean;
  size: number;
  mtime_ms: number;
}

const RESULTS_ROOT = "results";

function statusGlyph(status: Round["status"]): { glyph: string; cls: string } {
  if (status === "pass") return { glyph: "✓", cls: "text-emerald-400" };
  if (status === "fail") return { glyph: "✗", cls: "text-rose-400" };
  return { glyph: "·", cls: "text-term-dim" };
}

export default function FlywheelPane() {
  const [loops, setLoops] = useState<string[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [text, setText] = useState<string>("");
  const [rawView, setRawView] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      await inv("fs_watch", { root: RESULTS_ROOT, rel: "" });
      const entries = await inv<Entry[]>("fs_list", { root: RESULTS_ROOT, rel: "" });
      if (cancelled) return;
      const trajectoryFiles = entries.filter(
        (e) => !e.is_dir && e.rel_path.endsWith("/trajectory.json"),
      );
      const loopDirs = Array.from(
        new Set(
          trajectoryFiles
            .filter((e) => e.rel_path.split("/")[0]?.startsWith("loop-"))
            .map((e) => e.rel_path.split("/")[0]),
        ),
      );
      // Sort by the trajectory file's own mtime, newest first.
      const byMtime = new Map(
        trajectoryFiles.map((e) => [e.rel_path.split("/")[0], e.mtime_ms]),
      );
      loopDirs.sort((a, b) => (byMtime.get(b) ?? 0) - (byMtime.get(a) ?? 0));
      setLoops(loopDirs);
      setSelected((prev) => prev ?? loopDirs[0] ?? null);
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, []);

  async function reload(loop: string) {
    const t = await inv<string>("fs_read_text", {
      root: RESULTS_ROOT,
      rel: `${loop}/trajectory.json`,
    });
    setText(t);
  }

  useEffect(() => {
    if (!selected) return;
    void reload(selected);
  }, [selected]);

  useEffect(() => {
    let cancelled = false;
    const sub = subscribe<{ root: string; rel_path: string }>("fs-change", (payload) => {
      if (cancelled || payload.root !== RESULTS_ROOT || !selected) return;
      if (payload.rel_path === `${selected}/trajectory.json`) void reload(selected);
    });
    return () => {
      cancelled = true;
      sub.unsubscribe();
    };
  }, [selected]);

  const rounds = useMemo(() => parseTrajectory(text), [text]);
  const showRaw = rawView || rounds === null;

  return (
    <div className="flex h-full flex-col text-xs">
      <div className="flex items-center gap-2 border-b border-term-edge p-2">
        <select
          value={selected ?? ""}
          onChange={(e) => setSelected(e.target.value)}
          className="border border-term-edge bg-term-bg text-term-fg"
        >
          {loops.map((l) => (
            <option key={l} value={l}>
              {l}
            </option>
          ))}
        </select>
        <button
          type="button"
          onClick={() => setRawView((r) => !r)}
          className="ml-auto border border-term-edge px-2 py-0.5 text-[10px] uppercase tracking-wider text-term-dim hover:text-term-fg"
        >
          [raw]
        </button>
      </div>
      <div className="flex-1 overflow-auto p-2">
        {showRaw ? (
          <pre className="whitespace-pre-wrap text-[11px] text-term-dim">
            {text.split("\n").slice(-200).join("\n")}
          </pre>
        ) : (
          <ol className="space-y-0.5">
            {[...(rounds ?? [])].reverse().map((r) => {
              const { glyph, cls } = statusGlyph(r.status);
              return (
                <li key={`${r.index}-${r.label}`} className="flex gap-2 font-mono">
                  <span className="text-term-dim">{String(r.index).padStart(2, "0")}</span>
                  <span className="text-term-fg">{r.label}</span>
                  <span className={cls}>{glyph}</span>
                  <span className="truncate text-term-dim">{r.detail}</span>
                </li>
              );
            })}
          </ol>
        )}
      </div>
    </div>
  );
}
