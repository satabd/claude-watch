// SSE wrapper that auto-reconnects.

export interface LiveEvent {
  kind: "event" | "session-touched" | "hello" | "ping";
  bucket?: string;
  session_id?: string;
  event?: any;
  size?: number;
  modified_ms?: number;
}

export type SseHandler = (e: LiveEvent) => void;

export class LiveStream {
  private es: EventSource | null = null;
  private handlers = new Set<SseHandler>();
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private connected = false;

  on(h: SseHandler): () => void {
    this.handlers.add(h);
    return () => this.handlers.delete(h);
  }

  isConnected() {
    return this.connected;
  }

  private emit(kind: LiveEvent["kind"], payload: any) {
    const ev: LiveEvent = { kind, ...payload };
    this.handlers.forEach((h) => h(ev));
  }

  start() {
    this.stop();
    const es = new EventSource("/sse/live");
    this.es = es;
    es.addEventListener("hello", () => {
      this.connected = true;
      this.emit("hello", {});
    });
    es.addEventListener("event", (e) => {
      try {
        this.emit("event", JSON.parse((e as MessageEvent).data));
      } catch {}
    });
    es.addEventListener("session-touched", (e) => {
      try {
        this.emit("session-touched", JSON.parse((e as MessageEvent).data));
      } catch {}
    });
    es.addEventListener("ping", () => {
      this.emit("ping", {});
    });
    es.onerror = () => {
      this.connected = false;
      es.close();
      this.es = null;
      // exponential-ish backoff capped at 5s
      if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
      this.reconnectTimer = setTimeout(() => this.start(), 1500);
    };
  }

  stop() {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.es) {
      this.es.close();
      this.es = null;
    }
    this.connected = false;
  }
}

export const liveStream = new LiveStream();
