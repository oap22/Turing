// Test ε: AlertBanner renders alerts with severity-correct styling.
// Test θ-TS: parses the golden fixture into the AlertFrame shape without drift.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import AlertBanner from "../AlertBanner";
import { applyAlert, emptyAlerts, listAlerts } from "../reducer";
import type { Alert, AlertFrame } from "../types";
import golden from "./fixtures/alert_frames_golden.json";

afterEach(cleanup);

function alert(overrides: Partial<Alert> = {}): Alert {
  return {
    node_id: "pi-beta",
    node_name: "pi-beta",
    field: "temp_celsius",
    severity: "warn",
    value: 76.4,
    threshold: 75.0,
    state: "alerting",
    fired_at_ms: 1700000000000,
    ...overrides,
  };
}

describe("AlertBanner", () => {
  it("renders nothing when there are no alerts", () => {
    const { container } = render(<AlertBanner alerts={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders a warn row with amber styling", () => {
    render(<AlertBanner alerts={[alert({ severity: "warn" })]} />);
    const row = screen.getByTestId("alert-row");
    expect(row.getAttribute("data-severity")).toBe("warn");
    expect(row.className).toMatch(/amber/);
  });

  it("renders a danger row with rose styling", () => {
    render(
      <AlertBanner
        alerts={[
          alert({ severity: "danger", value: 83.4, threshold: 82.0 }),
        ]}
      />,
    );
    const row = screen.getByTestId("alert-row");
    expect(row.getAttribute("data-severity")).toBe("danger");
    expect(row.className).toMatch(/rose/);
  });

  it("renders peer name + value + threshold", () => {
    render(
      <AlertBanner
        alerts={[
          alert({
            node_name: "pi-gamma",
            severity: "danger",
            value: 83.4,
            threshold: 82.0,
          }),
        ]}
      />,
    );
    expect(screen.getByText("pi-gamma")).toBeInTheDocument();
    expect(screen.getByText("83.4°C (>82.0°C)")).toBeInTheDocument();
  });

  it("renders a disk_pct row with percentage formatting", () => {
    render(
      <AlertBanner
        alerts={[
          alert({
            field: "disk_pct",
            severity: "danger",
            value: 96.4,
            threshold: 95.0,
          }),
        ]}
      />,
    );
    const row = screen.getByTestId("alert-row");
    expect(row.getAttribute("data-severity")).toBe("danger");
    // DISK renders rounded integer percentages, not degrees.
    expect(screen.getByText("96% (>95%)")).toBeInTheDocument();
  });

  it("renders TEMP and DISK rows together, one per (peer, field)", () => {
    render(
      <AlertBanner
        alerts={[
          alert({ field: "temp_celsius", severity: "warn", value: 76.4, threshold: 75.0 }),
          alert({ field: "disk_pct", severity: "danger", value: 96.0, threshold: 95.0 }),
        ]}
      />,
    );
    expect(screen.getAllByTestId("alert-row")).toHaveLength(2);
    expect(screen.getByText("76.4°C (>75.0°C)")).toBeInTheDocument();
    expect(screen.getByText("96% (>95%)")).toBeInTheDocument();
  });
});

describe("reducer", () => {
  it("alerting frame populates state, cleared frame removes", () => {
    let state = emptyAlerts();
    const frame: AlertFrame = {
      type: "alert",
      node_id: "a",
      node_name: "a",
      field: "temp_celsius",
      severity: "danger",
      value: 90,
      threshold: 82,
      state: "alerting",
      fired_at_ms: 1,
    };
    state = applyAlert(state, frame);
    expect(listAlerts(state)).toHaveLength(1);
    state = applyAlert(state, { ...frame, state: "cleared" });
    expect(listAlerts(state)).toHaveLength(0);
  });
});

describe("golden frame parity", () => {
  it("alerting fixture round-trips into AlertFrame", () => {
    const f = golden.alerting as AlertFrame;
    expect(f.type).toBe("alert");
    expect(f.field).toBe("temp_celsius");
    expect(f.state).toBe("alerting");
    expect(f.severity).toBe("danger");
    expect(f.threshold).toBe(82.0);
  });

  it("cleared fixture round-trips into AlertFrame", () => {
    const f = golden.cleared as AlertFrame;
    expect(f.state).toBe("cleared");
    expect(f.node_id).toBe("pi-beta");
  });

  it("disk_alerting fixture round-trips into AlertFrame", () => {
    const f = golden.disk_alerting as AlertFrame;
    expect(f.type).toBe("alert");
    expect(f.field).toBe("disk_pct");
    expect(f.state).toBe("alerting");
    expect(f.severity).toBe("danger");
    expect(f.threshold).toBe(95.0);
  });
});
