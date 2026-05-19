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

const GIB = 1024 ** 3;

function specsFor(model: string, temp: number | null, cpu = 5.5) {
  return {
    model_name: model,
    os: "linux",
    arch: "aarch64",
    cpu_cores: 4,
    ram_total_bytes: 8 * GIB,
    disk_total_bytes: 128 * GIB,
    cpu_percent: cpu,
    mem_used_bytes: 2 * GIB,
    disk_used_bytes: 10 * GIB,
    temp_celsius: temp,
    uptime_seconds: 3600,
    loadavg_1m: 0.5,
    loadavg_5m: 0.4,
    loadavg_15m: 0.3,
  };
}

const peersBody = {
  peers: [
    {
      node_id: "self-id",
      node_name: "pi-alpha",
      self: true,
      capabilities: [],
      last_seen: null,
      specs: specsFor("Raspberry Pi 5 Model B Rev 1.0", 47.5, 11.0),
    },
    {
      node_id: "mac-id",
      node_name: "mbp",
      self: false,
      capabilities: [],
      last_seen: 0,
      specs: specsFor("MacBookPro18,3", null, 5.5),
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
    expect(self.textContent).toContain("Pi 5");

    const mac = screen.getByTestId("specs-row-mac-id");
    expect(mac.textContent).toContain("—");
  });
});
