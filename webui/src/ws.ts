/**
 * Connects to the gateway's authenticated WebSocket and reconnects with
 * exponential backoff. The browser presents the cookie set by
 * /token-handoff, so no token has to live in JS.
 */

export type Frame = Record<string, unknown> & { type: string };

interface ConnectOptions {
  url: string;
  onFrame: (frame: Frame) => void;
  onStatus?: (status: "open" | "closed" | "reconnecting") => void;
}

const MIN_DELAY_MS = 250;
const MAX_DELAY_MS = 15_000;

export function connectGatewayWS(opts: ConnectOptions): () => void {
  let ws: WebSocket | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let backoff = MIN_DELAY_MS;
  let stopped = false;

  function open() {
    if (stopped) return;
    ws = new WebSocket(opts.url);
    ws.onopen = () => {
      backoff = MIN_DELAY_MS;
      opts.onStatus?.("open");
    };
    ws.onmessage = (event) => {
      try {
        const frame = JSON.parse(event.data) as Frame;
        opts.onFrame(frame);
      } catch {
        // ignore malformed frames; the gateway only emits JSON
      }
    };
    ws.onclose = () => {
      opts.onStatus?.("closed");
      if (stopped) return;
      opts.onStatus?.("reconnecting");
      timer = setTimeout(open, backoff);
      backoff = Math.min(MAX_DELAY_MS, backoff * 2);
    };
    ws.onerror = () => {
      ws?.close();
    };
  }

  open();

  return () => {
    stopped = true;
    if (timer) clearTimeout(timer);
    ws?.close();
  };
}
