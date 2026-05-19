import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import SpecsGrid from "../SpecsGrid";
import {
  formatGiB,
  formatHardwareLabel,
  formatUptime,
  type NodeSpecs,
  type PeerSpecsRow,
} from "../types";

afterEach(cleanup);

const GIB = 1024 ** 3;

function specs(overrides: Partial<NodeSpecs> = {}): NodeSpecs {
  return {
    model_name: "Raspberry Pi 5 Model B Rev 1.0",
    os: "linux",
    arch: "aarch64",
    cpu_cores: 4,
    ram_total_bytes: 8 * GIB,
    disk_total_bytes: 128 * GIB,
    cpu_percent: 12.0,
    mem_used_bytes: 2 * GIB,
    disk_used_bytes: 10 * GIB,
    temp_celsius: 48.0,
    uptime_seconds: 3600,
    loadavg_1m: 0.5,
    loadavg_5m: 0.4,
    loadavg_15m: 0.3,
    ...overrides,
  };
}

const rows: PeerSpecsRow[] = [
  // Mac peer first to prove we sort and pin self.
  {
    node_id: "mbp-id",
    node_name: "mbp",
    self: false,
    specs: specs({
      model_name: "MacBookPro18,3",
      os: "darwin",
      arch: "arm64",
      cpu_cores: 10,
      ram_total_bytes: 16 * GIB,
      disk_total_bytes: 512 * GIB,
      cpu_percent: 8.1,
      temp_celsius: null,
    }),
  },
  {
    node_id: "self-id",
    node_name: "pi-alpha",
    self: true,
    specs: specs(),
  },
  {
    node_id: "beta-id",
    node_name: "pi-beta",
    self: false,
    specs: specs({
      model_name: "Raspberry Pi 4 Model B Rev 1.2",
      cpu_percent: 20.5,
      temp_celsius: 55.5,
    }),
  },
];

describe("formatGiB helper", () => {
  it("rounds to whole GiB at >= 10", () => {
    expect(formatGiB(128 * GIB)).toBe("128 GiB");
  });
  it("uses one decimal under 10 GiB", () => {
    expect(formatGiB(8 * GIB)).toBe("8.0 GiB");
  });
  it("returns em-dash on null/invalid", () => {
    expect(formatGiB(null)).toBe("—");
    expect(formatGiB(0)).toBe("—");
  });
});

describe("formatHardwareLabel helper", () => {
  it("shortens Raspberry Pi model name and includes ram/cores", () => {
    expect(
      formatHardwareLabel(specs({ model_name: "Raspberry Pi 5 Model B Rev 1.0" })),
    ).toBe("Pi 5 · 8.0 GiB · 4c");
  });
  it("shortens MacBook Pro model name", () => {
    expect(
      formatHardwareLabel(
        specs({ model_name: "MacBookPro18,3", ram_total_bytes: 16 * GIB, cpu_cores: 10 }),
      ),
    ).toBe("MacBook Pro · 16 GiB · 10c");
  });
  it("returns em-dash for null specs", () => {
    expect(formatHardwareLabel(null)).toBe("—");
  });
});

describe("formatUptime helper", () => {
  it("uses seconds under a minute", () => {
    expect(formatUptime(30)).toBe("30s");
  });
  it("uses minutes under an hour", () => {
    expect(formatUptime(60 * 5)).toBe("5m");
  });
  it("uses hours under a day", () => {
    expect(formatUptime(60 * 60 * 5)).toBe("5h");
  });
  it("uses days otherwise", () => {
    expect(formatUptime(60 * 60 * 24 * 3)).toBe("3d");
  });
});

describe("<SpecsGrid>", () => {
  it("renders one row per peer including self", () => {
    render(<SpecsGrid rows={rows} />);
    expect(screen.getByTestId("specs-row-self-id")).toBeInTheDocument();
    expect(screen.getByTestId("specs-row-beta-id")).toBeInTheDocument();
    expect(screen.getByTestId("specs-row-mbp-id")).toBeInTheDocument();
  });

  it("pins the self row first, then peers alphabetically", () => {
    const { container } = render(<SpecsGrid rows={rows} />);
    const items = Array.from(container.querySelectorAll("li"));
    const ids = items.map((el) => el.getAttribute("data-testid"));
    expect(ids).toEqual([
      "specs-row-self-id",
      "specs-row-mbp-id",
      "specs-row-beta-id",
    ]);
  });

  it("renders hardware label inline with model, ram, and cores", () => {
    render(<SpecsGrid rows={rows} />);
    expect(screen.getByTestId("hw-self-id").textContent).toBe(
      "Pi 5 · 8.0 GiB · 4c",
    );
    expect(screen.getByTestId("hw-mbp-id").textContent).toBe(
      "MacBook Pro · 16 GiB · 10c",
    );
  });

  it("shows an em-dash in the temp column for Mac peers", () => {
    render(<SpecsGrid rows={rows} />);
    const mac = screen.getByTestId("specs-row-mbp-id");
    expect(mac.textContent).toContain("—");
  });

  it("renders mem and disk usage as 'used / total (pct%)'", () => {
    render(<SpecsGrid rows={rows} />);
    const self = screen.getByTestId("specs-row-self-id");
    expect(self.textContent).toContain("2.0 GiB / 8.0 GiB");
    expect(self.textContent).toContain("10 GiB / 128 GiB");
  });

  it("renders em-dash for legacy rows with no specs", () => {
    render(
      <SpecsGrid
        rows={[
          {
            node_id: "old",
            node_name: "pi-old",
            self: false,
            specs: null,
          },
        ]}
      />,
    );
    const row = screen.getByTestId("specs-row-old");
    expect(row.textContent?.match(/—/g)?.length ?? 0).toBeGreaterThanOrEqual(6);
  });

  it("flags the grid as scrollable when peer count exceeds 6", () => {
    const many: PeerSpecsRow[] = [];
    for (let i = 0; i < 8; i++) {
      many.push({
        node_id: `n${i}`,
        node_name: `node-${i}`,
        self: i === 0,
        specs: specs(),
      });
    }
    render(<SpecsGrid rows={many} />);
    const grid = screen.getByTestId("specs-grid");
    expect(grid.getAttribute("data-scrolls")).toBe("true");
  });

  it("does not flag the grid as scrollable at 6 or fewer", () => {
    render(<SpecsGrid rows={rows} />);
    const grid = screen.getByTestId("specs-grid");
    expect(grid.getAttribute("data-scrolls")).toBe("false");
  });
});
