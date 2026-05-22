// Test ε: AlertBanner renders alerts with severity-correct styling and
// drives the snooze POST. Test θ-TS: parses the golden fixture into the
// AlertFrame shape without drift.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import AlertBanner from "../AlertBanner";
import { applyAlert, emptyAlerts, listAlerts } from "../reducer";
import type { Alert, AlertFrame } from "../types";
import golden from "./fixtures/alert_frames_golden.json";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

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
    snoozed_until_ms: null,
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

describe("AlertBanner snooze", () => {
  it("POSTs to the snooze endpoint and dims the row once snoozed", async () => {
    const fetchMock = vi.fn(
      async () =>
        new Response(
          JSON.stringify({ snoozed_until_ms: Date.now() + 4 * 3600 * 1000 }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const danger = alert({ severity: "danger", value: 83.4, threshold: 82.0 });
    const { rerender } = render(<AlertBanner alerts={[danger]} />);

    fireEvent.click(screen.getByTestId("snooze-button"));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/alerts/pi-beta/temp_celsius/snooze",
        expect.objectContaining({ method: "POST" }),
      ),
    );

    // Authoritative `update` frame arrives via the reducer: snoozed_until_ms set.
    const future = Date.now() + 3 * 3600 * 1000 + 58 * 60 * 1000;
    rerender(<AlertBanner alerts={[{ ...danger, snoozed_until_ms: future }]} />);

    const row = screen.getByTestId("alert-row");
    expect(row.className).toMatch(/opacity-50/);
    expect(screen.getByTestId("snooze-remaining").textContent).toMatch(
      /snoozed \d+h \d+m/,
    );
    // The button is replaced by the remaining-time readout while snoozed.
    expect(screen.queryByTestId("snooze-button")).not.toBeInTheDocument();
  });

  it("an expired snoozed_until_ms leaves the row undimmed", () => {
    const past = Date.now() - 60_000;
    render(
      <AlertBanner
        alerts={[alert({ severity: "danger", snoozed_until_ms: past })]}
      />,
    );
    const row = screen.getByTestId("alert-row");
    expect(row.className).not.toMatch(/opacity-50/);
    expect(screen.getByTestId("snooze-button")).toBeInTheDocument();
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
      snoozed_until_ms: null,
    };
    state = applyAlert(state, frame);
    expect(listAlerts(state)).toHaveLength(1);
    state = applyAlert(state, { ...frame, state: "cleared" });
    expect(listAlerts(state)).toHaveLength(0);
  });

  it("carries snoozed_until_ms from an update frame onto the stored alert", () => {
    let state = emptyAlerts();
    const base: AlertFrame = {
      type: "alert",
      node_id: "a",
      node_name: "a",
      field: "temp_celsius",
      severity: "danger",
      value: 90,
      threshold: 82,
      state: "alerting",
      fired_at_ms: 1,
      snoozed_until_ms: null,
    };
    state = applyAlert(state, base);
    state = applyAlert(state, { ...base, snoozed_until_ms: 1700014400000 });
    expect(listAlerts(state)[0].snoozed_until_ms).toBe(1700014400000);
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

  it("snoozed update fixture round-trips into AlertFrame", () => {
    const f = golden.snoozed as AlertFrame;
    expect(f.type).toBe("alert");
    expect(f.state).toBe("alerting");
    expect(f.snoozed_until_ms).toBe(1700014400000);
  });
});
