// Flywheel pane: parses the round timeline out of `loop-*/trajectory.json`
// under the `results` root, newest loop selected by default with a dropdown to
// pin an older one. Falls back to a raw tail view when the file doesn't parse.
//
// Three views (#390): the `list` timeline, which is dense and scannable and
// stays the default because the pane's preset is a third of a workspace
// column; the `wheel`, which makes the loop metaphor real and reads
// accumulating rounds as momentum; and `raw`. Clicking a round in either view
// expands the full `round-NN/round.json` artifact underneath it, and the
// expanded detail carries a `[metrics]` link that points the metrics pane at
// that round's runs (paneLink.ts — an in-app event, never `.viewer.json`,
// which is the agent's channel and the app must not write).

import {
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { inv, subscribe } from "../tauri";
import { parseTrajectory, type Round } from "./flywheel";
import Select from "../Select";
import {
  parseRoundRecord,
  parseRoundSummary,
  roundDirName,
  comparableToParent,
  type Comparability,
  type ProblemScore,
  type RoundRecord,
  type RoundSummary,
} from "./roundRecord";
import { wheelGeometry } from "./wheel";
import { publishMetricsTarget } from "./paneLink";

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
  // the call was never made. It must not look like either outcome — and it
  // needs its own *colour*, not just its own glyph, because the wheel draws
  // wedges and has no glyph to carry the distinction. Sharing `term-dim` with
  // "no verdict" let a missing measurement read as a real result there.
  if (status === "refused") {
    return {
      glyph: "?",
      cls: "text-amber-400",
      title: "refused — the call could not be made",
    };
  }
  return { glyph: "·", cls: "text-term-dim", title: "no verdict" };
}

/**
 * Fixed-point, with the trailing `.00` trimmed so whole numbers stay short.
 *
 * `Object.is` guards the `-0` case: a marginal gain of -0.004 at two decimals
 * renders "-0", which reads as a sign that isn't there.
 */
function fmtNum(n: number | null, digits = 2): string {
  if (n === null) return "—";
  const s = n.toFixed(digits).replace(/\.00$/, "");
  return s === "-0" || s === "-0.0" ? s.slice(1) : s;
}

/**
 * Cost per unit gain spans orders of magnitude — tokens-per-point can be tens
 * of thousands or a fraction. A fixed 0 decimals rendered anything under 0.5
 * as "0", i.e. indistinguishable from free.
 */
function fmtCost(n: number | null): string {
  if (n === null) return "—";
  if (n === 0) return "0";
  const abs = Math.abs(n);
  if (abs >= 100) return n.toFixed(0);
  if (abs >= 1) return n.toFixed(1);
  if (abs >= 0.01) return n.toFixed(2);
  return n.toExponential(1);
}

/**
 * The comparability label, worded for what the pane actually knows.
 *
 * The wording tracks the basis on purpose. "eval set changed" used to be the
 * only refusal the pane could say, so it was hard-coded into the false branch
 * — but the summary refuses for lost attempts too (this round's or its
 * parent's), and naming the wrong cause sends the operator to re-cut an eval
 * set when what they need is to re-drive one problem.
 */
function comparabilityLabel(c: Comparability): { text: string; title: string } {
  if (c.comparable === true) {
    return {
      text: "comparable to parent",
      title: "the round summary records comparable_to_parent: true",
    };
  }
  if (c.basis === "eval-set-changed") {
    return {
      text: "NOT comparable — eval set changed",
      title: "this round and its parent measured different eval sets",
    };
  }
  if (c.comparable === false) {
    return {
      text: "NOT comparable to parent",
      title:
        "the round summary records comparable_to_parent: false — the gates " +
        "and saturation verdicts below say which measurement is missing",
    };
  }
  return {
    text: "comparable: unknown",
    title:
      c.basis === "no-parent"
        ? "a baseline round has no parent to be compared with"
        : // Deliberately not recomputed from the eval-set hashes: matching
          // hashes are necessary for comparability, not sufficient, and
          // claiming "comparable" from them alone is the defect this label
          // replaced.
          "no round summary on disk records whether these numbers may be " +
          "compared with the parent's",
  };
}

/**
 * The problems reduced into each cell, keyed by cell.
 *
 * Grouped rather than listed flat so a problem sits under the mean it moved —
 * the two numbers only mean anything together.
 */
function problemsByCell(problems: ProblemScore[]): Map<string, ProblemScore[]> {
  const byCell = new Map<string, ProblemScore[]>();
  for (const p of problems) {
    const existing = byCell.get(p.cell);
    if (existing) existing.push(p);
    else byCell.set(p.cell, [p]);
  }
  return byCell;
}

