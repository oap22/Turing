// Test ζ: an `alert` WS frame (mocked) feeds the banner via the reducer.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import type { AlertFrame } from "../alerts/types";

let frameSink: ((f: unknown) => void) | null = null;

vi.mock("../ws", () => ({
  connectGatewayWS: ({ onFrame }: { onFrame: (f: unknown) => void }) => {
    frameSink = onFrame;
    return () => {
      frameSink = null;
    };
  },
}));
vi.mock("../CallGraphCanvas", () => ({ default: () => null }));

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(JSON.stringify({ peers: [], count: 0 }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    ),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  cleanup();
  frameSink = null;
});

const danger: AlertFrame = {
  type: "alert",
  node_id: "pi-beta",
  node_name: "pi-beta",
  field: "temp_celsius",
  severity: "danger",
  value: 83.4,
  threshold: 82.0,
  state: "alerting",
  fired_at_ms: 1700000000000,
  snoozed_until_ms: null,
};

describe("App — alerts integration", () => {
  it("renders an AlertBanner row when an alert WS frame arrives", async () => {
    render(<App />);
    await waitFor(() => expect(frameSink).not.toBeNull());
    frameSink!(danger);
    await waitFor(() => {
      expect(screen.getByTestId("alert-banner")).toBeInTheDocument();
    });
    const row = screen.getByTestId("alert-row");
    expect(row.getAttribute("data-severity")).toBe("danger");
    expect(row.textContent).toContain("pi-beta");
    expect(row.textContent).toContain("83.4°C");
  });

  it("cleared frame removes the banner row", async () => {
    render(<App />);
    await waitFor(() => expect(frameSink).not.toBeNull());
    frameSink!(danger);
    await waitFor(() => screen.getByTestId("alert-row"));

    frameSink!({ ...danger, state: "cleared" });
    await waitFor(() => {
      expect(screen.queryByTestId("alert-row")).not.toBeInTheDocument();
    });
  });
});
