// desktop/gateway.ts tests (issue #382): the browser fetch fallback, the
// Tauri-proxied fetch (success + rejection → synthetic 599), and the WS
// event wiring for connectTauriWS. `desktop/tauri.ts` — the only module that
// touches `@tauri-apps/api` — is mocked so these run in plain jsdom.

import { afterEach, describe, expect, it, vi } from "vitest";

const invMock = vi.fn();
const subscribeMock = vi.fn();
let isTauriValue = false;

vi.mock("../desktop/tauri", () => ({
  isTauri: () => isTauriValue,
  inv: (...args: unknown[]) => invMock(...args),
  subscribe: (...args: unknown[]) => subscribeMock(...args),
}));

afterEach(() => {
  vi.clearAllMocks();
  isTauriValue = false;
  vi.unstubAllGlobals();
});

describe("gatewayFetch", () => {
  it("uses a plain same-origin fetch in the browser", async () => {
    isTauriValue = false;
    const fetchMock = vi.fn(async () => new Response("ok", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    const { gatewayFetch } = await import("../desktop/gateway");
    const res = await gatewayFetch("/api/queue/approve/1", { method: "POST", body: "{}" });

    expect(res.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/queue/approve/1",
      expect.objectContaining({ credentials: "same-origin", method: "POST", body: "{}" }),
    );
  });

  it("proxies through inv('gateway_fetch') in Tauri", async () => {
    isTauriValue = true;
    invMock.mockResolvedValueOnce({ status: 204, body: "" });

    const { gatewayFetch } = await import("../desktop/gateway");
    const res = await gatewayFetch("/peers");

    expect(res.status).toBe(204);
    expect(invMock).toHaveBeenCalledWith(
      "gateway_fetch",
      expect.objectContaining({ path: "/peers", method: "GET" }),
    );
  });

  it("maps a rejected invoke to a synthetic 599", async () => {
    isTauriValue = true;
    invMock.mockRejectedValueOnce(new Error("connection refused"));

    const { gatewayFetch } = await import("../desktop/gateway");
    const res = await gatewayFetch("/peers");

    expect(res.status).toBe(599);
  });
});

describe("connectTauriWS", () => {
  it("subscribes before starting the ws proxy and unsubscribes on cleanup", async () => {
    invMock.mockResolvedValue(undefined);
    const callbacks: Record<string, (payload: unknown) => void> = {};
    const unsubFrame = vi.fn();
    const unsubStatus = vi.fn();
    subscribeMock.mockImplementation((event: string, cb: (payload: unknown) => void) => {
      callbacks[event] = cb;
      return {
        unsubscribe: event === "gateway-frame" ? unsubFrame : unsubStatus,
        ready: Promise.resolve(),
      };
    });

    const { connectTauriWS } = await import("../desktop/gateway");
    const onFrame = vi.fn();
    const onStatus = vi.fn();
    const cleanup = connectTauriWS(onFrame, onStatus);

    // Both subscriptions are registered synchronously, and the socket is only
    // started once they have attached — otherwise the first frames would be
    // emitted into a window with no listener and dropped.
    expect(Object.keys(callbacks).sort()).toEqual(["gateway-frame", "gateway-status"]);
    expect(invMock).not.toHaveBeenCalledWith("gateway_ws_start");

    await Promise.resolve();
    await Promise.resolve();
    expect(invMock).toHaveBeenCalledWith("gateway_ws_start");

    callbacks["gateway-frame"]("raw-frame");
    expect(onFrame).toHaveBeenCalledWith("raw-frame");
    callbacks["gateway-status"]({ status: "open" });
    expect(onStatus).toHaveBeenCalledWith("open");

    // Cleanup is a local unsubscribe only; it must never unlisten the shared
    // Tauri listener (see tauri.ts and bus.test.ts).
    cleanup();
    expect(unsubFrame).toHaveBeenCalled();
    expect(unsubStatus).toHaveBeenCalled();
  });
});
