// Images pane selection rules — the follow-mode fix for #391 and the
// arrow-stepping #389 builds on.

import { describe, expect, it } from "vitest";
import {
  type ImageEntry,
  ageLabel,
  mimeFor,
  newerCount,
  nextSelection,
  relTail,
  sameEntry,
  stepSelection,
} from "../desktop/panes/images";

function img(rel: string, mtime: number, root = "results"): ImageEntry {
  return { rel_path: rel, is_dir: false, size: 1, mtime_ms: mtime, root };
}

// Newest-first, the order the pane sorts into and the list renders.
const newest = img("rosie-live/loss.png", 300);
const middle = img("rosie-live/acc.png", 200);
const oldest = img("plots/baseline.png", 100);
const files = [newest, middle, oldest];

describe("nextSelection", () => {
  it("auto-selects the newest when nothing is chosen yet", () => {
    expect(nextSelection(files, null, true)).toBe(newest);
  });

  it("does not auto-select when the user turned following off first", () => {
    // Turning [watch latest] off before any image has landed is still an
    // instruction not to follow; honouring it only once something has been
    // selected would ignore it exactly when it was given.
    expect(nextSelection(files, null, false)).toBeNull();
  });

  it("follows the newest while following is on", () => {
    expect(nextSelection(files, middle, true)).toBe(newest);
  });

  it("leaves the selection alone when following is off (#391)", () => {
    expect(nextSelection(files, middle, false)).toBeNull();
    expect(nextSelection(files, oldest, false)).toBeNull();
  });

  it("does not re-select the newest when it is already selected", () => {
    expect(nextSelection(files, newest, true)).toBeNull();
  });

  it("returns null for an empty listing rather than clearing the selection", () => {
    expect(nextSelection([], middle, true)).toBeNull();
  });

  it("keeps a user's selection across repeated arrivals", () => {
    // The reported bug: ssh-pull-assets lands a plot every 30s and the pane
    // rips the view away each time. With following off it must not.
    let disk = files;
    let selection: ImageEntry | null = oldest;
    for (let i = 0; i < 10; i++) {
      disk = [img(`rosie-live/step-${i}.png`, 400 + i), ...disk];
      const next = nextSelection(disk, selection, false);
      if (next) selection = next;
    }
    expect(selection).toBe(oldest);
  });

  it("distinguishes entries with the same path under different roots", () => {
    const a = img("same.png", 100, "vault");
    const b = img("same.png", 100, "results");
    expect(sameEntry(a, b)).toBe(false);
    // So a same-named file in the other root still reads as a new newest.
    expect(nextSelection([b, a], a, true)).toBe(b);
  });
});

describe("newerCount", () => {
  it("counts the images ahead of the selection", () => {
    expect(newerCount(files, oldest)).toBe(2);
    expect(newerCount(files, middle)).toBe(1);
    expect(newerCount(files, newest)).toBe(0);
  });

  it("is zero with no selection", () => {
    expect(newerCount(files, null)).toBe(0);
  });

  it("counts everything as newer when the selection has gone from the list", () => {
    // Deleted, or pushed out of the MAX_ENTRIES window by a long run. The
    // list highlight is already gone; reporting 0 would also hide [N new],
    // leaving no way back to following at the moment it is most needed.
    expect(newerCount(files, img("gone.png", 999))).toBe(3);
    expect(newerCount([], img("gone.png", 999))).toBe(0);
  });
});

describe("stepSelection", () => {
  it("steps toward older and newer in list order", () => {
    expect(stepSelection(files, middle, 1)).toBe(oldest);
    expect(stepSelection(files, middle, -1)).toBe(newest);
  });

  it("clamps at both ends instead of wrapping", () => {
    expect(stepSelection(files, newest, -1)).toBeNull();
    expect(stepSelection(files, oldest, 1)).toBeNull();
  });

  it("falls back to the newest when the selection vanished from disk", () => {
    expect(stepSelection(files, img("deleted.png", 50), 1)).toBe(newest);
  });

  it("returns null when there is nothing to step through", () => {
    expect(stepSelection([], middle, 1)).toBeNull();
  });
});

describe("presentation helpers", () => {
  it("maps extensions to mime types, defaulting to png", () => {
    expect(mimeFor("a/b/loss.svg")).toBe("image/svg+xml");
    expect(mimeFor("a.JPG")).toBe("image/jpeg");
    expect(mimeFor("a.jpeg")).toBe("image/jpeg");
    expect(mimeFor("a.gif")).toBe("image/gif");
    expect(mimeFor("a.webp")).toBe("image/webp");
    expect(mimeFor("noext")).toBe("image/png");
  });

  it("shows the filename tail", () => {
    expect(relTail("rosie-live/deep/loss.png")).toBe("loss.png");
    expect(relTail("loss.png")).toBe("loss.png");
  });

  it("labels age in s/m/h and never goes negative", () => {
    const now = 1_000_000;
    expect(ageLabel(now - 5_000, now)).toBe("5s");
    expect(ageLabel(now - 120_000, now)).toBe("2m");
    expect(ageLabel(now - 7_200_000, now)).toBe("2h");
    // Clock skew on a mirrored file must not render "-3s".
    expect(ageLabel(now + 3_000, now)).toBe("0s");
  });
});
