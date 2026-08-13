// Gateway HTTP/WS access from inside Tauri: requests and the WS connection
// are proxied through Rust (`gateway.rs`) so the bearer token never reaches
// the webview. Browser callers of `gatewayFetch` fall through to a plain
// same-origin `fetch`.

import { inv, isTauri, subscribe } from "./tauri";

interface GwResponse {
  status: number;
  body: string;
}

export async function gatewayFetch(
  path: string,
  init?: { method?: string; body?: string },
): Promise<Response> {
  if (!isTauri()) {
    return fetch(path, {
      credentials: "same-origin",
      method: init?.method,
      body: init?.body,
      headers: init?.body ? { "Content-Type": "application/json" } : undefined,
    });
  }
  try {
    const r = await inv<GwResponse>("gateway_fetch", {
      method: init?.method ?? "GET",
      path,
      body: init?.body,
    });
    // Statuses 204/205/304 forbid a body on the Response — the Fetch API
    // throws if one is passed even as "".
    const nullBodyStatus = r.status === 204 || r.status === 205 || r.status === 304;
    return new Response(nullBodyStatus ? null : r.body, { status: r.status });
  } catch {
    return new Response("", { status: 599 });
  }
}

export function connectTauriWS(
  onFrame: (raw: string) => void,
  onStatus: (s: "open" | "closed" | "reconnecting") => void,
): () => void {
  let cancelled = false;

  // Subscribe before asking Rust to open the socket, so the first frames
  // cannot land before anyone is listening. Teardown is a local unsubscribe
  // only — the shared Tauri listener stays attached for the app's lifetime
  // (see tauri.ts), which is what makes StrictMode's double-mount harmless.
  const frameSub = subscribe<string>("gateway-frame", (raw) => {
    if (!cancelled) onFrame(raw);
  });
  const statusSub = subscribe<{ status: "open" | "closed" | "reconnecting" }>(
    "gateway-status",
    (payload) => {
      if (!cancelled) onStatus(payload.status);
    },
  );
  void Promise.all([frameSub.ready, statusSub.ready]).then(() => {
    if (!cancelled) void inv("gateway_ws_start");
  });

  return () => {
    cancelled = true;
    frameSub.unsubscribe();
    statusSub.unsubscribe();
  };
}
