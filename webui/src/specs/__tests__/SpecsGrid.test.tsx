import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import SpecsGrid from "../SpecsGrid";
import type { PeerSpecsRow } from "../types";

afterEach(cleanup);

const rows: PeerSpecsRow[] = [
  // Mac peer first to prove we sort and pin self.
  {
    node_id: "mbp-id",
    node_name: "mbp",
    self: false,
    specs: { cpu_percent: 8.1, temp_celsius: null },
  },
  {
    node_id: "self-id",
    node_name: "pi-alpha",
    self: true,
    specs: { cpu_percent: 12.0, temp_celsius: 48.0 },
  },
  {
    node_id: "beta-id",
    node_name: "pi-beta",
    self: false,
    specs: { cpu_percent: 20.5, temp_celsius: 55.5 },
  },
];

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

  it("shows an em-dash in the temp column for Mac peers", () => {
    render(<SpecsGrid rows={rows} />);
    const mac = screen.getByTestId("specs-row-mbp-id");
    expect(mac.textContent).toContain("—");
  });

  it("formats CPU% with one decimal", () => {
    render(<SpecsGrid rows={rows} />);
    const self = screen.getByTestId("specs-row-self-id");
    expect(self.textContent).toContain("12.0%");
    expect(self.textContent).toContain("48.0°C");
  });

  it("renders em-dash for rows with no specs at all", () => {
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
    // Both columns dashed.
    expect(row.textContent?.match(/—/g)?.length).toBe(2);
  });
});
