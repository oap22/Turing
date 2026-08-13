// Flywheel pane: parses the round timeline out of `loop-*/trajectory.json`
// under the `results` root, newest loop selected by default with a dropdown to
// pin an older one. Falls back to a raw tail view when the file doesn't parse.
//
// Three views (#390): the `list` timeline, which is dense and scannable and
// stays the default because the pane's preset is a third of a workspace
// column; the `wheel`, which makes the loop metaphor real and reads
// accumulating rounds as momentum; and `raw`. Clicking a round in either view
// expands the full `round-NN/round.json` artifact underneath it.

import { useEffect, useMemo, useRef, useState } from "react";
import { inv, subscribe } from "../tauri";
import { parseTrajectory, type Round } from "./flywheel";
import {
  parseRoundRecord,
  roundDirName,
  comparableToParent,
  type RoundRecord,
} from "./roundRecord";
import { wheelGeometry } from "./wheel";

interface Entry {
  rel_path: string;
  is_dir: boolean;
  size: number;
  mtime_ms: number;
}

const RESULTS_ROOT = "results";

type View = "list" | "wheel";

function statusGlyph(status: Round["status"]): {
  glyph: string;
  cls: string;
  title: string;
} {
  if (status === "pass") {
    return {
      glyph: "✓",
      cls: "text-emerald-400",
      title: "improving — a cell beat its noise floor",
    };
  }
  if (status === "fail") {
    return { glyph: "✗", cls: "text-rose-400", title: "failed a gate" };
  }
  if (status === "saturated") {
    return {
      glyph: "=",
      cls: "text-term-fg",
      title: "saturated — no cell beat its noise floor",
    };
  }
  // A refusal is not a flat round: it means the measurement needed to make
  // the call was never made. It must not look like either outcome.
  if (status === "refused") {
    return {
      glyph: "?",
      cls: "text-term-dim",
      title: "refused — the call could not be made",
    };
  }
  return { glyph: "·", cls: "text-term-dim", title: "no verdict" };
}

function fmtNum(n: number | null, digits = 2): string {
  return n === null ? "—" : n.toFixed(digits).replace(/\.00$/, "");
}

