// Reconnecting WebSocket client with exponential backoff.

import { useStore } from "./store";
import type { WsMessage } from "./types";

let socket: WebSocket | null = null;
let attempts = 0;
let closedByApp = false;

function wsUrl(): string {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}/ws`;
}

export function connectWebSocket(): void {
  closedByApp = false;
  open();
}

function open(): void {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return;
  }
  socket = new WebSocket(wsUrl());

  socket.onopen = () => {
    attempts = 0;
    useStore.getState().setConnected(true);
  };

  socket.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data) as WsMessage;
      useStore.getState().applyMessage(msg);
    } catch {
      /* ignore malformed frames */
    }
  };

  socket.onclose = () => {
    useStore.getState().setConnected(false);
    socket = null;
    if (!closedByApp) {
      const delay = Math.min(15000, 500 * 2 ** attempts);
      attempts += 1;
      window.setTimeout(open, delay);
    }
  };

  socket.onerror = () => {
    socket?.close();
  };
}

export function disconnectWebSocket(): void {
  closedByApp = true;
  socket?.close();
  socket = null;
}

// Keepalive so intermediaries don't drop the idle connection.
window.setInterval(() => {
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send("ping");
  }
}, 25000);
