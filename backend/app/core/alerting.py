import asyncio
import httpx
from typing import Optional
from app.core.applog import LogEntry, applog

class SlackAlerter:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        self._client: Optional[httpx.AsyncClient] = None
        self._queue: asyncio.Queue[LogEntry] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        if not self.webhook_url:
            applog.info("alerting", "Slack webhook URL not provided, alerts disabled.")
            return
        
        self._client = httpx.AsyncClient(timeout=5.0)
        self._task = asyncio.create_task(self._worker())
        applog.info("alerting", "Slack alerting initialized.")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()

    def handle_log(self, entry: LogEntry):
        if not self.webhook_url:
            return
        if entry.level == "error":
            # Fire and forget enqueue to prevent blocking the logger
            try:
                self._queue.put_nowait(entry)
            except Exception:
                pass

    async def _worker(self):
        while True:
            try:
                entry = await self._queue.get()
                await self._send_slack_alert(entry)
                self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                # Do not use applog.error here to prevent recursive loops!
                print(f"[SlackAlerter] Failed to send alert: {exc}")
                await asyncio.sleep(5)

    async def _send_slack_alert(self, entry: LogEntry):
        if not self._client or not self.webhook_url:
            return
            
        payload = {
            "text": f"🚨 *CRITICAL ERROR* [{entry.source}]\n```{entry.message}```"
        }
        
        response = await self._client.post(self.webhook_url, json=payload)
        response.raise_for_status()

    async def send_startup_ping(self):
        if not self._client or not self.webhook_url:
            return
        
        payload = {
            "text": "🟢 *Alpha-Bot Engine Started* - Slack alerting is connected and live!"
        }
        try:
            await self._client.post(self.webhook_url, json=payload)
        except Exception as exc:
            print(f"[SlackAlerter] Failed to send startup ping: {exc}")

# Singleton for application lifecycle
slack_alerter: Optional[SlackAlerter] = None

async def init_alerting(webhook_url: str):
    global slack_alerter
    if slack_alerter is None and webhook_url:
        slack_alerter = SlackAlerter(webhook_url)
        await slack_alerter.start()
        # Subscribe to global logger
        applog.subscribe(slack_alerter.handle_log)
        
        # Fire and forget startup ping
        asyncio.create_task(slack_alerter.send_startup_ping())

async def stop_alerting():
    global slack_alerter
    if slack_alerter:
        await slack_alerter.stop()
        slack_alerter = None