/** The full round artifact, expanded under the round the user clicked. */
function RoundDetail({
  record,
  parent,
  onOpen,
}: {
  record: RoundRecord | null;
  parent: RoundRecord | null;
  onOpen: () => void;
}) {
  if (!record) {
    // Missing or unparseable round.json — say so and keep the timeline. The
    // artifact is written after the trajectory row, so a round mid-flight
    // legitimately has no round.json yet.
    return (
      <div className="border-l border-term-edge py-1 pl-3 text-[11px] text-term-dim">
        no round.json for this round yet
      </div>
    );
  }

  const comparable = comparableToParent(record, parent);
  const deltas = new Map(record.deltas.map((d) => [d.cell, d]));

  return (
    <div className="space-y-1 border-l border-term-edge py-1 pl-3 text-[11px]">
      <div className="flex flex-wrap gap-x-3 text-term-dim">
        <span>
          run <span className="text-term-fg">{record.runId ?? "—"}</span>
        </span>
        <span>
          parent{" "}
          <span className="text-term-fg">{record.parentRoundId ?? "none"}</span>
        </span>
        <span>
          eval <span className="text-term-fg">{record.evalSetHash ?? "—"}</span>
        </span>
        {/* Rounds measured on different eval sets may not be compared at all,
            so this is stated rather than left to be inferred from a delta. */}
        <span
          className={
            comparable === false
              ? "text-rose-400"
              : comparable
                ? "text-emerald-400"
                : ""
          }
          title="whether this round may be compared to its parent"
        >
          {comparable === null
            ? "comparable: unknown"
            : comparable
              ? "comparable to parent"
              : "NOT comparable — eval set changed"}
        </span>
      </div>

      <div className="text-term-dim">
        engine{" "}
        <span className="text-term-fg">{record.engine.backend ?? "—"}</span>
        {" · "}
        {record.engine.orchestratorModel ?? "—"}/
        {record.engine.substepModel ?? "—"}
        {record.engine.scaffoldGitSha
          ? ` · ${record.engine.scaffoldGitSha}`
          : ""}
      </div>

      {record.scores.length > 0 && (
        <table className="w-full text-left">
          <thead className="text-term-dim">
            <tr>
              <th className="font-normal">cell</th>
              <th className="font-normal">n</th>
              <th className="font-normal">score</th>
              <th className="font-normal">Δ</th>
              <th className="font-normal">σ</th>
              <th className="font-normal">cost/pt</th>
            </tr>
          </thead>
          <tbody>
            {record.scores.map((s) => {
              const d = deltas.get(s.cell);
              return (
                <tr key={s.cell} className="text-term-fg">
                  <td className="pr-2">{s.cell}</td>
                  <td className="pr-2 text-term-dim">{s.n ?? "—"}</td>
                  <td className="pr-2">{fmtNum(s.meanScore)}</td>
                  <td className="pr-2">{fmtNum(d?.marginalGain ?? null)}</td>
                  <td
                    className={`pr-2 ${
                      d?.beatsNoiseFloor === true
                        ? "text-emerald-400"
                        : d?.beatsNoiseFloor === false
                          ? "text-term-dim"
                          : ""
                    }`}
                    title={
                      d?.beatsNoiseFloor === true
                        ? "beats the noise floor"
                        : d?.beatsNoiseFloor === false
                          ? "inside the noise floor"
                          : "no noise floor measured"
                    }
                  >
                    {fmtNum(d?.gainInNoiseUnits ?? null, 1)}
                  </td>
                  <td className="text-term-dim">
                    {fmtNum(d?.costPerUnitGain ?? null, 0)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      <div className="flex flex-wrap gap-x-3 text-term-dim">
        <span>
          {record.cost.tokens === null
            ? "—"
            : record.cost.tokens.toLocaleString()}{" "}
          tok
        </span>
        <span>{fmtNum(record.cost.wallClockSeconds, 0)}s</span>
        <span>{record.cost.attempts ?? "—"} attempts</span>
        <span>{record.escalationCount ?? 0} escalations</span>
      </div>

      {record.gates.length > 0 && (
        <div className="flex flex-wrap gap-x-3">
          {record.gates.map((g) => (
            <span
              key={g.name}
              className={g.passed ? "text-emerald-400" : "text-rose-400"}
            >
              {g.passed ? "✓" : "✗"} {g.name}
            </span>
          ))}
        </div>
      )}

      {record.verdict && <div className="text-term-dim">{record.verdict}</div>}

      <button
        type="button"
        onClick={onOpen}
        className="text-term-dim underline-offset-2 hover:text-term-fg hover:underline"
      >
        [open round dir]
      </button>
    </div>
  );
}

export default function FlywheelPane() {
  const [loops, setLoops] = useState<string[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [text, setText] = useState<string>("");
  const [rawView, setRawView] = useState(false);
  const [view, setView] = useState<View>("list");
  const [openRound, setOpenRound] = useState<number | null>(null);
  const [detail, setDetail] = useState<{
    record: RoundRecord | null;
    parent: RoundRecord | null;
  } | null>(null);
  const wheelBoxRef = useRef<HTMLDivElement | null>(null);
  const [box, setBox] = useState({ w: 0, h: 0 });

  useEffect(() => {
    let cancelled = false;
    async function load() {
      await inv("fs_watch", { root: RESULTS_ROOT, rel: "" });
      const entries = await inv<Entry[]>("fs_list", {
        root: RESULTS_ROOT,
        rel: "",
      });
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
    // A different loop's round numbering is unrelated to this one's.
    setOpenRound(null);
    setDetail(null);
  }, [selected]);

  useEffect(() => {
    let cancelled = false;
    const sub = subscribe<{ root: string; rel_path: string }>(
      "fs-change",
      (payload) => {
        if (cancelled || payload.root !== RESULTS_ROOT || !selected) return;
        if (payload.rel_path === `${selected}/trajectory.json`)
          void reload(selected);
      },
    );
    return () => {
      cancelled = true;
      sub.unsubscribe();
    };
  }, [selected]);

  // The round artifact is written after the trajectory row, so an expanded
  // round that is still in flight re-reads when its round.json lands.
  useEffect(() => {
    if (!selected || openRound === null) {
      setDetail(null);
      return;
    }
    let cancelled = false;

    async function read(index: number): Promise<RoundRecord | null> {
      try {
        const t = await inv<string>("fs_read_text", {
          root: RESULTS_ROOT,
          rel: `${selected}/${roundDirName(index)}/round.json`,
        });
        return parseRoundRecord(t);
      } catch {
        return null;
      }
    }

    async function loadDetail() {
      const [record, parent] = await Promise.all([
        read(openRound!),
        openRound! > 0 ? read(openRound! - 1) : Promise.resolve(null),
      ]);
      if (!cancelled) setDetail({ record, parent });
    }

    void loadDetail();
    const sub = subscribe<{ root: string; rel_path: string }>(
      "fs-change",
      (payload) => {
        if (cancelled || payload.root !== RESULTS_ROOT) return;
        if (
          payload.rel_path ===
          `${selected}/${roundDirName(openRound)}/round.json`
        ) {
          void loadDetail();
        }
      },
    );
    return () => {
      cancelled = true;
      sub.unsubscribe();
    };
  }, [selected, openRound]);

  // The wheel needs real pixels to decide whether it is legible at all and
  // whether labels fit, so it measures rather than scaling a viewBox.
  useEffect(() => {
    const el = wheelBoxRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver((entries) => {
      const r = entries[0]?.contentRect;
      if (r) setBox({ w: r.width, h: r.height });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [view, rawView]);

  const rounds = useMemo(() => parseTrajectory(text), [text]);
  const showRaw = rawView || rounds === null;

  function openRoundDir() {
    if (!selected || openRound === null) return;
    void inv("fs_open_external", {
      root: RESULTS_ROOT,
      rel: `${selected}/${roundDirName(openRound)}`,
    });
  }

  function toggleRound(index: number) {
    setOpenRound((prev) => (prev === index ? null : index));
  }

  const geo = useMemo(
    () =>
      rounds
        ? wheelGeometry(
            rounds.map((r) => r.index),
            box.w,
            box.h,
          )
        : null,
    [rounds, box.w, box.h],
  );
  const byIndex = useMemo(
    () => new Map((rounds ?? []).map((r) => [r.index, r])),
    [rounds],
  );

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
          onClick={() => {
            setView((v) => (v === "list" ? "wheel" : "list"));
            setRawView(false);
          }}
          disabled={rounds === null}
          className={`ml-auto border border-term-edge px-2 py-0.5 text-[10px] uppercase tracking-wider disabled:opacity-40 ${
            view === "wheel" && !showRaw
              ? "text-term-accent"
              : "text-term-dim hover:text-term-fg"
          }`}
        >
          [wheel]
        </button>
        <button
          type="button"
          onClick={() => setRawView((r) => !r)}
          className={`border border-term-edge px-2 py-0.5 text-[10px] uppercase tracking-wider ${
            showRaw ? "text-term-accent" : "text-term-dim hover:text-term-fg"
          }`}
        >
          [raw]
        </button>
      </div>

      <div className="flex-1 overflow-auto p-2">
        {showRaw ? (
          <pre className="whitespace-pre-wrap text-[11px] text-term-dim">
            {text.split("\n").slice(-200).join("\n")}
          </pre>
        ) : view === "wheel" ? (
          <div className="flex h-full flex-col">
            <div ref={wheelBoxRef} className="min-h-0 flex-1">
              {geo ? (
                <svg
                  width={box.w}
                  height={box.h}
                  role="img"
                  aria-label="round wheel"
                >
                  {geo.segments.map((seg) => {
                    const round = byIndex.get(seg.index);
                    const { cls, title } = statusGlyph(
                      round?.status ?? "other",
                    );
                    const isOpen = openRound === seg.index;
                    return (
                      <g key={seg.index}>
                        <path
                          d={seg.path}
                          className={`${cls} cursor-pointer fill-current`}
                          opacity={isOpen ? 1 : 0.65}
                          onClick={() => toggleRound(seg.index)}
                        >
                          <title>{`round ${seg.index} — ${title}`}</title>
                        </path>
                        {seg.label && (
                          <text
                            x={seg.label.x}
                            y={seg.label.y}
                            textAnchor="middle"
                            dominantBaseline="central"
                            className="pointer-events-none fill-term-bg text-[9px] font-mono"
                          >
                            {seg.index}
                          </text>
                        )}
                      </g>
                    );
                  })}
                  {/* The hub carries the count, so the ring reads as "how far
                      this loop has come" even when labels have been dropped. */}
                  <text
                    x={geo.cx}
                    y={geo.cy}
                    textAnchor="middle"
                    dominantBaseline="central"
                    className="fill-term-dim text-[11px] font-mono"
                  >
                    {rounds?.length ?? 0}
                  </text>
                </svg>
              ) : (
                <div className="p-2 text-[11px] text-term-dim">
                  pane too small for the wheel — use [wheel] to go back to the
                  list
                </div>
              )}
            </div>
            {openRound !== null && (
              <div className="max-h-[50%] shrink-0 overflow-auto border-t border-term-edge pt-1">
                <RoundDetail
                  record={detail?.record ?? null}
                  parent={detail?.parent ?? null}
                  onOpen={openRoundDir}
                />
              </div>
            )}
          </div>
        ) : (
          <ol className="space-y-0.5">
            {[...(rounds ?? [])].reverse().map((r) => {
              const { glyph, cls, title } = statusGlyph(r.status);
              const isOpen = openRound === r.index;
              return (
                <li key={`${r.index}-${r.label}`}>
                  <button
                    type="button"
                    onClick={() => toggleRound(r.index)}
                    className={`flex w-full gap-2 text-left font-mono ${
                      isOpen ? "bg-term-raised" : ""
                    }`}
                  >
                    <span className="text-term-dim">
                      {String(r.index).padStart(2, "0")}
                    </span>
                    <span className="shrink-0 text-term-fg">{r.label}</span>
                    <span className={cls} title={title}>
                      {glyph}
                    </span>
                    <span className="truncate text-term-dim" title={r.detail}>
                      {r.detail}
                    </span>
                  </button>
                  {isOpen && (
                    <RoundDetail
                      record={detail?.record ?? null}
                      parent={detail?.parent ?? null}
                      onOpen={openRoundDir}
                    />
                  )}
                </li>
              );
            })}
          </ol>
        )}
      </div>
    </div>
  );
}
