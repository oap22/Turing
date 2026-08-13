// Image viewer pane — browses image files under the `vault` and `results`
// roots (plots/artifacts research runs drop, plus anything in the vault),
// merges them newest-first, and previews the selected one.

import { useEffect, useRef, useState } from "react";
import { inv, subscribe } from "../tauri";

interface Entry {
  rel_path: string;
  is_dir: boolean;
  size: number;
  mtime_ms: number;
}

interface ImageEntry extends Entry {
  root: string;
}

const ROOTS = ["vault", "results"] as const;
const IMAGE_EXTS = ["png", "jpg", "jpeg", "gif", "svg", "webp"];
const MAX_ENTRIES = 200;
const LIST_DEBOUNCE_MS = 1000;

function mimeFor(relPath: string): string {
  const ext = relPath.split(".").pop()?.toLowerCase() ?? "";
  if (ext === "svg") return "image/svg+xml";
  if (ext === "jpg" || ext === "jpeg") return "image/jpeg";
  if (ext === "gif") return "image/gif";
  if (ext === "webp") return "image/webp";
  return "image/png";
}

function relTail(relPath: string): string {
  const parts = relPath.split("/");
  return parts[parts.length - 1] ?? relPath;
}

function ageLabel(mtimeMs: number, nowMs: number): string {
  const s = Math.max(0, Math.floor((nowMs - mtimeMs) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h`;
}

function sameEntry(a: ImageEntry | null, b: ImageEntry): boolean {
  return !!a && a.root === b.root && a.rel_path === b.rel_path;
}

export default function ImagesPane() {
  const [files, setFiles] = useState<ImageEntry[]>([]);
  const [selected, setSelected] = useState<ImageEntry | null>(null);
  const [dataUri, setDataUri] = useState<string | null>(null);
  const [watchLatest, setWatchLatest] = useState(true);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const selectedRef = useRef<ImageEntry | null>(null);
  const watchLatestRef = useRef(true);
  selectedRef.current = selected;
  watchLatestRef.current = watchLatest;

  async function refresh() {
    // A root that doesn't exist on this machine (e.g. no vault configured)
    // must not break the pane — treat its list as empty and keep going.
    const lists = await Promise.all(
      ROOTS.map(async (root) => {
        try {
          const entries = await inv<Entry[]>("fs_list", { root, rel: "", exts: IMAGE_EXTS });
          return entries.map((e): ImageEntry => ({ ...e, root }));
        } catch {
          return [] as ImageEntry[];
        }
      }),
    );
    const merged = lists
      .flat()
      .sort((a, b) => b.mtime_ms - a.mtime_ms)
      .slice(0, MAX_ENTRIES);
    setFiles(merged);

    if (merged.length === 0) return;
    const prev = selectedRef.current;
    if (!prev) {
      setSelected(merged[0]);
      return;
    }
    if (watchLatestRef.current) {
      const newest = merged[0];
      if (!sameEntry(prev, newest)) setSelected(newest);
    }
  }

  useEffect(() => {
    let cancelled = false;
    void Promise.all(
      ROOTS.map((root) =>
        inv("fs_watch", { root, rel: "" }).catch(() => {
          // Missing/unreadable root: no watcher for it, but the other root
          // still works.
        }),
      ),
    ).then(() => {
      if (!cancelled) void refresh();
    });
    const sub = subscribe<{ root: string; rel_path: string }>("fs-change", (payload) => {
      if (cancelled) return;
      if (!(ROOTS as readonly string[]).includes(payload.root)) return;
      if (debounceRef.current) clearTimeout(debounceRef.current);
      debounceRef.current = setTimeout(() => void refresh(), LIST_DEBOUNCE_MS);
    });
    return () => {
      cancelled = true;
      sub.unsubscribe();
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!selected) {
      setDataUri(null);
      return;
    }
    let cancelled = false;
    void inv<string>("fs_read_binary", { root: selected.root, rel: selected.rel_path }).then(
      (b64) => {
        if (!cancelled) setDataUri(`data:${mimeFor(selected.rel_path)};base64,${b64}`);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [selected]);

  function openSelected() {
    if (selected) void inv("fs_open_external", { root: selected.root, rel: selected.rel_path });
  }

  const now = Date.now();

  return (
    <div className="flex h-full text-xs">
      <div className="flex w-[30%] shrink-0 flex-col overflow-hidden border-r border-term-edge">
        <div className="flex items-center gap-2 border-b border-term-edge px-2 py-1 text-[11px] text-term-dim">
          <button
            type="button"
            onClick={() => setWatchLatest((v) => !v)}
            className={watchLatest ? "text-term-accent" : "text-term-dim hover:text-term-fg"}
          >
            [watch latest]
          </button>
          <button
            type="button"
            onClick={openSelected}
            disabled={!selected}
            className="ml-auto text-term-dim hover:text-term-fg disabled:opacity-40"
          >
            [open]
          </button>
        </div>
        <ul className="overflow-auto">
          {files.length === 0 && (
            <li className="p-2 text-[11px] text-term-dim">no images found</li>
          )}
          {files.map((f) => (
            <li key={`${f.root}/${f.rel_path}`}>
              <button
                type="button"
                onClick={() => setSelected(f)}
                className={`flex w-full items-center gap-2 truncate px-2 py-1 text-left text-[11px] ${
                  sameEntry(selected, f)
                    ? "bg-term-raised text-term-accent"
                    : "text-term-dim hover:text-term-fg"
                }`}
              >
                <span className="truncate">{relTail(f.rel_path)}</span>
                <span className="ml-auto shrink-0 text-term-dim">{ageLabel(f.mtime_ms, now)}</span>
              </button>
            </li>
          ))}
        </ul>
      </div>
      <div className="flex flex-1 items-center justify-center overflow-auto p-2">
        {dataUri ? (
          <img
            src={dataUri}
            alt={selected?.rel_path ?? ""}
            style={{ objectFit: "contain", maxWidth: "100%", maxHeight: "100%" }}
          />
        ) : (
          <span className="text-[11px] text-term-dim">select an image</span>
        )}
      </div>
    </div>
  );
}
