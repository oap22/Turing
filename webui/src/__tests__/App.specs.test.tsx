import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";

// Stub the WS connector — App.tsx opens a WebSocket on mount otherwise.
vi.mock("../ws", () => ({
  connectGatewayWS: () => () => {},
}));
// Force the CallGraphCanvas (ReactFlow) into a non-rendering stub to keep
// the jsdom test fast and free of layout-engine quirks.
vi.mock("../CallGraphCanvas", () => ({
  default: () => null,
}));

const peersBody = {
  peers: [
    {
      node_id: "self-id",
      node_name: "pi-alpha",
      self: true,
      capabilities: [],
      last_seen: null,
      specs: { cpu_percent: 11.0, temp_celsius: 47.5 },
    },
    {
      node_id: "mac-id",
      node_name: "mbp",
      self: false,
      capabilities: [],
      last_seen: 0,
      specs: { cpu_percent: 5.5, temp_celsius: null },
    },
  ],
  count: 2,
};

beforeEach(() => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url.endsWith("/peers")) {
      return new Response(JSON.stringify(peersBody), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    return new Response("not found", { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  cleanup();
});

describe("App — specs panel integration", () => {
  it("polls /peers and renders the SpecsGrid with returned rows", async () => {
    render(<App />);
    await waitFor(() => {
      expect(screen.getByTestId("specs-row-self-id")).toBeInTheDocument();
    });
    const self = screen.getByTestId("specs-row-self-id");
    expect(self.textContent).toContain("11.0%");
    expect(self.textContent).toContain("47.5°C");

    const mac = screen.getByTestId("specs-row-mac-id");
    expect(mac.textContent).toContain("—");
  });
});
