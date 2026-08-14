// The update overlay's log model: ANSI stripped, `\r` applied as an
// overwrite, escape sequences split across pty chunk boundaries survive.

import { describe, expect, it } from "vitest";
import { applyCarriageReturns, createUpdateLog, stripAnsi } from "../desktop/updateLog";

describe("stripAnsi", () => {
  it("removes SGR color sequences", () => {
    expect(stripAnsi("\x1b[32mgreen\x1b[0m plain")).toBe("green plain");
  });

  it("removes OSC titles terminated by BEL and by ST", () => {
    expect(stripAnsi("\x1b]0;window title\x07text")).toBe("text");
    expect(stripAnsi("\x1b]8;;https://x\x1b\\link")).toBe("link");
  });

  it("removes cursor-movement CSI and 2-byte escapes", () => {
    expect(stripAnsi("a\x1b[2K\x1b[1Gb\x1bMc")).toBe("abc");
  });

  it("drops stray control chars but keeps tab, cr, lf", () => {
    expect(stripAnsi("a\x07b\tc\rd\ne")).toBe("ab\tc\rd\ne");
  });
});

describe("applyCarriageReturns", () => {
  it("overwrites from column 0, keeping a longer remainder", () => {
    expect(applyCarriageReturns("abc\rX")).toBe("Xbc");
  });

  it("last full-width rewrite wins (progress bar case)", () => {
    expect(applyCarriageReturns("10%...\r20%...\r100%..")).toBe("100%..");
  });

  it("is identity without a cr", () => {
    expect(applyCarriageReturns("plain")).toBe("plain");
  });
});

describe("createUpdateLog", () => {
  it("splits lines and keeps the in-progress tail", () => {
    const log = createUpdateLog();
    log.feed("one\ntwo\nthr");
    expect(log.lines()).toEqual(["one", "two", "thr"]);
    log.feed("ee\n");
    expect(log.lines()).toEqual(["one", "two", "three", ""]);
  });

  it("applies a cr overwrite arriving in a later chunk to the same line", () => {
    const log = createUpdateLog();
    log.feed("Compiling 10/100");
    log.feed("\rCompiling 99/100");
    expect(log.lines()).toEqual(["Compiling 99/100"]);
  });

  it("carries an escape sequence split across chunks", () => {
    const log = createUpdateLog();
    // SGR split mid-sequence: "\x1b[3" + "2mok"
    log.feed("a\x1b[3");
    expect(log.lines()).toEqual(["a"]);
    log.feed("2mok");
    expect(log.lines()).toEqual(["aok"]);
  });

  it("carries a split OSC until its terminator", () => {
    const log = createUpdateLog();
    log.feed("x\x1b]0;half a title");
    expect(log.lines()).toEqual(["x"]);
    log.feed(" done\x07y");
    expect(log.lines()).toEqual(["xy"]);
  });

  it("reports whether the visible text changed", () => {
    const log = createUpdateLog();
    expect(log.feed("text")).toBe(true);
    expect(log.feed("\x1b[0m")).toBe(false);
    // A carried partial sequence changes nothing visible yet.
    expect(log.feed("\x1b[3")).toBe(false);
  });
});
