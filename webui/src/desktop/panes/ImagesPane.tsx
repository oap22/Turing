// Image viewer pane — browses image files under the `vault` and `results`
// roots (plots/artifacts research runs drop, plus anything in the vault),
// merges them newest-first, and previews the selected one.
//
// Selection discipline (issues #391, #389): the pane follows the newest image
// until the user picks one, and an image the user deliberately selected or
// enlarged does not change without another user action.

import { useEffect, useRef, useState } from "react";
import { inv, subscribe } from "../tauri";
import {
  type Entry,
  type ImageEntry,
  IMAGE_EXTS,
  ageLabel,
  mimeFor,
  newerCount,
  nextSelection,
  relTail,
  sameEntry,
  stepSelection,
} from "./images";

const ROOTS = ["vault", "results"] as const;
const MAX_ENTRIES = 200;
const LIST_DEBOUNCE_MS = 1000;

export default function ImagesPane() {
  const [files, setFiles] = useState<ImageEntry[]>([]);
  const [selected, setSelected] = useState<ImageEntry | null>(null);
  // Bytes stay tagged with the entry they were read from. Rendering is gated
  // on that tag matching the selection, so a failed or slow read can never
  // caption one image with another's name.
  const [image, setImage] = useState<{ entry: ImageEntry; uri: string } | null>(
    null,
  );
  const [watchLatest, setWatchLatest] = useState(true);
  const [enlarged, setEnlarged] = useState(false);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const selectedRef = useRef<ImageEntry | null>(null);
  const watchLatestRef = useRef(true);
  const filesRef = useRef<ImageEntry[]>([]);
  const enlargedRef = useRef(false);
  const lightboxRef = useRef<HTMLDivElement | null>(null);
  selectedRef.current = selected;
  watchLatestRef.current = watchLatest;
  filesRef.current = files;
  enlargedRef.current = enlarged;

  // A deliberate selection ends follow-mode. Without this the pane cannot
  // tell the selection it made from the one the user made, and every user
  // choice is overwritten by the next file to land (#391).
  function selectByUser(f: ImageEntry) {
    setSelected(f);
    setWatchLatest(false);
  }

  // The one-click way back: re-select the newest and resume following.
  function jumpToNewest() {
    const newest = filesRef.current[0];
    if (!newest) return;
    setSelected(newest);
    setWatchLatest(true);
  }

  async function refresh() {
    // A root that doesn't exist on this machine (e.g. no vault configured)
    // must not break the pane — treat its list as empty and keep going.
    const lists = await Promise.all(
      ROOTS.map(async (root) => {
        try {
          const entries = await inv<Entry[]>("fs_list", {
            root,
            rel: "",
            exts: IMAGE_EXTS,
          });
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

    // An enlarged image is pinned: swapping it out mid-look is the worst
    // version of #391, so following is suspended for as long as it is up.
    const following = watchLatestRef.current && !enlargedRef.current;
    const next = nextSelection(merged, selectedRef.current, following);
    if (next) setSelected(next);
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
    const sub = subscribe<{ root: string; rel_path: string }>(
      "fs-change",
      (payload) => {
        if (cancelled) return;
        if (!(ROOTS as readonly string[]).includes(payload.root)) return;
        if (debounceRef.current) clearTimeout(debounceRef.current);
        debounceRef.current = setTimeout(
          () => void refresh(),
          LIST_DEBOUNCE_MS,
        );
      },
    );
    return () => {
      cancelled = true;
      sub.unsubscribe();
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!selected) {
      setImage(null);
      return;
    }
    let cancelled = false;
    void inv<string>("fs_read_binary", {
      root: selected.root,
      rel: selected.rel_path,
    })
      .then((b64) => {
        if (!cancelled)
          setImage({
            entry: selected,
            uri: `data:${mimeFor(selected.rel_path)};base64,${b64}`,
          });
      })
      .catch(() => {
        // Deleted or truncated mid-view — a real case while rsync mirrors
        // into the watched roots. The bytes stay tagged with the entry they
        // came from, so the pane keeps showing the last good image *and*
        // keeps calling it by its own name; the next refresh drops the dead
        // entry from the list. Showing old pixels under a new caption would
        // be worse than showing nothing.
      });
    return () => {
      cancelled = true;
    };
  }, [selected]);

  // Lightbox keys, bound to the lightbox element rather than to `window`.
  //
  // A window listener was wrong in two ways that both broke the pane's own
  // invariant. Panes in inactive workspaces stay mounted (rendered with
  // `display: none`), so an enlarged pane on another workspace consumed
  // arrows meant for the visible one. And the shell only stops propagation
  // for Escape and ⌘-digits while the ⌘p/⌘/ overlay is open, so a bare arrow
  // pressed while editing the launcher's query reached this handler, moved
  // the pinned image and silently ended follow-mode.
  //
  // Keying off the focused element scopes both away: a hidden element cannot
  // hold focus, and the launcher's input takes it while the overlay is up.
  function onLightboxKey(e: React.KeyboardEvent<HTMLDivElement>) {
    if (e.key === "Escape") {
      e.preventDefault();
      setEnlarged(false);
      return;
    }
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    // Newest-first order: ArrowRight walks toward older images, matching
    // the direction the list reads.
    const next = stepSelection(
      filesRef.current,
      selectedRef.current,
      e.key === "ArrowRight" ? 1 : -1,
    );
    // Stepping is a user action, so it also ends follow-mode and keeps the
    // list highlight in sync.
    if (next) selectByUser(next);
  }

  // Focus the lightbox when it opens so it receives those keys. This follows
  // a click on this pane, so it takes nothing the user had elsewhere, and it
  // moves no pointer.
  useEffect(() => {
    if (enlarged) lightboxRef.current?.focus();
  }, [enlarged]);

  function openSelected() {
    if (selected)
      void inv("fs_open_external", {
        root: selected.root,
        rel: selected.rel_path,
      });
  }

  const now = Date.now();
  // Only show bytes that belong to the current selection. Anything else is a
  // stale read, and captioning it with the new name would mislabel a plot.
  const shown = image && sameEntry(image.entry, selected) ? image : null;
  const newer = newerCount(files, selected);

  return (
    <div className="flex h-full text-xs">
      <div className="flex w-[30%] shrink-0 flex-col overflow-hidden border-r border-term-edge">
        <div className="flex items-center gap-2 border-b border-term-edge px-2 py-1 text-[11px] text-term-dim">
          <button
            type="button"
            onClick={() =>
              watchLatest ? setWatchLatest(false) : jumpToNewest()
            }
            title={
              watchLatest
                ? "following the newest image — click to stop"
                : "resume following the newest image"
            }
            className={
              watchLatest
                ? "text-term-accent"
                : "text-term-dim hover:text-term-fg"
            }
          >
            [watch latest]
          </button>
          {!watchLatest && newer > 0 && (
            <button
              type="button"
              onClick={jumpToNewest}
              title="jump to the newest image and resume following"
              className="text-term-accent hover:text-term-fg"
            >
              [{newer} new]
            </button>
          )}
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
                onClick={() => selectByUser(f)}
                className={`flex w-full items-center gap-2 truncate px-2 py-1 text-left text-[11px] ${
                  sameEntry(selected, f)
                    ? "bg-term-raised text-term-accent"
                    : "text-term-dim hover:text-term-fg"
                }`}
              >
                <span className="truncate">{relTail(f.rel_path)}</span>
                <span className="ml-auto shrink-0 text-term-dim">
                  {ageLabel(f.mtime_ms, now)}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </div>
      {/* `relative` scopes the lightbox to the pane's own rect, so it cannot
          escape its tile and never touches the layout reducer. */}
      <div className="relative flex flex-1 items-center justify-center overflow-auto p-2">
        {shown ? (
          <img
            src={shown.uri}
            alt={shown.entry.rel_path}
            onClick={() => setEnlarged(true)}
            className="cursor-zoom-in"
            style={{
              objectFit: "contain",
              maxWidth: "100%",
              maxHeight: "100%",
            }}
          />
        ) : (
          <span className="text-[11px] text-term-dim">
            {selected ? "reading image…" : "select an image"}
          </span>
        )}
        {enlarged && shown && (
          <div
            ref={lightboxRef}
            role="presentation"
            tabIndex={-1}
            onKeyDown={onLightboxKey}
            onClick={() => setEnlarged(false)}
            className="absolute inset-0 z-10 flex flex-col items-center justify-center bg-term-bg/90 p-2 outline-none"
          >
            <img
              src={shown.uri}
              alt={shown.entry.rel_path}
              onClick={(e) => {
                // Clicking the image itself dismisses too, but stop the event
                // so it isn't also counted as a backdrop click.
                e.stopPropagation();
                setEnlarged(false);
              }}
              className="cursor-zoom-out"
              style={{
                objectFit: "contain",
                maxWidth: "100%",
                maxHeight: "100%",
              }}
            />
            <div className="pointer-events-none mt-1 shrink-0 truncate text-[11px] text-term-dim">
              {relTail(shown.entry.rel_path)} · ←/→ step · esc close
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