/** One problem's own row, indented under the cell it is reduced into. */
function ProblemRow({
  problem,
  label,
}: {
  problem: ProblemScore;
  label: string;
}) {
  const scale = problem.scoreScale ? `${problem.scoreScale}` : "score";
  return (
    <tr className="text-term-dim">
      <td className="break-all pr-2 pl-2" title={problem.problemId}>
        ↳ {label}
      </td>
      <td className="pr-2" />
      <td className="pr-2" title={scale}>
        {/* An unscored problem ran without producing a number; "—" says that,
            where a 0 would read as a measured zero. */}
        {problem.scored === false ? "unscored" : fmtNum(problem.score)}
      </td>
      <td
        className={`pr-2 ${problem.passedCorrectness === false ? "text-rose-400" : ""}`}
        title={
          problem.passedCorrectness === null
            ? "correctness not recorded"
            : problem.passedCorrectness
              ? "passed correctness"
              : "failed correctness"
        }
      >
        {problem.passedCorrectness === null
          ? "—"
          : problem.passedCorrectness
            ? "✓"
            : "✗"}
      </td>
      {/* Δ, floor, σ and cost/pt are per-cell quantities; a problem has no
          noise floor of its own, and repeating the cell's would assert a
          measurement that was never made per problem. */}
      <td colSpan={4} />
    </tr>
  );
}

/** The `[metrics]` cross-pane link: point the metrics pane at this round's
 * runs (see paneLink.ts for the mapping and the last-action-wins rule). An
 * explicit affordance rather than the row click doing double duty — the row
 * click's job is expand-in-place, and silently re-aiming another pane on
 * every expansion would make *reading* a round rearrange the workspace. */
function MetricsLink({ onShowMetrics }: { onShowMetrics: () => void }) {
  return (
    <button
      type="button"
      onClick={onShowMetrics}
      title="show this round's runs in the metrics pane"
      className="text-term-dim underline-offset-2 hover:text-term-fg hover:underline"
    >
      [metrics]
    </button>
  );
}

