import { describe, expect, it } from "vitest";
import {
  CPU_DANGER,
  CPU_WARN,
  DISK_DANGER,
  DISK_WARN,
  MEM_DANGER,
  MEM_WARN,
  TEMP_DANGER,
  TEMP_WARN,
  severityFor,
} from "../thresholds";
import type { NodeSpecs } from "../types";

const GIB = 1024 ** 3;

function specs(overrides: Partial<NodeSpecs> = {}): NodeSpecs {
  return {
    model_name: "test",
    os: "linux",
    arch: "aarch64",
    cpu_cores: 4,
    ram_total_bytes: 100,
    disk_total_bytes: 100,
    cpu_percent: 0,
    mem_used_bytes: 0,
    disk_used_bytes: 0,
    temp_celsius: 25.0,
    uptime_seconds: 0,
    loadavg_1m: 0,
    loadavg_5m: 0,
    loadavg_15m: 0,
    ...overrides,
  };
}

describe("severityFor — CPU", () => {
  it("ok below warn", () => {
    expect(severityFor(specs({ cpu_percent: CPU_WARN - 1 }))).toBe("ok");
  });
  it("warn at exact threshold", () => {
    expect(severityFor(specs({ cpu_percent: CPU_WARN }))).toBe("warn");
  });
  it("danger at exact threshold", () => {
    expect(severityFor(specs({ cpu_percent: CPU_DANGER }))).toBe("danger");
  });
});

describe("severityFor — memory (uses utilisation pct)", () => {
  it("warn at the exact MEM_WARN ratio", () => {
    const s = specs({ ram_total_bytes: 100, mem_used_bytes: MEM_WARN });
    expect(severityFor(s)).toBe("warn");
  });
  it("danger at MEM_DANGER ratio", () => {
    const s = specs({ ram_total_bytes: 100, mem_used_bytes: MEM_DANGER });
    expect(severityFor(s)).toBe("danger");
  });
  it("ok just below MEM_WARN", () => {
    const s = specs({ ram_total_bytes: 100, mem_used_bytes: MEM_WARN - 1 });
    expect(severityFor(s)).toBe("ok");
  });
});

describe("severityFor — disk", () => {
  it("warn at the DISK_WARN ratio", () => {
    const s = specs({ disk_total_bytes: 100, disk_used_bytes: DISK_WARN });
    expect(severityFor(s)).toBe("warn");
  });
  it("danger at the DISK_DANGER ratio", () => {
    const s = specs({ disk_total_bytes: 100, disk_used_bytes: DISK_DANGER });
    expect(severityFor(s)).toBe("danger");
  });
});

describe("severityFor — temperature", () => {
  it("warn at TEMP_WARN", () => {
    expect(severityFor(specs({ temp_celsius: TEMP_WARN }))).toBe("warn");
  });
  it("danger at TEMP_DANGER", () => {
    expect(severityFor(specs({ temp_celsius: TEMP_DANGER }))).toBe("danger");
  });
  it("null temp does not raise severity", () => {
    expect(severityFor(specs({ temp_celsius: null }))).toBe("ok");
  });
});

describe("severityFor — worst-of across fields", () => {
  it("a single danger field pegs the row to danger", () => {
    const s = specs({
      cpu_percent: 5,
      temp_celsius: TEMP_DANGER + 5,
    });
    expect(severityFor(s)).toBe("danger");
  });
  it("multiple warn fields stay at warn", () => {
    const s = specs({
      cpu_percent: CPU_WARN + 1,
      temp_celsius: TEMP_WARN + 1,
    });
    expect(severityFor(s)).toBe("warn");
  });
  it("returns ok for null specs", () => {
    expect(severityFor(null)).toBe("ok");
    expect(severityFor(undefined)).toBe("ok");
  });
  it("ignores ram_total_bytes <= 0 to avoid division blowup", () => {
    expect(severityFor(specs({ ram_total_bytes: 0, mem_used_bytes: 1000 }))).toBe("ok");
  });
  it("does not penalise low utilisation when ram=GiB scale", () => {
    expect(
      severityFor(specs({ ram_total_bytes: 8 * GIB, mem_used_bytes: 1 * GIB })),
    ).toBe("ok");
  });
});
