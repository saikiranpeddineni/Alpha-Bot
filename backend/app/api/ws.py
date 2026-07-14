"""WebSocket event hub.

Fans engine events out to every connected dashboard. `publish` is safe to
call from the engine loop or worker threads; messages are JSON envelopes:
{"type": "...", "data": ...}.

Design notes:
  - Each client gets a dedicated asyncio.Queue.  Slow clients are dropped by
    queue overflow, NOT by cancelling a send_text mid-frame.  Cancelling
    send_text mid-frame corrupts the WebSocket protocol framing and causes the
    browser to close the connection and reload (the blank-screen loop bug).
  - The send loop for each client runs as a Task and exits cleanly on any
    send error, removing the client from the hub.
  - Messages larger than MAX_MSG_BYTES are discarded so a single large training
    readiness report cannot stall the queue.
  - The hub tracks total pending items across all clients; if the sum exceeds
    MAX_PENDING_BROADCASTS, new messages are silently dropped (training flood
    guard).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder

from app.core.logging import applog

# Sentinel object placed in a client queue to signal the send-loop to exit.
_CLOSE = object()

MAX_MSG_BYTES = 131072       # 128 KB — drop any single message larger than this
MAX_QUEUE_DEPTH = 64         # per-client queue depth before we drop new messages
MAX_PENDING_BROADCASTS = 200  # total items in all queues before publish drops globally


class EventHub:
    def __init__(self) -> None:
        # map of WebSocket -> asyncio.Queue
        self._clients: dict[WebSocket, asyncio.Queue] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # ------------------------------------------------------------------
    # Public API (thread-safe)
    # ------------------------------------------------------------------

    def publish(self, msg_type: str, data: Any) -> None:
        """Enqueue a message for all connected clients.  Safe from any thread."""
        if self._loop is None or self._loop.is_closed():
            return

        # Global flood guard: count total queued items across all clients
        total_pending = 0  # Client queues are only inspected on the server loop below.
        if total_pending >= MAX_PENDING_BROADCASTS:
            return  # drop — training burst in progress

        try:
            payload = json.dumps(
                jsonable_encoder({"type": msg_type, "data": data}), default=str
            )
        except Exception:
            return  # serialisation error — discard

        if len(payload) > MAX_MSG_BYTES:
            return  # message too large — discard

        # The trainer's readiness audit calls this from asyncio.to_thread().
        # asyncio.Queue and _clients are owned by the server loop, so never
        # touch them directly from that worker thread.
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._enqueue(payload)
        else:
            self._loop.call_soon_threadsafe(self._enqueue, payload)

    def _enqueue(self, payload: str) -> None:
        """Enqueue on the server loop only."""
        if not self._clients:
            return
        total_pending = sum(q.qsize() for q in self._clients.values())
        if total_pending >= MAX_PENDING_BROADCASTS:
            return
        for q in list(self._clients.values()):
            if q.qsize() < MAX_QUEUE_DEPTH:
                q.put_nowait(payload)

    # ------------------------------------------------------------------
    # Connection handler (called from the WebSocket endpoint)
    # ------------------------------------------------------------------

    async def handle(self, ws: WebSocket, bootstrap: dict) -> None:
        """Accept a new WebSocket connection, send bootstrap, then relay events."""
        try:
            await ws.accept()
        except Exception:
            return

        # Send bootstrap synchronously before adding to the client set.
        # If this fails the connection is already broken — close and return.
        try:
            bootstrap_payload = json.dumps(
                jsonable_encoder({"type": "bootstrap", "data": bootstrap}),
                default=str,
            )
            await ws.send_text(bootstrap_payload)
        except Exception:
            try:
                await ws.close(code=1011)
            except Exception:
                pass
            return

        # Register client with its own queue
        q: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE_DEPTH)
        self._clients[ws] = q
        applog.debug("ws", f"client connected ({len(self._clients)} total)")

        # Start the dedicated send-loop task for this client
        send_task = asyncio.create_task(self._send_loop(ws, q))

        try:
            while True:
                await ws.receive_text()  # keepalive / ping frames from client
        except (WebSocketDisconnect, Exception):  # noqa: BLE001
            pass
        finally:
            self._clients.pop(ws, None)
            # Make room for the close sentinel.  Do not cancel an active
            # send_text call: cancelling it mid-frame is unsafe on Windows.
            while q.full():
                q.get_nowait()
            q.put_nowait(_CLOSE)
            applog.debug("ws", f"client disconnected ({len(self._clients)} total)")

    # ------------------------------------------------------------------
    # Per-client send loop
    # ------------------------------------------------------------------

    async def _send_loop(self, ws: WebSocket, q: asyncio.Queue) -> None:
        """Drain q and send each message to ws.  Exits on send error or _CLOSE."""
        try:
            while True:
                item = await q.get()
                if item is _CLOSE:
                    break
                try:
                    await ws.send_text(item)  # NO timeout — never cancel mid-frame
                except Exception:
                    # Client is dead — stop sending; handle() will clean up
                    break
        except asyncio.CancelledError:
            pass