/** The full round artifact, expanded under the round the user clicked. */
function RoundDetail({
  record,
  parent,
  summary,
  reason,
  onOpen,
  onShowMetrics,
}: {
  record: RoundRecord | null;
  parent: RoundRecord | null;
  summary: RoundSummary | null;
  reason: "loading" | "ok" | "missing" | "unparseable";
  onOpen: () => void;
  onShowMetrics: () => void;
}) {
  if (!record) {
    // "Not written yet" and "there but unreadable" are different situations
    // and must not be reported as the same one. The artifact is written after
    // the trajectory row, so a round mid-flight genuinely has none yet.
    const message =
      reason === "loading"
        ? "reading round.json…"
        : reason === "unparseable"
          ? "round.json is present but could not be read"
          : "no round.json for this round yet";
    return (
      <div className="flex flex-wrap gap-x-3 border-l border-term-edge py-1 pl-3 text-[11px] text-term-dim">
        <span>{message}</span>
        {/* The metrics link needs only the loop and index, and a round whose
            round.json has not landed yet is exactly the one being watched
            live — hiding the link here would hide it when it is most wanted. */}
        <MetricsLink onShowMetrics={onShowMetrics} />
      </div>
    );
  }

  const comparable = comparableToParent(record, parent, summary);
  const label = comparabilityLabel(comparable);
  const deltas = new Map(record.deltas.map((d) => [d.cell, d]));
  const byCell = problemsByCell(record.problems);
  // A problem whose cell never made it into `type_scores` would otherwise be
  // dropped entirely — invisible is exactly the failure these rows fix, so the
  // orphans are listed under their own cell name instead.
  const scoredCells = new Set(record.scores.map((s) => s.cell));
  const orphans = record.problems.filter((p) => !scoredCells.has(p.cell));

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
        {/* Whether these numbers may be compared with the parent's at all —
            stated rather than left to be inferred from a delta, and read from
            the round summary rather than recomputed. */}
        <span
          className={
            comparable.comparable === false
              ? "text-rose-400"
              : comparable.comparable
                ? "text-emerald-400"
                : ""
          }
          title={label.title}
        >
          {label.text}
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

      {(record.scores.length > 0 || record.problems.length > 0) && (
        <table className="w-full text-left">
          <thead className="text-term-dim">
            <tr>
              <th className="font-normal">cell</th>
              <th className="font-normal">n</th>
              <th className="font-normal">score</th>
              <th className="font-normal" title="correctness pass rate">
                pass
              </th>
              <th className="font-normal">Δ</th>
              <th
                className="font-normal"
                title="the noise floor Δ is measured against"
              >
                floor
              </th>
              <th className="font-normal" title="gain in noise units">
                σ
              </th>
              <th className="font-normal">cost/pt</th>
            </tr>
          </thead>
          <tbody>
            {record.scores.map((s) => {
              const d = deltas.get(s.cell);
              return (
                <Fragment key={s.cell}>
                  <tr className="text-term-fg">
                    <td className="pr-2">{s.cell}</td>
                    <td className="pr-2 text-term-dim">{s.n ?? "—"}</td>
                    <td className="pr-2">{fmtNum(s.meanScore)}</td>
                    <td className="pr-2 text-term-dim">
                      {fmtNum(s.correctnessPassRate)}
                    </td>
                    <td className="pr-2">{fmtNum(d?.marginalGain ?? null)}</td>
                    <td className="pr-2 text-term-dim">
                      {fmtNum(d?.noiseFloor ?? null)}
                    </td>
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
                      {fmtCost(d?.costPerUnitGain ?? null)}
                    </td>
                  </tr>
                  {/* The problems the cell above averages, so an operator can
                      see which ran and what each scored — a cell mean alone
                      cannot distinguish a corpus that changed composition
                      from one that did not. */}
                  {(byCell.get(s.cell) ?? []).map((p) => (
                    <ProblemRow
                      key={`${s.cell}:${p.problemId}`}
                      problem={p}
                      label={p.problemId}
                    />
                  ))}
                </Fragment>
              );
            })}
            {orphans.map((p) => (
              <ProblemRow
                key={`orphan:${p.cell}:${p.problemId}`}
                problem={p}
                label={`${p.cell} · ${p.problemId}`}
              />
            ))}
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
        {/* Human-gate load is driving function #4. Rendering a missing value
            as 0 would assert a measurement that was never made. */}
        <span>{record.escalationCount ?? "—"} escalations</span>
      </div>

      {record.saturation.length > 0 && (
        <div className="space-y-0.5 text-term-dim">
          {record.saturation.map((a) => (
            <div key={`${a.cell}-${a.verdict}`}>
              <span className="text-term-fg">{a.cell}</span>{" "}
              <span
                className={
                  a.verdict === "improving"
                    ? "text-emerald-400"
                    : a.verdict?.startsWith("refused")
                      ? "text-amber-400"
                      : ""
                }
              >
                {a.verdict ?? "—"}
              </span>
              {a.reason ? ` — ${a.reason}` : ""}
            </div>
          ))}
        </div>
      )}

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

      <div className="flex flex-wrap gap-x-3">
        <button
          type="button"
          onClick={onOpen}
          className="text-term-dim underline-offset-2 hover:text-term-fg hover:underline"
        >
          [open round dir]
        </button>
        <MetricsLink onShowMetrics={onShowMetrics} />
      </div>
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
  // Stamped with the loop and round it describes. Rendering is gated on that
  // stamp matching what is currently open, so a slow read can never paint one
  // round's measurements under another round's row — the exact misattribution
  // the comparable-to-parent check exists to prevent.
  const [detail, setDetail] = useState<{
    loop: string;
    index: number;
    record: RoundRecord | null;
    /** Distinguishes "not written yet" from "there but unreadable". */
    reason: "ok" | "missing" | "unparseable";
    parent: RoundRecord | null;
    /** `round-NN/metrics.json`, the only file carrying comparability. */
    summary: RoundSummary | null;
  } | null>(null);
  const [box, setBox] = useState({ w: 0, h: 0 });
  const observerRef = useRef<ResizeObserver | null>(null);
  const reloadSeq = useRef(0);

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

  // Sequenced and guarded. `loops` is listed once at mount, so a loop dir
  // removed during the session stays in the dropdown; without the catch its
  // read would reject and leave `text` holding the *previous* loop's rounds,
  // displayed under the new loop's name. The sequence number handles the same
  // hazard from the other direction: two reads in flight, the slower one
  // landing last and winning.
  async function reload(loop: string) {
    const seq = ++reloadSeq.current;
    try {
      const t = await inv<string>("fs_read_text", {
        root: RESULTS_ROOT,
        rel: `${loop}/trajectory.json`,
      });
      if (reloadSeq.current === seq) setText(t);
    } catch {
      if (reloadSeq.current === seq) setText("");
    }
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
    // Clear on *every* change, not just on close. Leaving the previous
    // round's record in place while the next read is in flight renders one
    // round's numbers, lineage and comparability verdict under another
    // round's row.
    setDetail(null);
    if (!selected || openRound === null) return;
    const loop = selected;
    const index = openRound;
    let cancelled = false;

    async function read(
      i: number,
    ): Promise<{
      record: RoundRecord | null;
      reason: "ok" | "missing" | "unparseable";
    }> {
      let t: string;
      try {
        t = await inv<string>("fs_read_text", {
          root: RESULTS_ROOT,
          rel: `${loop}/${roundDirName(i)}/round.json`,
        });
      } catch {
        return { record: null, reason: "missing" };
      }
      const record = parseRoundRecord(t);
      // A file that is there but will not parse is a different situation
      // from one not written yet, and must not be reported as "in flight".
      return record
        ? { record, reason: "ok" }
        : { record: null, reason: "unparseable" };
    }

    // The summary is written by the reporting layer, separately from
    // `round.json`; an unreadable or absent one is not an error to report,
    // it just leaves comparability unknown (see `comparableToParent`).
    async function readSummary(i: number): Promise<RoundSummary | null> {
      try {
        return parseRoundSummary(
          await inv<string>("fs_read_text", {
            root: RESULTS_ROOT,
            rel: `${loop}/${roundDirName(i)}/metrics.json`,
          }),
        );
      } catch {
        return null;
      }
    }

    async function loadDetail() {
      const [own, parent, summary] = await Promise.all([
        read(index),
        index > 0
          ? read(index - 1)
          : Promise.resolve({ record: null, reason: "ok" as const }),
        readSummary(index),
      ]);
      if (cancelled) return;
      setDetail({
        loop,
        index,
        record: own.record,
        reason: own.reason,
        parent: parent.record,
        summary,
      });
    }

    void loadDetail();
    const sub = subscribe<{ root: string; rel_path: string }>(
      "fs-change",
      (payload) => {
        if (cancelled || payload.root !== RESULTS_ROOT) return;
        // Both artifacts, because they land independently: a round whose
        // summary is written after its `round.json` would otherwise stay
        // "comparable: unknown" until the operator collapsed and re-expanded
        // it, which reads as the answer rather than as a stale view.
        const dir = `${selected}/${roundDirName(openRound)}`;
        if (
          payload.rel_path === `${dir}/round.json` ||
          payload.rel_path === `${dir}/metrics.json`
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
  //
  // A callback ref rather than an effect: the measured div is conditionally
  // rendered, and an effect would need every condition that governs it in its
  // deps. Missing one (`rounds === null` is not a state variable) left the
  // observer attached to a detached node and `box` frozen at its last value —
  // so a shrunken pane would draw a 400px ring in a 90px box, the exact
  // "render a smudge" case the minimum radius exists to prevent. A ref
  // callback fires precisely when the node mounts and unmounts.
  const attachWheelBox = useCallback((el: HTMLDivElement | null) => {
    observerRef.current?.disconnect();
    observerRef.current = null;
    if (!el || typeof ResizeObserver === "undefined") {
      setBox({ w: 0, h: 0 });
      return;
    }
    const ro = new ResizeObserver((entries) => {
      const r = entries[0]?.contentRect;
      if (r) setBox({ w: r.width, h: r.height });
    });
    ro.observe(el);
    observerRef.current = ro;
    // Seed synchronously; the observer's first callback is a frame away.
    const r = el.getBoundingClientRect();
    setBox({ w: r.width, h: r.height });
  }, []);

  useEffect(() => () => observerRef.current?.disconnect(), []);

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

  // The `[metrics]` link in the expanded detail: hand the round to whatever
  // metrics pane is mounted (paneLink.ts). Fired from the affordance, never
  // from the expand click itself — expanding a round to read it must not
  // re-aim another pane as a side effect.
  function showMetrics(index: number) {
    if (!selected) return;
    publishMetricsTarget({ loop: selected, round: index });
  }

  // Only hand the detail view a record that is stamped with the loop and
  // round it is being rendered under. Anything else is a leftover from a
  // previous selection whose read has not landed yet, and showing it would
  // label one round's measurements as another's.
  function detailFor(index: number): {
    record: RoundRecord | null;
    parent: RoundRecord | null;
    summary: RoundSummary | null;
    reason: "loading" | "ok" | "missing" | "unparseable";
  } {
    if (!detail || detail.loop !== selected || detail.index !== index) {
      return { record: null, parent: null, summary: null, reason: "loading" };
    }
    return {
      record: detail.record,
      parent: detail.parent,
      summary: detail.summary,
      reason: detail.reason,
    };
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
        <Select
          value={selected ?? ""}
          options={loops.map((l) => ({ value: l, label: l }))}
          onChange={setSelected}
          label="loop"
          className="w-40"
        />
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
            <div ref={attachWheelBox} className="min-h-0 flex-1">
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
                  {...detailFor(openRound)}
                  onOpen={openRoundDir}
                  onShowMetrics={() => showMetrics(openRound)}
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
                      {...detailFor(r.index)}
                      onOpen={openRoundDir}
                      onShowMetrics={() => showMetrics(r.index)}
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
